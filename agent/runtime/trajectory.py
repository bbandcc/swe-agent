"""Stable public facade for the versioned local audit contracts.

The implementation is split into contracts, sink, record, and recorder modules;
existing callers continue importing from agent.runtime.trajectory.
"""

from agent.runtime.trajectory_contracts import (
    EVENT_SCHEMA_VERSION,
    EVENTS_FILENAME,
    RECORD_SCHEMA_VERSION,
    RECORDS_DIRECTORY,
    AppendResult,
    AppendResultStatus,
    AppendStatus,
    AuditIncompleteError,
    AuditStatus,
    EventSink,
    EventSinkError,
    EventSinkErrorCode,
    RunEvent,
    make_idempotency_key,
)
from agent.runtime.trajectory_sink import JsonlEventSink
from agent.runtime.trajectory_record import (
    RecordWriteResult,
    RunRecord,
    record_ref_for,
    write_run_record,
)
from agent.runtime.trajectory_recorder import EventRecorder

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
