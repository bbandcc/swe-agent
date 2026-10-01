"""Prepare dependencies from a task-pinned uv.lock for the existing sandbox."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from agent.evaluation.python_toolchain import (
    PythonToolchainError,
    stage_python_312,
    validate_python_312,
)
from agent.workspace.paths import canonical_workspace_relative_path


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_LOCK_BYTES = 4 * 1024 * 1024
_UV_TIMEOUT_SECONDS = 20 * 60
_EXPECTED_UV_VERSION = "0.12.1"


class FrozenEnvironmentError(PythonToolchainError):
    """The task's pinned Python environment could not be prepared safely."""


class FrozenEnvironmentCleanupError(FrozenEnvironmentError):
    """A temporary frozen environment could not be removed completely."""


def stage_frozen_python_312(
    *,
    task: Mapping[str, Any],
    workspace_root: str | Path,
    workspace_revision: str,
    repository_root: str | Path,
    runtime_root: str | Path,
    python_toolchain_root: str | Path,
    uv_executable: str | Path,
    destination: Path,
) -> Path:
    """Verify the snapshot lock, install locked wheels, then stage only packages.

    The temporary uv environment and cache are removed before this function
    returns, so the sandbox is granted access only to the staged toolchain.
    """
    try:
        workspace = Path(workspace_root).resolve(strict=True)
        repository = Path(repository_root).resolve(strict=True)
        runtime = Path(runtime_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise FrozenEnvironmentError("Frozen task paths are unavailable.") from error
    target = task.get("target") if isinstance(task, Mapping) else None
    environment = task.get("environment") if isinstance(task, Mapping) else None
    revision = target.get("revision") if isinstance(target, Mapping) else None
    expected_fields = {
        "os_family", "architecture", "python_version", "lock_path", "lock_sha256"
    }
    if (
        not isinstance(revision, str)
        or revision != workspace_revision
        or not isinstance(environment, Mapping)
        or set(environment) != expected_fields
        or environment.get("os_family") != platform.system()
        or environment.get("architecture") != platform.machine()
        or environment.get("python_version") != "3.12"
    ):
        raise FrozenEnvironmentError("Task runtime identity does not match the frozen environment.")

    lock_spelling = environment.get("lock_path")
    if not isinstance(lock_spelling, str):
        raise FrozenEnvironmentError("Task lock path is invalid.")
    lock_relative = canonical_workspace_relative_path(lock_spelling, allow_root=False)
    if lock_relative is None or lock_relative != lock_spelling:
        raise FrozenEnvironmentError("Task lock path is not canonical and workspace-relative.")
    expected_hash = environment.get("lock_sha256")
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        raise FrozenEnvironmentError("Task lock identity is invalid.")

    lock_path = workspace.joinpath(*PurePosixPath(lock_relative).parts)
    try:
        _ensure_no_links(workspace, lock_path)
        lock_metadata = lock_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_nlink != 1
            or lock_metadata.st_size > _MAX_LOCK_BYTES
        ):
            raise FrozenEnvironmentError("Pinned task lock is not a safe bounded regular file.")
        actual_hash = _hash_file(lock_path)
    except FrozenEnvironmentError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise FrozenEnvironmentError("Pinned task lock is unavailable.") from error
    if actual_hash != expected_hash:
        raise FrozenEnvironmentError("Pinned task lock hash does not match the manifest.")

    uv_path = _trusted_external_file(
        uv_executable,
        blocked_roots=(repository, runtime, workspace),
        description="uv executable",
    )
    _validate_uv_0121(uv_path)
    source_python = Path(python_toolchain_root)
    if not source_python.is_absolute() or _is_link_or_junction(source_python):
        raise FrozenEnvironmentError("Trusted Python toolchain root is invalid.")
    try:
        python_root = source_python.resolve(strict=True)
        if not python_root.is_dir():
            raise FrozenEnvironmentError("Trusted Python toolchain root is unavailable.")
    except (OSError, RuntimeError, ValueError) as error:
        raise FrozenEnvironmentError("Trusted Python toolchain root is unavailable.") from error
    if _overlaps_any(python_root, (repository, runtime, workspace)):
        raise FrozenEnvironmentError("Trusted Python must be outside task-owned storage.")
    try:
        python_executable = validate_python_312(
            source_python, expected_architecture=environment["architecture"]
        )
    except PythonToolchainError as error:
        raise FrozenEnvironmentError("Trusted Python runtime identity is invalid.") from error

    temporary_path: Path | None = None
    temporary_root: Path | None = None
    executable: Path | None = None
    operation_error: BaseException | None = None
    try:
        temporary_path = Path(tempfile.mkdtemp(prefix="s5b-frozen-python-"))
        temporary_root = temporary_path.resolve(strict=True)
        if _overlaps_any(temporary_root, (repository, runtime, workspace)):
            raise FrozenEnvironmentError("Temporary dependency environment overlaps task storage.")
        environment_root = temporary_root / "environment"
        cache_root = temporary_root / "cache"
        profile_root = temporary_root / "profile"
        cache_root.mkdir()
        profile_root.mkdir()
        child_environment = _uv_environment(
            temporary_root=temporary_root,
            cache_root=cache_root,
            environment_root=environment_root,
            profile_root=profile_root,
        )
        command = [
            str(uv_path),
            "--no-config",
            "sync",
            "--locked",
            "--no-build",
            "--no-install-project",
            "--no-dev",
            "--link-mode",
            "copy",
            "--no-python-downloads",
            "--keyring-provider",
            "disabled",
            "--python",
            str(python_executable),
            "--project",
            str(workspace),
        ]
        result = _run_uv_sync(command, workspace, child_environment)
        if result.returncode != 0:
            raise FrozenEnvironmentError(
                "Pinned dependencies require an unavailable binary wheel or failed installation."
            )
        if _hash_file(lock_path) != expected_hash:
            raise FrozenEnvironmentError("uv changed the pinned task lock unexpectedly.")
        site_packages = environment_root / "Lib" / "site-packages"
        if _is_link_or_junction(site_packages) or not site_packages.is_dir():
            raise FrozenEnvironmentError("uv did not create a safe Windows dependency tree.")
        executable = stage_python_312(
            source_python,
            destination,
            dependency_site_packages=site_packages,
        )
    except BaseException as error:
        operation_error = error

    cleanup_error: OSError | None = None
    if temporary_path is not None:
        try:
            shutil.rmtree(temporary_path)
        except OSError as error:
            cleanup_error = error
        try:
            if temporary_path.exists():
                cleanup_error = cleanup_error or OSError(
                    "Temporary frozen environment still exists after cleanup."
                )
        except OSError as error:
            cleanup_error = cleanup_error or error
    if cleanup_error is not None:
        raise FrozenEnvironmentCleanupError(
            "Temporary frozen environment cleanup failed."
        ) from cleanup_error
    if operation_error is not None:
        if isinstance(operation_error, (KeyboardInterrupt, SystemExit)):
            raise operation_error
        if isinstance(operation_error, FrozenEnvironmentError):
            raise operation_error
        if isinstance(
            operation_error,
            (OSError, RuntimeError, subprocess.SubprocessError, PythonToolchainError),
        ):
            raise FrozenEnvironmentError(
                "Frozen Python environment preparation failed safely."
            ) from operation_error
        raise operation_error
    if executable is None:
        raise FrozenEnvironmentError("Frozen Python environment preparation was incomplete.")
    return executable


