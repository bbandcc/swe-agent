"""Single-process JSONL audit sink."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

from agent.runtime.secrets import KnownSecretFilter
from agent.runtime.trajectory_contracts import (
    EVENTS_FILENAME,
    AppendResult,
    AppendStatus,
    EventSinkError,
    EventSinkErrorCode,
    RunEvent,
)


class JsonlEventSink:
    """Deterministic single-process JSONL sink rooted outside the workspace."""

    _path_locks: dict[str, threading.Lock] = {}
    _path_locks_guard = threading.Lock()

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
        lock_key = os.path.normcase(str(self.path.resolve(strict=False)))
        with self._path_locks_guard:
            self._path_lock = self._path_locks.setdefault(lock_key, threading.Lock())
        self._events: dict[str, RunEvent] = {}
        self._last_sequence = 0
        try:
            with self._path_lock:
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
        with self._path_lock, self._lock:
            # A different sink instance in this process may have appended
            # since this instance was constructed. Refresh while the shared
            # path lock is held so sequence assignment is based on disk.
            self._load_existing()
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
        self._events.clear()
        self._last_sequence = 0
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
