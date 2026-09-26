"""Bounded, structured detection of the running Agent code revision."""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from agent.verification.process_tree import ProcessTree

_GIT_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_WORKSPACE_REVISION_STDOUT_MAX_BYTES = 1_048_576
_WORKSPACE_REVISION_STDERR_MAX_BYTES = 65_536
_GIT_PIPE_READ_CHUNK_BYTES = 16_384
_GIT_PROCESS_POLL_SECONDS = 0.05
_GIT_PROCESS_REAP_TIMEOUT_SECONDS = 2.0
_GIT_PIPE_JOIN_TIMEOUT_SECONDS = 2.0


class AgentRevisionStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class AgentRevisionReason(str, Enum):
    NOT_GIT = "not_git"
    DIRTY = "dirty"
    GIT_UNAVAILABLE = "git_unavailable"
    QUERY_FAILED = "query_failed"


class WorkspaceRevisionStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class WorkspaceRevisionReason(str, Enum):
    NOT_GIT = "not_git"
    GIT_UNAVAILABLE = "git_unavailable"
    QUERY_FAILED = "query_failed"


@dataclass(frozen=True, slots=True)
class WorkspaceRevision:
    """Secret-free Git working-tree evidence captured under admission."""

    head: str | None
    dirty: bool | None
    status_digest: str | None
    diff_digest: str | None
    status: WorkspaceRevisionStatus
    reason: WorkspaceRevisionReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, WorkspaceRevisionStatus):
            raise ValueError("Workspace revision status is invalid.")
        if self.status is WorkspaceRevisionStatus.KNOWN:
            if (
                not isinstance(self.head, str)
                or not _GIT_REVISION_PATTERN.fullmatch(self.head.lower())
                or not isinstance(self.dirty, bool)
                or not _DIGEST_PATTERN.fullmatch(self.status_digest or "")
                or not _DIGEST_PATTERN.fullmatch(self.diff_digest or "")
                or self.reason is not None
            ):
                raise ValueError("Known workspace revision is incomplete.")
            object.__setattr__(self, "head", self.head.lower())
            return
        if any(
            value is not None
            for value in (self.head, self.dirty, self.status_digest, self.diff_digest)
        ):
            raise ValueError("Unknown workspace revision must not contain evidence.")
        try:
            reason = WorkspaceRevisionReason(self.reason)
        except (TypeError, ValueError) as error:
            raise ValueError("Unknown workspace revision needs a reason.") from error
        object.__setattr__(self, "reason", reason)

    @classmethod
    def unknown(cls, reason: WorkspaceRevisionReason) -> "WorkspaceRevision":
        return cls(None, None, None, None, WorkspaceRevisionStatus.UNKNOWN, reason)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "WorkspaceRevision":
        """Parse the exact checkpoint-safe revision shape."""
        required = {
            "status",
            "head",
            "dirty",
            "status_digest",
            "diff_digest",
            "reason",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("Workspace revision shape is invalid.")
        try:
            status = WorkspaceRevisionStatus(value["status"])
        except (TypeError, ValueError) as error:
            raise ValueError("Workspace revision status is invalid.") from error
        reason_value = value["reason"]
        reason = None
        if reason_value is not None:
            try:
                reason = WorkspaceRevisionReason(reason_value)
            except (TypeError, ValueError) as error:
                raise ValueError("Workspace revision reason is invalid.") from error
        return cls(
            head=value["head"],
            dirty=value["dirty"],
            status_digest=value["status_digest"],
            diff_digest=value["diff_digest"],
            status=status,
            reason=reason,
        )

    def to_dict(self) -> dict[str, object | None]:
        return {
            "status": self.status.value,
            "head": self.head,
            "dirty": self.dirty,
            "status_digest": self.status_digest,
            "diff_digest": self.diff_digest,
            "reason": self.reason.value if self.reason else None,
        }


_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AgentCodeRevision:
    commit_sha: str | None
    status: AgentRevisionStatus
    reason: AgentRevisionReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AgentRevisionStatus):
            raise ValueError("Agent revision status is invalid.")
        if self.status is AgentRevisionStatus.KNOWN:
            revision = (self.commit_sha or "").lower()
            if not _GIT_REVISION_PATTERN.fullmatch(revision):
                raise ValueError("Known Agent revision requires a Git SHA.")
            if self.reason is not None:
                raise ValueError("Known Agent revision must not have a reason.")
            object.__setattr__(self, "commit_sha", revision)
            return
        if self.commit_sha is not None:
            raise ValueError("Unknown Agent revision must not contain a SHA.")
        try:
            reason = AgentRevisionReason(self.reason)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Unknown Agent revision requires a structured reason."
            ) from error
        object.__setattr__(self, "reason", reason)


