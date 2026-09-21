"""Versioned run record persistence."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent.runtime.secrets import KnownSecretFilter
from agent.runtime.revision import WorkspaceRevision
from agent.runtime.trajectory_contracts import (
    RECORD_SCHEMA_VERSION,
    RECORDS_DIRECTORY,
    AuditStatus,
    EventSinkErrorCode,
    _bounded_json_value,
    _canonical_digest,
    _json_value,
    _require_text,
)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Small result manifest; it is not a recovery or run manager."""

    schema_version: int
    run_id: str
    thread_id: str
    task_id: str
    workspace_root: str
    workspace_root_digest: str
    run_config_digest: str
    runtime_status: str
    workflow_outcome: str | None
    verification_status: str | None
    error_code: str | None
    budget: Mapping[str, Any] | None
    started_at: float
    finished_at: float
    last_event_sequence: int | None
    audit_status: AuditStatus
    agent_revision: Mapping[str, Any]
    workspace_revision_start: Mapping[str, Any] | str = "UNKNOWN"
    workspace_revision_end: Mapping[str, Any] | str = "UNKNOWN"
    audit_error_code: str | None = None
    record_ref: str | None = None

    def __post_init__(self) -> None:
        input_schema_version = self.schema_version
        if (
            isinstance(input_schema_version, bool)
            or not isinstance(input_schema_version, int)
            or input_schema_version not in {1, RECORD_SCHEMA_VERSION}
        ):
            raise ValueError("Unsupported run record schema version.")
        # Keep schema-1 values readable/writable through the legacy seam. New
        # durable records are constructed with the current schema explicitly.
        if input_schema_version == RECORD_SCHEMA_VERSION:
            object.__setattr__(self, "schema_version", RECORD_SCHEMA_VERSION)
        for name in (
            "run_id",
            "thread_id",
            "task_id",
            "workspace_root",
            "workspace_root_digest",
            "run_config_digest",
            "runtime_status",
        ):
            _require_text(getattr(self, name), name)
        if self.workflow_outcome is not None:
            _require_text(self.workflow_outcome, "workflow_outcome")
        if self.verification_status is not None:
            _require_text(self.verification_status, "verification_status")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")
        if not isinstance(self.audit_status, AuditStatus):
            try:
                object.__setattr__(self, "audit_status", AuditStatus(self.audit_status))
            except (TypeError, ValueError) as error:
                raise ValueError("audit_status is invalid.") from error
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            try:
                finite = math.isfinite(value)
            except (OverflowError, TypeError):
                finite = False
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not finite
            ):
                raise ValueError(f"{name} must be finite.")
        if self.last_event_sequence is not None and (
            isinstance(self.last_event_sequence, bool)
            or not isinstance(self.last_event_sequence, int)
            or self.last_event_sequence < 0
        ):
            raise ValueError("last_event_sequence must be a non-negative integer or None.")
        if not isinstance(self.agent_revision, Mapping):
            raise ValueError("agent_revision must be a mapping.")
        for name in ("workspace_revision_start", "workspace_revision_end"):
            value = getattr(self, name)
            if input_schema_version == RECORD_SCHEMA_VERSION:
                if not isinstance(value, Mapping):
                    raise ValueError(
                        f"{name} must contain a complete workspace revision."
                    )
                revision = WorkspaceRevision.from_dict(value)
                object.__setattr__(self, name, revision.to_dict())
            elif isinstance(value, str):
                if value != "UNKNOWN":
                    raise ValueError(f"{name} has an invalid legacy value.")
                _require_text(value, name)
            elif isinstance(value, Mapping):
                object.__setattr__(self, name, _bounded_json_value(value))
            else:
                raise ValueError(f"{name} must be a mapping or UNKNOWN string.")
        if self.record_ref is not None:
            _require_text(self.record_ref, "record_ref")
        object.__setattr__(self, "budget", _bounded_json_value(self.budget))
        object.__setattr__(
            self,
            "agent_revision",
            _bounded_json_value(self.agent_revision),
        )

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "task_id": self.task_id,
            "workspace": {
                "canonical_root": self.workspace_root,
                "root_digest": self.workspace_root_digest,
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunRecord":
        if not isinstance(value, Mapping):
            raise ValueError("Run record must be a mapping.")
        schema_version = value.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {1, RECORD_SCHEMA_VERSION}
        ):
            raise ValueError("Run record schema version is invalid.")
        required_legacy = {
            "schema_version",
            "identity",
            "run_config_digest",
            "runtime_status",
            "started_at",
            "finished_at",
            "audit_status",
            "agent_revision",
        }
        allowed = {
            *required_legacy,
            "workflow_outcome",
            "verification_status",
            "error_code",
            "budget",
            "last_event_sequence",
            "audit_error_code",
            "workspace_revision_start",
            "workspace_revision_end",
            "record_ref",
        }
        if set(value) - allowed:
            raise ValueError("Run record contains unknown fields.")
        missing = required_legacy - set(value)
        if missing:
            raise ValueError("Run record required fields are missing.")
        if schema_version == RECORD_SCHEMA_VERSION:
            missing = allowed - set(value)
            if missing:
                raise ValueError("Run record v2 fields are missing.")
        identity = value.get("identity")
        if not isinstance(identity, Mapping):
            raise ValueError("Run record identity is invalid.")
        if set(identity) != {"run_id", "thread_id", "task_id", "workspace"}:
            raise ValueError("Run record identity contains unknown fields.")
        workspace = identity.get("workspace")
        if not isinstance(workspace, Mapping):
            raise ValueError("Run record workspace identity is invalid.")
        if set(workspace) != {"canonical_root", "root_digest"}:
            raise ValueError("Run record workspace identity contains unknown fields.")
        return cls(
            schema_version=schema_version,
            run_id=identity["run_id"],
            thread_id=identity["thread_id"],
            task_id=identity["task_id"],
            workspace_root=workspace["canonical_root"],
            workspace_root_digest=workspace["root_digest"],
            run_config_digest=value["run_config_digest"],
            runtime_status=value["runtime_status"],
            workflow_outcome=value.get("workflow_outcome"),
            verification_status=value.get("verification_status"),
            error_code=value.get("error_code"),
            budget=value.get("budget"),
            started_at=value["started_at"],
            finished_at=value["finished_at"],
            last_event_sequence=value.get("last_event_sequence"),
            audit_status=value["audit_status"],
            agent_revision=value["agent_revision"],
            workspace_revision_start=value.get(
                "workspace_revision_start", "UNKNOWN"
            ),
            workspace_revision_end=value.get("workspace_revision_end", "UNKNOWN"),
            audit_error_code=value.get("audit_error_code"),
            record_ref=value.get("record_ref"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "run_config_digest": self.run_config_digest,
            "runtime_status": self.runtime_status,
            "workflow_outcome": self.workflow_outcome,
            "verification_status": self.verification_status,
            "error_code": self.error_code,
            "budget": _json_value(self.budget),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_event_sequence": self.last_event_sequence,
            "audit_status": self.audit_status.value,
            "audit_error_code": self.audit_error_code,
            "agent_revision": _json_value(self.agent_revision),
            "workspace_revision_start": _json_value(self.workspace_revision_start),
            "workspace_revision_end": _json_value(self.workspace_revision_end),
            "record_ref": self.record_ref,
        }


@dataclass(frozen=True, slots=True)
class RecordWriteResult:
    success: bool
    record_ref: str | None = None
    path: Path | None = None
    error_code: str | None = None
    message: str = ""


def record_ref_for(*, run_id: str, thread_id: str, task_id: str) -> str:
    digest = _canonical_digest(
        {"run_id": run_id, "thread_id": thread_id, "task_id": task_id}
    )
    return f"{RECORDS_DIRECTORY}/{digest}.json"


def write_run_record(
    runtime_root: str | Path,
    record: RunRecord,
    *,
    secret_filter: KnownSecretFilter | None = None,
) -> RecordWriteResult:
    """Atomically publish a record under a hash-only, workspace-external name."""
    reference = record_ref_for(
        run_id=record.run_id,
        thread_id=record.thread_id,
        task_id=record.task_id,
    )
    root = Path(runtime_root)
    directory = root / RECORDS_DIRECTORY
    path = root / reference
    temporary = directory / f".{path.stem}.tmp"
    try:
        published = replace(record, record_ref=reference)
        if secret_filter is not None:
            safe = secret_filter.sanitize(published.to_dict()).value
        else:
            safe = published.to_dict()
        if path.exists():
            existing = path.read_bytes()
            if not existing.endswith(b"\n"):
                return RecordWriteResult(
                    success=False,
                    record_ref=reference,
                    path=path,
                    error_code=EventSinkErrorCode.CORRUPT_RECORD.value,
                    message="The existing run record has an incomplete final line.",
                )
            try:
                decoded = json.loads(existing.decode("utf-8"))
                if secret_filter is not None:
                    sanitized = secret_filter.sanitize(decoded).value
                    if sanitized != decoded:
                        return RecordWriteResult(
                            success=False,
                            record_ref=reference,
                            path=path,
                            error_code=EventSinkErrorCode.SENSITIVE_DATA_DETECTED.value,
                            message="The existing run record contains a configured secret.",
                        )
                RunRecord.from_dict(decoded)
            except (
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ):
                return RecordWriteResult(
                    success=False,
                    record_ref=reference,
                    path=path,
                    error_code=EventSinkErrorCode.CORRUPT_RECORD.value,
                    message="The existing run record is invalid.",
                )
        directory.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, UnicodeError, TypeError, ValueError):
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        return RecordWriteResult(
            success=False,
            record_ref=reference,
            path=path,
            error_code=EventSinkErrorCode.IO_ERROR.value,
            message="Unable to publish the run record.",
        )
    return RecordWriteResult(success=True, record_ref=reference, path=path)
