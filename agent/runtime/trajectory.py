"""Small, versioned, local audit contracts for durable runs.

Events are an audit trail only.  They are never used as checkpoint or replay
state.  The implementation intentionally stays single-process; callers that
need cross-process append coordination must add that boundary separately.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from agent.runtime.secrets import KnownSecretFilter

EVENT_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
EVENTS_FILENAME = "events.jsonl"
RECORDS_DIRECTORY = "records"
_MAX_SUMMARY_TEXT = 512


class AppendStatus(str, Enum):
    APPENDED = "appended"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    ERROR = "error"


# A descriptive alias keeps the public contract discoverable for callers that
# prefer the result-oriented name.
AppendResultStatus = AppendStatus


class AuditStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class EventSinkErrorCode(str, Enum):
    INVALID_EVENT = "invalid_event"
    TRUNCATED_TAIL = "truncated_tail"
    CORRUPT_LOG = "corrupt_log"
    CORRUPT_RECORD = "corrupt_record"
    CONFLICTING_KEY = "conflicting_key"
    IO_ERROR = "io_error"
    SENSITIVE_DATA_DETECTED = "sensitive_data_detected"


class EventSinkError(RuntimeError):
    """Structured startup/read failure for a local event sink."""

    def __init__(
        self,
        code: EventSinkErrorCode,
        message: str,
        *,
        line_number: int | None = None,
    ) -> None:
        self.code = code
        self.line_number = line_number
        super().__init__(message)


class AuditIncompleteError(RuntimeError):
    """Raised when an audit event cannot be durably appended."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AppendResult:
    status: AppendStatus
    idempotency_key: str
    sequence: int | None = None
    error_code: str | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {AppendStatus.APPENDED, AppendStatus.DUPLICATE}


