"""Node-level audit recorder for public durable graph seams."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent.runtime.secrets import KnownSecretFilter
from agent.runtime.trajectory_contracts import (
    _MAX_SUMMARY_TEXT,
    AppendResult,
    AuditIncompleteError,
    EventSink,
    EventSinkError,
    EventSinkErrorCode,
    RunEvent,
    _canonical_digest,
)


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
        try:
            result = self.sink.append_once(event)
        except EventSinkError as error:
            error_code = getattr(error.code, "value", error.code)
            message = str(error)
            if self.secret_filter is not None:
                error_code = self.secret_filter.redact_text(str(error_code))
                message = self.secret_filter.redact_text(message)
            # Do not chain the original exception: LangGraph may persist the
            # raised exception as a pending write before the caller sees it.
            raise AuditIncompleteError(str(error_code), message) from None
        if not result.ok:
            error_code = result.error_code or EventSinkErrorCode.IO_ERROR.value
            message = result.message or "The audit event could not be appended."
            if self.secret_filter is not None:
                error_code = self.secret_filter.redact_text(str(error_code))
                message = self.secret_filter.redact_text(str(message))
            raise AuditIncompleteError(
                str(error_code),
                str(message),
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
                                "message_digest": _canonical_digest(str(error)),
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
                                "message_digest": _canonical_digest(
                                    str(
                                        _mapping_value(result, "runtime_message")
                                        or ""
                                    )
                                ),
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
        if event_type in {"model", "tool"}:
            request_digest, response_digest = _call_digests(
                state, result, call_id
            )
            if request_digest is not None:
                summary["request_digest"] = request_digest
            if response_digest is not None:
                summary["response_digest"] = response_digest
        if event_type == "tool":
            tool_name, tool_call_id = _tool_identity(state, call_id)
            if tool_name is not None:
                summary["tool_name"] = tool_name
            if tool_call_id is not None:
                summary["tool_call_id"] = tool_call_id
        elif event_type == "edit":
            edit = _mapping_value(result, "last_edit_result")
            if edit is None:
                edit = _state_attr(state, "last_edit_result")
            summary.update(_edit_evidence(edit))
        elif event_type == "verification":
            checks = _mapping_value(result, "post_verification")
            if checks is None:
                checks = _mapping_value(result, "baseline_verification")
            if checks is None:
                checks = _state_attr(state, "post_verification", ())
            summary.update(
                {
                    "checks": _verification_evidence(checks),
                    "attempt": _state_attr(state, "repair_attempts", 0),
                }
            )
        artifact_refs = (
            _verification_artifact_refs(checks)
            if event_type == "verification"
            else ()
        )
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
            artifact_refs=artifact_refs,
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


def _call_digests(
    state: Any, result: Any, call_id: str | None
) -> tuple[str | None, str | None]:
    if call_id is None:
        return None, None
    budget = _state_attr(state, "budget")
    request_digest = None
    for reservation in getattr(budget, "active", ()):
        if getattr(reservation, "call_id", None) == call_id:
            request_digest = getattr(reservation, "request_digest", None)
            break
    durable = _mapping_value(result, "durable_call_result")
    if durable is None:
        durable = _state_attr(state, "durable_call_result")
    response_digest = None
    if durable is not None:
        call_ids = tuple(getattr(durable, "call_ids", ()))
        digests = tuple(getattr(durable, "response_digests", ()))
        try:
            response_digest = digests[call_ids.index(call_id)]
        except (ValueError, IndexError):
            pass
    return request_digest, response_digest


def _edit_evidence(edit: Any) -> dict[str, Any]:
    if edit is None:
        return {}
    diff = getattr(edit, "diff", None)
    return {
        "status": _enum_value(getattr(edit, "status", None)),
        "error_code": _enum_value(getattr(edit, "error_code", None)),
        "before_hash": getattr(edit, "before_hash", None),
        "after_hash": getattr(edit, "after_hash", None),
        "diff_digest": _canonical_digest(diff) if isinstance(diff, str) else None,
    }


def _verification_evidence(checks: Any) -> list[dict[str, Any]]:
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes, bytearray)):
        return []
    evidence: list[dict[str, Any]] = []
    for check in checks:
        name = getattr(check, "name", None)
        status = _enum_value(getattr(check, "status", None))
        if not isinstance(name, str) or not name.strip():
            continue
        evidence.append(
            {
                "check_id": name,
                "status": status,
                "failure_id": getattr(check, "failure_id", None),
                "stdout_digest": getattr(check, "stdout_digest", None),
                "stderr_digest": getattr(check, "stderr_digest", None),
                "stdout_truncated": bool(
                    getattr(check, "stdout_truncated", False)
                ),
                "stderr_truncated": bool(
                    getattr(check, "stderr_truncated", False)
                ),
                "artifact_error_code": getattr(
                    check, "artifact_error_code", None
                ),
            }
        )
    return evidence


def _verification_artifact_refs(checks: Any) -> tuple[str, ...]:
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes, bytearray)):
        return ()
    refs: list[str] = []
    for check in checks:
        for name in ("stdout_artifact", "stderr_artifact"):
            ref = getattr(check, name, None)
            path = getattr(ref, "path", None)
            if isinstance(path, str) and path:
                refs.append(path)
    return tuple(refs)


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
