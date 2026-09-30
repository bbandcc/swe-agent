"""Trusted argv execution inside a capability-less Windows AppContainer."""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agent.workspace.paths import (
    WorkspaceRootError,
    canonical_workspace_relative_path,
    canonicalize_root_path,
)


class SandboxStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class SandboxErrorCode(str, Enum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INVALID_REQUEST = "invalid_request"
    INVALID_WORKSPACE = "invalid_workspace"
    SETUP_FAILED = "setup_failed"
    SPAWN_FAILED = "spawn_failed"
    PROCESS_FAILED = "process_failed"
    TIMEOUT = "timeout"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    """A trusted command request; command text is always an argv tuple."""

    workspace_root: Path
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 30.0
    memory_limit_bytes: int = 512 * 1024 * 1024
    cpu_rate_percent: int = 50
    cpu_time_seconds: float = 300.0
    output_limit_bytes: int = 64 * 1024
    python_toolchain_root: Path | None = None


@dataclass(frozen=True, slots=True)
class SandboxResult:
    status: SandboxStatus
    error_code: SandboxErrorCode | None = None
    exit_code: int | None = None
    message: str = ""
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    started: bool = False
    token_is_appcontainer: bool = False
    cleanup_complete: bool = False
    duration_seconds: float = 0.0
    cpu_time_seconds: float = 0.0
    peak_memory_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _ValidatedCommand:
    workspace_root: Path
    cwd: Path
    argv: tuple[str, ...]
    timeout_seconds: float
    memory_limit_bytes: int
    cpu_rate_percent: int
    cpu_time_seconds: float
    output_limit_bytes: int
    python_toolchain_root: Path | None


def run_in_windows_appcontainer(command: SandboxCommand) -> SandboxResult:
    """Run one argv in a lowbox token, Job Object, and explicit env block.

    Windows AppContainer is the only backend in this slice. Other operating
    systems fail closed instead of falling back to an ordinary subprocess.
    """
    if os.name != "nt":
        return _failure(
            SandboxErrorCode.UNSUPPORTED_PLATFORM,
            "Windows AppContainer is the only supported sandbox backend.",
        )
    validated = _validate(command)
    if isinstance(validated, SandboxResult):
        return validated
    from agent.evaluation._windows_appcontainer import execute_appcontainer

    return execute_appcontainer(validated)


def _validate(command: SandboxCommand) -> _ValidatedCommand | SandboxResult:
    if not isinstance(command, SandboxCommand):
        return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox command is invalid.")
    if (
        not isinstance(command.argv, tuple)
        or not command.argv
        or any(not isinstance(item, str) or not item or "\x00" in item for item in command.argv)
        or isinstance(command.timeout_seconds, bool)
        or not isinstance(command.timeout_seconds, (int, float))
        or not math.isfinite(command.timeout_seconds)
        or command.timeout_seconds <= 0
        or command.timeout_seconds > 24 * 60 * 60
        or isinstance(command.cpu_time_seconds, bool)
        or not isinstance(command.cpu_time_seconds, (int, float))
        or not math.isfinite(command.cpu_time_seconds)
        or command.cpu_time_seconds <= 0
        or isinstance(command.memory_limit_bytes, bool)
        or not isinstance(command.memory_limit_bytes, int)
        or command.memory_limit_bytes < 32 * 1024 * 1024
        or command.memory_limit_bytes > 4 * 1024 * 1024 * 1024
        or isinstance(command.cpu_rate_percent, bool)
        or not isinstance(command.cpu_rate_percent, int)
        or not 1 <= command.cpu_rate_percent <= 100
        or isinstance(command.output_limit_bytes, bool)
        or not isinstance(command.output_limit_bytes, int)
        or not 1 <= command.output_limit_bytes <= 1024 * 1024
    ):
        return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox command limits or argv are invalid.")
    try:
        root = canonicalize_root_path(command.workspace_root)
    except (OSError, WorkspaceRootError, TypeError, ValueError):
        return _failure(SandboxErrorCode.INVALID_WORKSPACE, "Task workspace is unavailable.")

    relative = canonical_workspace_relative_path(command.cwd)
    if relative is None:
        return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox cwd must be workspace-relative.")
    cwd = root if relative == "." else root.joinpath(*relative.split("/"))
    try:
        if _has_link_component(root, cwd):
            return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox cwd cannot contain links or junctions.")
        resolved_cwd = cwd.resolve(strict=True)
        if not resolved_cwd.is_relative_to(root) or not resolved_cwd.is_dir():
            return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox cwd must stay inside the task workspace.")
    except (OSError, RuntimeError, ValueError):
        return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox cwd is unavailable.")

    executable = Path(command.argv[0])
    if not executable.is_absolute():
        return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox executable must be an absolute trusted path.")
    try:
        resolved_executable = executable.resolve(strict=True)
        if not resolved_executable.is_file():
            raise OSError("not a file")
        system_root = Path(os.environ["SystemRoot"]).resolve(strict=True)
        system32 = system_root / "System32"
        in_workspace = resolved_executable.is_relative_to(root)
        toolchain = None
        if command.python_toolchain_root is not None:
            candidate = Path(command.python_toolchain_root)
            if not candidate.is_absolute() or _is_link_or_junction(candidate):
                return _failure(SandboxErrorCode.INVALID_REQUEST, "Python toolchain root is unsafe.")
            toolchain = candidate.resolve(strict=True)
            if (
                not toolchain.is_dir()
                or toolchain.parent != root.parent
                or not toolchain.name.startswith("toolchain-")
                or resolved_executable != toolchain / "python.exe"
            ):
                return _failure(SandboxErrorCode.INVALID_REQUEST, "Python toolchain is not an owned task sibling.")
        if not (in_workspace or resolved_executable.is_relative_to(system32) or toolchain is not None):
            return _failure(SandboxErrorCode.INVALID_REQUEST, "Executable must be in the task workspace, Windows System32, or the owned Python toolchain.")
        metadata = resolved_executable.stat(follow_symlinks=False)
        if _is_link_or_junction(executable) or not stat.S_ISREG(metadata.st_mode) or ((in_workspace or toolchain is not None) and metadata.st_nlink != 1):
            return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox executable is not a safe regular file.")
    except (OSError, KeyError, RuntimeError, TypeError, ValueError):
        return _failure(SandboxErrorCode.INVALID_REQUEST, "Sandbox executable is unavailable.")

    return _ValidatedCommand(
        workspace_root=root,
        cwd=resolved_cwd,
        argv=(str(resolved_executable), *command.argv[1:]),
        timeout_seconds=float(command.timeout_seconds),
        memory_limit_bytes=command.memory_limit_bytes,
        cpu_rate_percent=command.cpu_rate_percent,
        cpu_time_seconds=float(command.cpu_time_seconds),
        output_limit_bytes=command.output_limit_bytes,
        python_toolchain_root=toolchain,
    )


def _has_link_component(root: Path, target: Path) -> bool:
    current = root
    if _is_link_or_junction(current):
        return True
    for part in target.relative_to(root).parts:
        current = current / part
        if _is_link_or_junction(current):
            return True
    return False


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _failure(code: SandboxErrorCode, message: str) -> SandboxResult:
    status = SandboxStatus.TIMED_OUT if code is SandboxErrorCode.TIMEOUT else SandboxStatus.FAILED
    return SandboxResult(
        status=status,
        error_code=code,
        message=message,
        cleanup_complete=True,
    )
