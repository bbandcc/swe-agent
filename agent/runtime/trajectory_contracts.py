"""Versioned audit contracts shared by the local trajectory modules."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

EVENT_SCHEMA_VERSION = 1
# Version 2 records structured workspace revision evidence at run start/end.
RECORD_SCHEMA_VERSION = 2
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
