"""Path, revision, and pure start/resume identity contracts."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from agent.runtime.revision import AgentCodeRevision, AgentRevisionStatus
from agent.workspace import canonical_path_key, canonicalize_root_path

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True, eq=False)
class WorkspaceIdentity:
    """Persisted path value; use from_root to validate a live workspace."""

    canonical_root: str
    root_digest: str

    @classmethod
    def from_root(cls, root: str | Path) -> "WorkspaceIdentity":
        """Validate and canonicalize the current filesystem root."""
        canonical = canonicalize_root_path(root)
        comparison_key = canonical_path_key(canonical)
        digest = hashlib.sha256(comparison_key.encode("utf-8")).hexdigest()
        return cls(canonical_root=str(canonical), root_digest=digest)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.canonical_root, str)
            or not self.canonical_root
            or "\x00" in self.canonical_root
            or not Path(self.canonical_root).is_absolute()
            or canonical_path_key(os.path.normpath(self.canonical_root))
            != canonical_path_key(self.canonical_root)
        ):
            raise ValueError("Workspace identity root is invalid.")
        expected_digest = hashlib.sha256(
            canonical_path_key(self.canonical_root).encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(self.root_digest, str)
            or not _DIGEST_PATTERN.fullmatch(self.root_digest)
            or self.root_digest != expected_digest
        ):
            raise ValueError("Workspace identity is invalid.")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkspaceIdentity):
            return NotImplemented
        return (
            self.root_digest == other.root_digest
            and canonical_path_key(self.canonical_root)
            == canonical_path_key(other.canonical_root)
        )

    def __hash__(self) -> int:
        return hash((self.root_digest, canonical_path_key(self.canonical_root)))


@dataclass(frozen=True, slots=True)
class RunIdentity:
    run_id: str
    thread_id: str
    task_id: str
    workspace: WorkspaceIdentity

    def __post_init__(self) -> None:
        for name in ("run_id", "thread_id", "task_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be a non-empty string.")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.workspace, WorkspaceIdentity):
            raise ValueError("workspace must be WorkspaceIdentity.")


@dataclass(frozen=True, slots=True)
class StartRequest:
    identity: RunIdentity
    run_config_digest: str
    agent_revision: AgentCodeRevision

    def __post_init__(self) -> None:
        _validate_request(self)


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    identity: RunIdentity
    run_config_digest: str
    agent_revision: AgentCodeRevision

    def __post_init__(self) -> None:
        _validate_request(self)


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    """Secret-free identity fields read through a checkpoint lookup seam."""

    identity: RunIdentity
    run_config_digest: str
    agent_revision: AgentCodeRevision

    def __post_init__(self) -> None:
        _validate_request(self)


class CheckpointLookup(Protocol):
    def get(self, thread_id: str) -> RunCheckpoint | None: ...


class PreflightStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class PreflightErrorCode(str, Enum):
    THREAD_ALREADY_EXISTS = "thread_already_exists"
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    WORKSPACE_MISMATCH = "workspace_mismatch"
    THREAD_IDENTITY_MISMATCH = "thread_identity_mismatch"
    RUN_CONFIG_MISMATCH = "run_config_mismatch"
    AGENT_REVISION_MISMATCH = "agent_revision_mismatch"


class PreflightWarningCode(str, Enum):
    AGENT_REVISION_UNVERIFIED = "agent_revision_unverified"


@dataclass(frozen=True, slots=True)
class PreflightWarning:
    code: PreflightWarningCode
    message: str


@dataclass(frozen=True, slots=True)
class PreflightResult:
    status: PreflightStatus
    error_code: PreflightErrorCode | None = None
    message: str = ""
    warnings: tuple[PreflightWarning, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status is PreflightStatus.ACCEPTED


def preflight_start(
    request: StartRequest,
    checkpoint_lookup: CheckpointLookup,
) -> PreflightResult:
    """Accept only a thread that has never had a checkpoint."""
    checkpoint = checkpoint_lookup.get(request.identity.thread_id)
    if checkpoint is not None:
        return _rejected(
            PreflightErrorCode.THREAD_ALREADY_EXISTS,
            "The thread already has a checkpoint.",
        )
    return _accepted(_revision_warnings(request.agent_revision))


def preflight_resume(
    request: ResumeRequest,
    checkpoint_lookup: CheckpointLookup,
) -> PreflightResult:
    """Compare all durable identity fields without executing the graph."""
    checkpoint = checkpoint_lookup.get(request.identity.thread_id)
    if checkpoint is None:
        return _rejected(
            PreflightErrorCode.CHECKPOINT_NOT_FOUND,
            "The thread has no checkpoint to resume.",
        )
    if checkpoint.identity.workspace != request.identity.workspace:
        return _rejected(
            PreflightErrorCode.WORKSPACE_MISMATCH,
            "The checkpoint belongs to a different workspace path.",
        )
    if (
        checkpoint.identity.run_id != request.identity.run_id
        or checkpoint.identity.thread_id != request.identity.thread_id
        or checkpoint.identity.task_id != request.identity.task_id
    ):
        return _rejected(
            PreflightErrorCode.THREAD_IDENTITY_MISMATCH,
            "The checkpoint run, thread, or task identity does not match.",
        )
    if checkpoint.run_config_digest != request.run_config_digest:
        return _rejected(
            PreflightErrorCode.RUN_CONFIG_MISMATCH,
            "The semantic run configuration does not match the checkpoint.",
        )
    if (
        checkpoint.agent_revision.status is AgentRevisionStatus.KNOWN
        and request.agent_revision.status is AgentRevisionStatus.KNOWN
        and checkpoint.agent_revision.commit_sha
        != request.agent_revision.commit_sha
    ):
        return _rejected(
            PreflightErrorCode.AGENT_REVISION_MISMATCH,
            "The Agent code revision does not match the checkpoint.",
        )
    return _accepted(
        _revision_warnings(
            checkpoint.agent_revision,
            request.agent_revision,
        )
    )


def _validate_request(request: object) -> None:
    identity = getattr(request, "identity", None)
    digest = getattr(request, "run_config_digest", None)
    revision = getattr(request, "agent_revision", None)
    if not isinstance(identity, RunIdentity):
        raise ValueError("identity must be RunIdentity.")
    if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
        raise ValueError("run_config_digest must be a SHA-256 hex digest.")
    if not isinstance(revision, AgentCodeRevision):
        raise ValueError("agent_revision must be AgentCodeRevision.")


def _revision_warnings(
    *revisions: AgentCodeRevision,
) -> tuple[PreflightWarning, ...]:
    unknown_reasons = [
        revision.reason
        for revision in revisions
        if revision.status is AgentRevisionStatus.UNKNOWN
    ]
    if not unknown_reasons:
        return ()
    reasons = ", ".join(reason.value for reason in unknown_reasons if reason)
    return (
        PreflightWarning(
            code=PreflightWarningCode.AGENT_REVISION_UNVERIFIED,
            message=f"Agent revision could not be verified: {reasons}.",
        ),
    )


def _accepted(
    warnings: tuple[PreflightWarning, ...],
) -> PreflightResult:
    return PreflightResult(
        status=PreflightStatus.ACCEPTED,
        warnings=warnings,
    )


def _rejected(
    error_code: PreflightErrorCode,
    message: str,
) -> PreflightResult:
    return PreflightResult(
        status=PreflightStatus.REJECTED,
        error_code=error_code,
        message=message,
    )
