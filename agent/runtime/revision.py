"""Bounded, structured detection of the running Agent code revision."""

from __future__ import annotations

import math
import hashlib
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_GIT_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


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
        status = _run_git_bytes(
            [*prefix, "status", "--porcelain=v1", "--untracked-files=all"],
            timeout_seconds,
        )
    except FileNotFoundError:
        return WorkspaceRevision.unknown(WorkspaceRevisionReason.GIT_UNAVAILABLE)
    except (OSError, subprocess.TimeoutExpired):
        return WorkspaceRevision.unknown(WorkspaceRevisionReason.QUERY_FAILED)
    if status.returncode != 0:
        reason = (
            WorkspaceRevisionReason.NOT_GIT
            if b"not a git repository" in status.stderr.lower()
            else WorkspaceRevisionReason.QUERY_FAILED
        )
        return WorkspaceRevision.unknown(reason)
    try:
        head = _run_git_bytes([*prefix, "rev-parse", "HEAD"], timeout_seconds)
        diff = _run_git_bytes(
            [*prefix, "diff", "--no-ext-diff", "--binary", "HEAD"],
            timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
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


def _run_git_bytes(
    argv: list[str], timeout_seconds: float
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout_seconds,
        shell=False,
    )