def _validate_uv_0121(uv_path: Path) -> None:
    version = _read_uv_version(uv_path).split()
    if len(version) < 2 or version[:2] != ["uv", _EXPECTED_UV_VERSION]:
        raise FrozenEnvironmentError("Frozen dependency preparation requires uv 0.12.1.")


def _read_uv_version(uv_path: Path) -> str:
    try:
        result = subprocess.run(
            [str(uv_path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise FrozenEnvironmentError("Trusted uv executable could not be queried.") from error
    if result.returncode != 0:
        raise FrozenEnvironmentError("Trusted uv executable failed its version check.")
    return result.stdout.decode("ascii", errors="replace")


def _trusted_external_file(
    value: str | Path,
    *,
    blocked_roots: tuple[Path, ...],
    description: str,
) -> Path:
    path = Path(value)
    if not path.is_absolute() or _is_link_or_junction(path):
        raise FrozenEnvironmentError(f"Trusted {description} path is invalid.")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FrozenEnvironmentError(f"Trusted {description} must be a unique regular file.")
        if _overlaps_any(resolved, blocked_roots):
            raise FrozenEnvironmentError(f"Trusted {description} must be outside task-owned storage.")
        return resolved
    except (OSError, RuntimeError, ValueError) as error:
        raise FrozenEnvironmentError(f"Trusted {description} is unavailable.") from error


def _run_uv_sync(
    command: list[str], workspace: Path, child_environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        cwd=workspace,
        env=child_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_UV_TIMEOUT_SECONDS,
        check=False,
        shell=False,
    )


def _uv_environment(
    *,
    temporary_root: Path,
    cache_root: Path,
    environment_root: Path,
    profile_root: Path,
) -> dict[str, str]:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise FrozenEnvironmentError("Windows system runtime is unavailable for frozen dependencies.")
    system32 = str(Path(system_root) / "System32")
    appdata = profile_root / "Roaming"
    local_appdata = profile_root / "Local"
    appdata.mkdir()
    local_appdata.mkdir()
    return {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "PATH": system32,
        "TEMP": str(temporary_root),
        "TMP": str(temporary_root),
        "USERPROFILE": str(profile_root),
        "HOME": str(profile_root),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(local_appdata),
        "UV_NO_CONFIG": "1",
        "UV_NO_MANAGED_PYTHON": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_SYSTEM_CERTS": "1",
        "UV_KEYRING_PROVIDER": "disabled",
        "UV_PROJECT_ENVIRONMENT": str(environment_root),
        "UV_CACHE_DIR": str(cache_root),
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            total += len(chunk)
            if total > _MAX_LOCK_BYTES:
                raise FrozenEnvironmentError("Pinned task lock exceeds its size limit.")
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_no_links(root: Path, target: Path) -> None:
    if _is_link_or_junction(root):
        raise FrozenEnvironmentError("Task workspace contains an unsafe link.")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if _is_link_or_junction(current):
            raise FrozenEnvironmentError("Pinned task lock path contains a link or junction.")


def _overlaps_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) or root.is_relative_to(path) for root in roots)


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True