class EventSink(Protocol):
    """Stable append-only audit seam."""

    def append_once(self, event: "RunEvent") -> AppendResult: ...


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One bounded, versioned audit fact.

    ``sequence`` is assigned by the sink and deliberately excluded from the
    semantic idempotency key.  Event payloads are summaries, not raw model,
    tool, source, or verification content.
    """

    schema_version: int = EVENT_SCHEMA_VERSION
    run_id: str = ""
    thread_id: str = ""
    task_id: str = ""
    subject_id: str = ""
    event_type: str = ""
    phase: str = ""
    status: str = ""
    timestamp: float = field(default_factory=time.time)
    step_id: str | None = None
    call_id: str | None = None
    model: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] | None = None
    summary: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    artifact_refs: tuple[str, ...] = ()
    idempotency_key: str = ""
    event_id: str = ""
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError("Unsupported run event schema version.")
        for name in (
            "run_id",
            "thread_id",
            "task_id",
            "subject_id",
            "event_type",
            "phase",
            "status",
        ):
            _require_text(getattr(self, name), name)
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, (int, float))
            or not math.isfinite(self.timestamp)
        ):
            raise ValueError("timestamp must be finite.")
        for name in ("step_id", "call_id"):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        if self.sequence is not None and (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
        ):
            raise ValueError("sequence must be a positive integer or None.")
        object.__setattr__(
            self,
            "artifact_refs",
            _string_tuple(self.artifact_refs, "artifact_refs"),
        )
        for name in ("model", "usage", "summary", "error"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a mapping or None.")
            if value is not None:
                object.__setattr__(
                    self, name, _bounded_json_value(value)
                )
        expected_key = make_idempotency_key(
            schema_version=self.schema_version,
            run_id=self.run_id,
            subject_id=self.subject_id,
            event_type=self.event_type,
            phase=self.phase,
        )
        if self.idempotency_key and self.idempotency_key != expected_key:
            raise ValueError("idempotency_key does not match stable event identity.")
        object.__setattr__(self, "idempotency_key", expected_key)
        expected_event_id = hashlib.sha256(
            f"event:{expected_key}".encode("ascii")
        ).hexdigest()
        if self.event_id and self.event_id != expected_event_id:
            raise ValueError("event_id does not match stable event identity.")
        object.__setattr__(self, "event_id", expected_event_id)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        thread_id: str,
        task_id: str,
        subject_id: str,
        event_type: str,
        phase: str,
        status: str,
        timestamp: float | None = None,
        step_id: str | None = None,
        call_id: str | None = None,
        model: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        summary: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        artifact_refs: Sequence[str] = (),
    ) -> "RunEvent":
        return cls(
            run_id=run_id,
            thread_id=thread_id,
            task_id=task_id,
            subject_id=subject_id,
            event_type=event_type,
            phase=phase,
            status=status,
            timestamp=time.time() if timestamp is None else timestamp,
            step_id=step_id,
            call_id=call_id,
            model=model,
            usage=usage,
            summary=summary,
            error=error,
            artifact_refs=tuple(artifact_refs),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunEvent":
        if not isinstance(value, Mapping):
            raise ValueError("Run event must be a mapping.")
        allowed = {item.name for item in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("Run event contains unknown fields.")
        return cls(**dict(value))

    def to_dict(self, *, include_sequence: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "task_id": self.task_id,
            "subject_id": self.subject_id,
            "event_type": self.event_type,
            "phase": self.phase,
            "status": self.status,
            "timestamp": self.timestamp,
            "step_id": self.step_id,
            "call_id": self.call_id,
            "model": _json_value(self.model),
            "usage": _json_value(self.usage),
            "summary": _json_value(self.summary),
            "error": _json_value(self.error),
            "artifact_refs": list(self.artifact_refs),
            "idempotency_key": self.idempotency_key,
            "event_id": self.event_id,
        }
        if include_sequence:
            result["sequence"] = self.sequence
        return result

    def fingerprint(self) -> str:
        """Return the conflict detector value, ignoring time and sink order."""
        value = self.to_dict(include_sequence=False)
        value.pop("timestamp", None)
        value.pop("event_id", None)
        value.pop("idempotency_key", None)
        return _canonical_digest(value)


def make_idempotency_key(
    *,
    schema_version: int,
    run_id: str,
    subject_id: str,
    event_type: str,
    phase: str,
) -> str:
    """Derive semantic identity; sequence is intentionally absent."""
    return _canonical_digest(
        {
            "schema_version": schema_version,
            "run_id": run_id,
            "subject_id": subject_id,
            "event_type": event_type,
            "phase": phase,
        }
    )


class JsonlEventSink:
    """Deterministic single-process JSONL sink rooted outside the workspace."""

    def __init__(
        self,
        runtime_root: str | Path,
        *,
        filename: str = EVENTS_FILENAME,
        secret_filter: KnownSecretFilter | None = None,
    ) -> None:
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or filename in {".", ".."}
        ):
            raise ValueError("Event filename must be a simple file name.")
        self.runtime_root = Path(runtime_root)
        self.path = self.runtime_root / filename
        self._secret_filter = secret_filter
        self._lock = threading.Lock()
        self._events: dict[str, RunEvent] = {}
        self._last_sequence = 0
        try:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            self._load_existing()
        except EventSinkError:
            raise
        except OSError as error:
            raise EventSinkError(
                EventSinkErrorCode.IO_ERROR,
                "Unable to open the event sink.",
            ) from error

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def events(self) -> tuple[RunEvent, ...]:
        return tuple(
            sorted(self._events.values(), key=lambda event: event.sequence or 0)
        )

    def append_once(self, event: RunEvent) -> AppendResult:
        if not isinstance(event, RunEvent):
            return AppendResult(
                AppendStatus.ERROR,
                "",
                error_code=EventSinkErrorCode.INVALID_EVENT.value,
                message="Event must be a RunEvent.",
            )
        try:
            event = self._sanitize_event(event)
        except (TypeError, ValueError) as error:
            return AppendResult(
                AppendStatus.ERROR,
                event.idempotency_key,
                error_code=EventSinkErrorCode.SENSITIVE_DATA_DETECTED.value,
                message=str(error),
            )
        with self._lock:
            previous = self._events.get(event.idempotency_key)
            if previous is not None:
                if previous.fingerprint() == event.fingerprint():
                    return AppendResult(
                        AppendStatus.DUPLICATE,
                        event.idempotency_key,
                        sequence=previous.sequence,
                    )
                return AppendResult(
                    AppendStatus.CONFLICT,
                    event.idempotency_key,
                    sequence=previous.sequence,
                    error_code=EventSinkErrorCode.CONFLICTING_KEY.value,
                    message="The event key already represents different content.",
                )
            sequence = self._last_sequence + 1
            stored = replace(event, sequence=sequence)
            line = (
                json.dumps(
                    stored.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            try:
                with self.path.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, UnicodeError) as error:
                return AppendResult(
                    AppendStatus.ERROR,
                    event.idempotency_key,
                    error_code=EventSinkErrorCode.IO_ERROR.value,
                    message="Unable to append the audit event.",
                )
            self._events[stored.idempotency_key] = stored
            self._last_sequence = sequence
            return AppendResult(
                AppendStatus.APPENDED,
                stored.idempotency_key,
                sequence=sequence,
            )

    def _sanitize_event(self, event: RunEvent) -> RunEvent:
        if self._secret_filter is None:
            return event
        sanitized = self._secret_filter.sanitize(event.to_dict()).value
        return RunEvent.from_dict(sanitized)

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_bytes()
        except OSError as error:
            raise EventSinkError(
                EventSinkErrorCode.IO_ERROR,
                "Unable to read the event sink.",
            ) from error
        if not raw:
            return
        if not raw.endswith(b"\n"):
            raise EventSinkError(
                EventSinkErrorCode.TRUNCATED_TAIL,
                "The event log has an incomplete final line.",
            )
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                raise EventSinkError(
                    EventSinkErrorCode.CORRUPT_LOG,
                    "The event log contains an empty record.",
                    line_number=line_number,
                )
            try:
                decoded = json.loads(line.decode("utf-8"))
                if self._secret_filter is not None:
                    sanitized = self._secret_filter.sanitize(decoded).value
                    if sanitized != decoded:
                        raise EventSinkError(
                            EventSinkErrorCode.SENSITIVE_DATA_DETECTED,
                            "The event log contains a configured secret.",
                            line_number=line_number,
                        )
                event = RunEvent.from_dict(decoded)
            except EventSinkError:
                raise
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                raise EventSinkError(
                    EventSinkErrorCode.CORRUPT_LOG,
                    "The event log contains an invalid record.",
                    line_number=line_number,
                ) from error
            if event.sequence is None or event.sequence != self._last_sequence + 1:
                raise EventSinkError(
                    EventSinkErrorCode.CORRUPT_LOG,
                    "Event sequence is not contiguous.",
                    line_number=line_number,
                )
            previous = self._events.get(event.idempotency_key)
            if previous is not None and previous.fingerprint() != event.fingerprint():
                raise EventSinkError(
                    EventSinkErrorCode.CONFLICTING_KEY,
                    "The event log contains conflicting idempotency keys.",
                    line_number=line_number,
                )
            self._events[event.idempotency_key] = event
            self._last_sequence = event.sequence


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
    workspace_revision_start: str = "UNKNOWN"
    workspace_revision_end: str = "UNKNOWN"
    audit_error_code: str | None = None
    record_ref: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != RECORD_SCHEMA_VERSION:
            raise ValueError("Unsupported run record schema version.")
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
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
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
            _require_text(getattr(self, name), name)
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
        allowed = {
            "schema_version",
            "identity",
            "run_config_digest",
            "runtime_status",
            "workflow_outcome",
            "verification_status",
            "error_code",
            "budget",
            "started_at",
            "finished_at",
            "last_event_sequence",
            "audit_status",
            "audit_error_code",
            "agent_revision",
            "workspace_revision_start",
            "workspace_revision_end",
            "record_ref",
        }
        if set(value) - allowed:
            raise ValueError("Run record contains unknown fields.")
        identity = value.get("identity")
        if not isinstance(identity, Mapping):
            raise ValueError("Run record identity is invalid.")
        if set(identity) - {"run_id", "thread_id", "task_id", "workspace"}:
            raise ValueError("Run record identity contains unknown fields.")
        workspace = identity.get("workspace")
        if not isinstance(workspace, Mapping):
            raise ValueError("Run record workspace identity is invalid.")
        if set(workspace) - {"canonical_root", "root_digest"}:
            raise ValueError("Run record workspace identity contains unknown fields.")
        return cls(
            schema_version=value["schema_version"],
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
            "workspace_revision_start": self.workspace_revision_start,
            "workspace_revision_end": self.workspace_revision_end,
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
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
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


class EventRecorder:
    """Process-local node observer used by the durable graph factory."""

    def __init__(
        self,
        sink: EventSink,
        *,
        run_id: str,
        thread_id: str,
        task_id: str,
        model: Mapping[str, Any] | None = None,
        secret_filter: KnownSecretFilter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sink = sink
        self.run_id = run_id
        self.thread_id = thread_id
        self.task_id = task_id
        self.model = model
        self.secret_filter = secret_filter
        self.clock = clock

    def append(self, event: RunEvent) -> AppendResult:
        if self.secret_filter is not None:
            event = RunEvent.from_dict(
                self.secret_filter.sanitize(event.to_dict()).value
            )
        result = self.sink.append_once(event)
        if not result.ok:
            raise AuditIncompleteError(
                result.error_code or EventSinkErrorCode.IO_ERROR.value,
                result.message or "The audit event could not be appended.",
            )
        return result

    def lifecycle(self, *, phase: str, status: str, summary: Mapping[str, Any] | None = None) -> AppendResult:
        return self.append(
            RunEvent.create(
                run_id=self.run_id,
                thread_id=self.thread_id,
                task_id=self.task_id,
                subject_id=self.run_id,
                event_type="run",
                phase=phase,
                status=status,
                timestamp=self.clock(),
                summary=summary,
            )
        )

    def wrap_node(self, node: Any, *, event_type: str, node_name: str):
        def wrapped(state: Any, *args: Any, **kwargs: Any):
            subjects = self._subjects(state, event_type=event_type, node_name=node_name)
            for step_id, call_id, subject_id in subjects:
                self.append(
                    self._event(
                        subject_id=subject_id,
                        event_type=event_type,
                        phase="start",
                        status="started",
                        state=state,
                        step_id=step_id,
                        call_id=call_id,
                    )
                )
            try:
                result = node(state, *args, **kwargs)
            except Exception as error:
                for step_id, call_id, subject_id in subjects:
                    self.append(
                        self._event(
                            subject_id=subject_id,
                            event_type=event_type,
                            phase="error",
                            status="failed",
                            state=state,
                            step_id=step_id,
                            call_id=call_id,
                            error={
                                "type": type(error).__name__,
                                "message": str(error)[:_MAX_SUMMARY_TEXT],
                            },
                        )
                    )
                raise
            runtime_error = _mapping_value(result, "runtime_error_code")
            for step_id, call_id, subject_id in subjects:
                self.append(
                    self._event(
                        subject_id=subject_id,
                        event_type=event_type,
                        phase="error" if runtime_error is not None else "result",
                        status="failed" if runtime_error is not None else "succeeded",
                        state=state,
                        result=result,
                        step_id=step_id,
                        call_id=call_id,
                        error=(
                            {
                                "code": _enum_value(runtime_error),
                                "message": str(
                                    _mapping_value(result, "runtime_message") or ""
                                )[:_MAX_SUMMARY_TEXT],
                            }
                            if runtime_error is not None
                            else None
                        ),
                    )
                )
            return result

        return wrapped

    def _event(
        self,
        *,
        subject_id: str,
        event_type: str,
        phase: str,
        status: str,
        state: Any,
        result: Any = None,
        step_id: str | None = None,
        call_id: str | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> RunEvent:
        usage = _usage_summary(state, result, call_id)
        summary = {
            "node": subject_id.split(":", 1)[0],
            "result_keys": sorted(result.keys())
            if isinstance(result, Mapping)
            else (),
        }
        if event_type == "tool":
            tool_name, tool_call_id = _tool_identity(state, call_id)
            if tool_name is not None:
                summary["tool_name"] = tool_name
            if tool_call_id is not None:
                summary["tool_call_id"] = tool_call_id
        return RunEvent.create(
            run_id=self.run_id,
            thread_id=self.thread_id,
            task_id=self.task_id,
            subject_id=subject_id,
            event_type=event_type,
            phase=phase,
            status=status,
            timestamp=self.clock(),
            step_id=step_id,
            call_id=call_id,
            model=self.model if event_type == "model" else None,
            usage=usage,
            summary=summary,
            error=error,
        )

    def _subjects(
        self, state: Any, *, event_type: str, node_name: str
    ) -> list[tuple[str | None, str | None, str]]:
        if event_type in {"model", "tool"}:
            budget = _state_attr(state, "budget")
            active = getattr(budget, "active", ())
            if active:
                return [
                    (
                        getattr(item, "step_id", None),
                        getattr(item, "call_id", None),
                        str(getattr(item, "call_id", node_name)),
                    )
                    for item in active
                ]
        task = _state_attr(state, "current_task_idx")
        atomic = _state_attr(state, "current_atomic_task_idx")
        repair_attempt = _state_attr(state, "repair_attempts", 0)
        suffix = (
            f"{node_name}:{task}:{atomic}:repair-{repair_attempt}"
            if task is not None or atomic is not None
            else f"{node_name}:repair-{repair_attempt}"
        )
        return [(None, None, suffix)]


def _usage_summary(state: Any, result: Any, call_id: str | None) -> dict[str, Any] | None:
    durable = _mapping_value(result, "durable_call_result")
    if durable is None:
        durable = _state_attr(state, "durable_call_result")
    if durable is None or call_id is None:
        return None
    call_ids = tuple(getattr(durable, "call_ids", ()))
    usage_values = tuple(getattr(durable, "usage", ()))
    try:
        usage = usage_values[call_ids.index(call_id)]
    except (ValueError, IndexError):
        return None
    return {
        "status": _enum_value(getattr(usage, "status", None)),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "cost_microusd": getattr(usage, "cost_microusd", None),
        "cost_source": getattr(usage, "cost_source", None),
    }


def _tool_identity(state: Any, call_id: str | None) -> tuple[str | None, str | None]:
    """Extract only a tool name for audit context; never persist arguments/content."""
    if call_id is None:
        return None, None
    tool_call_id = call_id
    budget = _state_attr(state, "budget")
    active = getattr(budget, "active", ())
    for reservation in active:
        if getattr(reservation, "call_id", None) == call_id:
            tool_call_id = getattr(reservation, "tool_call_id", None) or call_id
            break
    messages = _state_attr(state, "atomic_implementation_research")
    if messages is None:
        messages = _state_attr(state, "implementation_research_scratchpad", ())
    for message in reversed(tuple(messages or ())):
        if getattr(message, "tool_call_id", None) == tool_call_id:
            name = getattr(message, "name", None)
            if isinstance(name, str) and name.strip():
                return name.strip()[:_MAX_SUMMARY_TEXT], tool_call_id
        for tool_call in getattr(message, "tool_calls", ()) or ():
            if str(tool_call.get("id", "")) == tool_call_id:
                name = tool_call.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()[:_MAX_SUMMARY_TEXT], tool_call_id
    return None, tool_call_id


def _mapping_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return None


def _state_attr(state: Any, name: str, default: Any = None) -> Any:
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _bounded_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[DEPTH_LIMIT]"
    if isinstance(value, str):
        return value[:_MAX_SUMMARY_TEXT]
    if isinstance(value, Mapping):
        return {
            str(key)[:_MAX_SUMMARY_TEXT]: _bounded_json_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_json_value(item, depth=depth + 1) for item in value[:64]]
    return _json_value(value)


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string.")


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a non-string sequence.")
    result = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError(f"{name} must contain non-empty strings.")
    return result


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "RECORD_SCHEMA_VERSION",
    "RECORDS_DIRECTORY",
    "EVENTS_FILENAME",
    "AppendResult",
    "AppendStatus",
    "AppendResultStatus",
    "AuditIncompleteError",
    "AuditStatus",
    "EventSink",
    "EventSinkError",
    "EventSinkErrorCode",
    "EventRecorder",
    "JsonlEventSink",
    "RunEvent",
    "RunRecord",
    "RecordWriteResult",
    "make_idempotency_key",
    "record_ref_for",
    "write_run_record",
]