def detect_agent_code_revision(
    root: str | Path,
    *,
    git_executable: str = "git",
    timeout_seconds: float = 5.0,
) -> AgentCodeRevision:
    """Return a clean Git revision or a structured unknown reason."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number.")
    command_prefix = [git_executable, "-C", str(Path(root).absolute())]
    try:
        status = _run_git(
            [
                *command_prefix,
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ],
            timeout_seconds,
        )
    except FileNotFoundError:
        return _unknown_revision(AgentRevisionReason.GIT_UNAVAILABLE)
    except (OSError, subprocess.TimeoutExpired):
        return _unknown_revision(AgentRevisionReason.QUERY_FAILED)
    if status.returncode != 0:
        reason = (
            AgentRevisionReason.NOT_GIT
            if "not a git repository" in status.stderr.lower()
            else AgentRevisionReason.QUERY_FAILED
        )
        return _unknown_revision(reason)
    if status.stdout.strip():
        return _unknown_revision(AgentRevisionReason.DIRTY)
    try:
        revision = _run_git(
            [*command_prefix, "rev-parse", "HEAD"], timeout_seconds
        )
    except FileNotFoundError:
        return _unknown_revision(AgentRevisionReason.GIT_UNAVAILABLE)
    except (OSError, subprocess.TimeoutExpired):
        return _unknown_revision(AgentRevisionReason.QUERY_FAILED)
    commit_sha = revision.stdout.strip().lower()
    if revision.returncode != 0 or not _GIT_REVISION_PATTERN.fullmatch(
        commit_sha
    ):
        return _unknown_revision(AgentRevisionReason.QUERY_FAILED)
    return AgentCodeRevision(
        commit_sha=commit_sha,
        status=AgentRevisionStatus.KNOWN,
    )


def _run_git(
    argv: list[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout_seconds,
        shell=False,
    )


def _unknown_revision(reason: AgentRevisionReason) -> AgentCodeRevision:
    return AgentCodeRevision(
        commit_sha=None,
        status=AgentRevisionStatus.UNKNOWN,
        reason=reason,
    )


def detect_workspace_revision(
    root: str | Path,
    *,
    git_executable: str = "git",
    timeout_seconds: float = 5.0,
) -> WorkspaceRevision:
    """Capture Git identity and bounded status/diff digests without raw diff."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number.")
    prefix = [git_executable, "-C", str(Path(root).absolute())]
    try:
        status = _run_git_bounded(
            [*prefix, "status", "--porcelain=v1", "--untracked-files=all"],
            timeout_seconds,
        )
    except FileNotFoundError:
        return WorkspaceRevision.unknown(WorkspaceRevisionReason.GIT_UNAVAILABLE)
    except (OSError, subprocess.TimeoutExpired, _GitOutputLimitExceeded):
        return WorkspaceRevision.unknown(WorkspaceRevisionReason.QUERY_FAILED)
    if status.returncode != 0:
        reason = (
            WorkspaceRevisionReason.NOT_GIT
            if b"not a git repository" in status.stderr.lower()
            else WorkspaceRevisionReason.QUERY_FAILED
        )
        return WorkspaceRevision.unknown(reason)
    try:
        head = _run_git_bounded([*prefix, "rev-parse", "HEAD"], timeout_seconds)
        diff = _run_git_bounded(
            [*prefix, "diff", "--no-ext-diff", "--binary", "HEAD"],
            timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired, _GitOutputLimitExceeded):
        return WorkspaceRevision.unknown(WorkspaceRevisionReason.QUERY_FAILED)
    commit_sha = head.stdout.decode("ascii", errors="ignore").strip().lower()
    if head.returncode != 0 or not _GIT_REVISION_PATTERN.fullmatch(commit_sha):
        return WorkspaceRevision.unknown(WorkspaceRevisionReason.QUERY_FAILED)
    if diff.returncode != 0:
        return WorkspaceRevision.unknown(WorkspaceRevisionReason.QUERY_FAILED)
    return WorkspaceRevision(
        head=commit_sha,
        dirty=bool(status.stdout.strip()),
        status_digest=hashlib.sha256(status.stdout).hexdigest(),
        diff_digest=hashlib.sha256(diff.stdout).hexdigest(),
        status=WorkspaceRevisionStatus.KNOWN,
    )


