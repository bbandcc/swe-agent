"""Pure rendering of tool calls and results for model research context."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

UNTRUSTED_EVIDENCE_MARKER = "[UNTRUSTED EVIDENCE]"
INCOMPLETE_TOOL_EXCHANGE_MARKER = "[INCOMPLETE TOOL EXCHANGE]"

_METADATA_KEYS = ("path", "hash", "truncated", "status")


@dataclass(frozen=True, slots=True)
class _ToolCall:
    order: int
    call_id: str | None
    name: str | None
    args: Any


@dataclass(frozen=True, slots=True)
class _ToolResult:
    order: int
    call_id: str | None
    name: str | None
    content: Any
    metadata: dict[str, Any]


def render_tool_messages(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """Render complete, ID-paired tool exchanges as safe model context.

    The input is never reordered in durable state.  Rendering first indexes all
    calls/results, then emits each call with the result selected by
    ``tool_call_id``.  Ambiguous exchanges remain visible as explicit errors;
    no result is silently attached to a different call.
    """

    calls: list[_ToolCall] = []
    calls_by_id: dict[str, list[_ToolCall]] = defaultdict(list)
    results_by_id: dict[str, list[_ToolResult]] = defaultdict(list)
    unknown_results: list[_ToolResult] = []

    for message_order, message in enumerate(messages):
        if _is_ai_with_tool_calls(message):
            for raw_call in message.tool_calls:
                call = _tool_call_from_raw(len(calls), raw_call)
                calls.append(call)
                if call.call_id is not None:
                    calls_by_id[call.call_id].append(call)
            continue
        if getattr(message, "type", None) != "tool":
            continue
        result = _tool_result_from_message(message_order, message)
        if result.call_id is None:
            unknown_results.append(result)
        else:
            results_by_id[result.call_id].append(result)

    for call_id, results in tuple(results_by_id.items()):
        if call_id not in calls_by_id:
            unknown_results.extend(results)
            del results_by_id[call_id]

    rendered: list[AnyMessage] = []
    call_index = 0
    for message in messages:
        if _is_ai_with_tool_calls(message):
            for raw_call in message.tool_calls:
                call = calls[call_index]
                call_index += 1
                rendered.append(
                    AIMessage(content=_render_call(call, calls_by_id))
                )
                rendered.append(
                    HumanMessage(
                        content=_render_result(call, calls_by_id, results_by_id)
                    )
                )
        elif getattr(message, "type", None) == "tool":
            continue
        else:
            rendered.append(message)

    for result in unknown_results:
        rendered.append(
            HumanMessage(content=_render_unknown_result(result))
        )
    return rendered


def _is_ai_with_tool_calls(message: AnyMessage) -> bool:
    return (
        getattr(message, "type", None) == "ai"
        and bool(getattr(message, "tool_calls", None))
    )


def _tool_call_from_raw(order: int, raw_call: Any) -> _ToolCall:
    if not isinstance(raw_call, Mapping):
        return _ToolCall(order, None, None, raw_call)
    raw_id = raw_call.get("id")
    call_id = str(raw_id) if raw_id is not None and str(raw_id) else None
    raw_name = raw_call.get("name")
    name = str(raw_name) if raw_name is not None else None
    return _ToolCall(order, call_id, name, raw_call.get("args"))


def _tool_result_from_message(order: int, message: AnyMessage) -> _ToolResult:
    raw_id = getattr(message, "tool_call_id", None)
    call_id = str(raw_id) if raw_id is not None and str(raw_id) else None
    raw_name = getattr(message, "name", None)
    name = str(raw_name) if raw_name is not None else None
    return _ToolResult(
        order=order,
        call_id=call_id,
        name=name,
        content=getattr(message, "content", None),
        metadata=_metadata_for(message),
    )


def _render_call(
    call: _ToolCall, calls_by_id: Mapping[str, Sequence[_ToolCall]]
) -> str:
    lines = [
        "[UNTRUSTED TOOL CALL]",
        f"call_id: {call.call_id or '<missing>'}",
        f"tool_name: {call.name or '<missing>'}",
        f"arguments: {_stable_text(call.args)}",
    ]
    if call.call_id is None:
        lines.append(f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} error=missing_call_id")
    elif len(calls_by_id.get(call.call_id, ())) > 1:
        lines.append(
            f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} "
            "error=duplicate_tool_call_id"
        )
    return "\n".join(lines)


def _render_result(
    call: _ToolCall,
    calls_by_id: Mapping[str, Sequence[_ToolCall]],
    results_by_id: Mapping[str, Sequence[_ToolResult]],
) -> str:
    lines = [UNTRUSTED_EVIDENCE_MARKER]
    if call.call_id is None:
        lines.append(f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} error=missing_call_id")
        return "\n".join(lines)

    matching_calls = calls_by_id.get(call.call_id, ())
    results = results_by_id.get(call.call_id, ())
    if len(matching_calls) > 1:
        lines.append(
            f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} "
            "error=duplicate_tool_call_id; result_not_assigned"
        )
        lines.extend(_render_result_entries(results))
        return "\n".join(lines)
    if not results:
        lines.append(
            f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} "
            "error=missing_tool_message"
        )
        return "\n".join(lines)
    if len(results) > 1:
        lines.append(
            f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} "
            "error=duplicate_tool_message; result_not_assigned"
        )
        lines.extend(_render_result_entries(results))
        return "\n".join(lines)

    result = results[0]
    if result.name is not None and call.name is not None and result.name != call.name:
        lines.append(
            f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} "
            "error=tool_name_mismatch; result_not_assigned"
        )
    lines.extend(_render_result_entries((result,)))
    return "\n".join(lines)


def _render_unknown_result(result: _ToolResult) -> str:
    error = (
        "missing_tool_call_id" if result.call_id is None else "unknown_tool_call_id"
    )
    lines = [
        UNTRUSTED_EVIDENCE_MARKER,
        f"{INCOMPLETE_TOOL_EXCHANGE_MARKER} error={error}",
        f"call_id: {result.call_id or '<missing>'}",
    ]
    lines.extend(_render_result_entries((result,)))
    return "\n".join(lines)


def _render_result_entries(results: Sequence[_ToolResult]) -> list[str]:
    lines: list[str] = []
    for result in results:
        if result.name is not None:
            lines.append(f"tool_name: {result.name}")
        if result.metadata:
            lines.append(f"metadata: {_stable_text(result.metadata)}")
        lines.append(f"content: {_stable_text(result.content)}")
    return lines


def _metadata_for(message: AnyMessage) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in _METADATA_KEYS:
        if hasattr(message, key):
            value = getattr(message, key)
            if value is not None:
                metadata[key] = value
    for source_name in ("additional_kwargs", "response_metadata"):
        source = getattr(message, source_name, None)
        if not isinstance(source, Mapping):
            continue
        for key in _METADATA_KEYS:
            if key in source and source[key] is not None:
                metadata.setdefault(key, source[key])
    return metadata


def _stable_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


__all__ = [
    "INCOMPLETE_TOOL_EXCHANGE_MARKER",
    "UNTRUSTED_EVIDENCE_MARKER",
    "render_tool_messages",
]