class _GitOutputLimitExceeded(Exception):
    """A required workspace Git stream exceeded its complete-evidence limit."""


class _BoundedGitPipe:
    def __init__(self, max_bytes: int, overflow: threading.Event) -> None:
        self.max_bytes = max_bytes
        self.overflow = overflow
        self.data = bytearray()
        self.error: BaseException | None = None

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                remaining = self.max_bytes - len(self.data)
                chunk = stream.read(
                    min(_GIT_PIPE_READ_CHUNK_BYTES, remaining + 1)
                )
                if not chunk:
                    return
                accepted = min(len(chunk), remaining)
                if accepted:
                    self.data.extend(chunk[:accepted])
                if len(chunk) > remaining:
                    self.overflow.set()
                    return
        except BaseException as error:
            self.error = error
            self.overflow.set()


def _run_git_bounded(
    argv: list[str], timeout_seconds: float
) -> subprocess.CompletedProcess[bytes]:
    """Read both Git pipes incrementally and reject incomplete evidence."""
    # Import lazily to keep module initialization independent of verification.
    from agent.verification.process_tree import ProcessTree

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        bufsize=0,
        start_new_session=os.name == "posix",
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        ),
    )
    process_tree = ProcessTree(process)
    if process.stdout is None or process.stderr is None:
        _terminate_and_reap_git(process, process_tree)
        process_tree.close()
        raise OSError("Git output pipes could not be opened.")

    overflow = threading.Event()
    stdout_capture = _BoundedGitPipe(
        _WORKSPACE_REVISION_STDOUT_MAX_BYTES, overflow
    )
    stderr_capture = _BoundedGitPipe(
        _WORKSPACE_REVISION_STDERR_MAX_BYTES, overflow
    )
    readers = [
        threading.Thread(
            target=stdout_capture.drain,
            args=(process.stdout,),
            daemon=True,
        ),
        threading.Thread(
            target=stderr_capture.drain,
            args=(process.stderr,),
            daemon=True,
        ),
    ]
    started_readers: list[threading.Thread] = []
    deadline = time.monotonic() + timeout_seconds
    terminated = False
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        while True:
            if overflow.is_set():
                _terminate_and_reap_git(process, process_tree)
                terminated = True
                _join_git_readers(started_readers)
                _raise_git_pipe_error(stdout_capture, stderr_capture)
                raise _GitOutputLimitExceeded
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_and_reap_git(process, process_tree)
                terminated = True
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            try:
                return_code = process.wait(
                    timeout=min(remaining, _GIT_PROCESS_POLL_SECONDS)
                )
                break
            except subprocess.TimeoutExpired:
                continue

        _join_git_readers(started_readers)
        _raise_git_pipe_error(stdout_capture, stderr_capture)
        if overflow.is_set():
            raise _GitOutputLimitExceeded
        return subprocess.CompletedProcess(
            argv,
            return_code,
            bytes(stdout_capture.data),
            bytes(stderr_capture.data),
        )
    except BaseException:
        if not terminated:
            _terminate_and_reap_git(process, process_tree)
        _join_git_readers(started_readers)
        raise
    finally:
        try:
            if all(not reader.is_alive() for reader in started_readers):
                process.stdout.close()
                process.stderr.close()
        finally:
            process_tree.close()


def _terminate_and_reap_git(
    process: subprocess.Popen[bytes], process_tree: ProcessTree
) -> None:
    """Kill and reap the owned Git process using finite waits only."""
    process_tree.terminate()
    try:
        process.wait(timeout=_GIT_PROCESS_REAP_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
    process.wait(timeout=_GIT_PROCESS_REAP_TIMEOUT_SECONDS)


def _join_git_readers(readers: list[threading.Thread]) -> None:
    deadline = time.monotonic() + _GIT_PIPE_JOIN_TIMEOUT_SECONDS
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        raise OSError("Git output pipes did not close after process exit.")


def _raise_git_pipe_error(
    stdout_capture: _BoundedGitPipe,
    stderr_capture: _BoundedGitPipe,
) -> None:
    for capture in (stdout_capture, stderr_capture):
        if capture.error is not None:
            raise capture.error
