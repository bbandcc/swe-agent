"""LangGraph node helpers for durable reserve, dispatch, and settle boundaries."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from agent.runtime.budget import (
    BudgetController,
    BudgetErrorCode,
    BudgetSnapshot,
    UsageRecord,
)
from agent.runtime.calls import ModelCallResult


@dataclass(frozen=True, slots=True)
class DurableCallResult:
    call_ids: tuple[str, ...]
    usage: tuple[UsageRecord, ...]
    response_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        size = len(self.call_ids)
        if size == 0 or len(self.usage) != size or len(self.response_digests) != size:
            raise ValueError("Durable call result fields must have equal non-zero size.")
        if any(len(digest) != 64 for digest in self.response_digests):
            raise ValueError("Response digests must be SHA-256 hex strings.")


class DurableBudgetState(BaseModel):
    """Fields shared across parent and child graph checkpoints."""

    budget: BudgetSnapshot | None = None
    durable_call_result: DurableCallResult | None = None
    runtime_error_code: BudgetErrorCode | None = None
    runtime_message: str = ""


class DurableBudgetBoundary:
    """Creates small state updates around existing model and ToolNode calls."""

    def __init__(
        self,
        run_id: str,
        *,
        controller: BudgetController | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not run_id:
            raise ValueError("run_id must be non-empty.")
        self.run_id = run_id
        self.controller = controller or BudgetController()
        self.clock = clock

    def reserve_model(
        self, state: DurableBudgetState, label: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        snapshot = self._snapshot(state)
        decision = self.controller.reserve_model(
            snapshot,
            run_id=self.run_id,
            request_digest=_digest({"label": label, "request": request}),
            now=self.clock(),
        )
        return self._decision_update(decision)

    def reserve_tools(
        self, state: DurableBudgetState, tool_calls: list[dict[str, Any]]
    ) -> dict[str, Any]:
        decision = self.controller.reserve_tools(
            self._snapshot(state),
            run_id=self.run_id,
            tool_calls=tuple(
                (
                    str(call.get("id", "")),
                    _digest({"name": call.get("name"), "args": call.get("args")}),
                )
                for call in tool_calls
            ),
            now=self.clock(),
        )
        return self._decision_update(decision)

    def capture_model(
        self, state: DurableBudgetState, response: Any
    ) -> tuple[Any, dict[str, Any]]:
        active = self._snapshot(state).active
        if len(active) != 1:
            raise ValueError("A model result requires one active reservation.")
        if isinstance(response, ModelCallResult):
            value = response.value
            usage = response.usage.for_call(active[0].call_id)
            digest = response.response_digest
            error_update = (
                {
                    "runtime_error_code": BudgetErrorCode.MODEL_OUTPUT_INVALID,
                    "runtime_message": response.error,
                }
                if response.error is not None
                else {}
            )
        else:
            value = response
            usage = UsageRecord.unknown(active[0].call_id)
            digest = _digest({"type": type(response).__name__})
            error_update = {}
        return value, {
            **error_update,
            "durable_call_result": DurableCallResult(
                (active[0].call_id,), (usage,), (digest,)
            )
        }

    def record_tool_results(self, state: DurableBudgetState) -> dict[str, Any]:
        active = self._snapshot(state).active
        messages = getattr(state, "atomic_implementation_research", None)
        if messages is None:
            messages = getattr(state, "implementation_research_scratchpad", ())
        by_id = {
            message.tool_call_id: message
            for message in messages
            if isinstance(message, ToolMessage)
        }
        if any(item.tool_call_id not in by_id for item in active):
            raise ValueError("Durable ToolMessages do not match the reserved batch.")
        usage = tuple(UsageRecord.unknown(item.call_id) for item in active)
        digests = tuple(
            _digest(
                {
                    "tool_call_id": item.tool_call_id,
                    "name": by_id[item.tool_call_id].name,
                    "content": by_id[item.tool_call_id].content,
                }
            )
            for item in active
        )
        return {
            "durable_call_result": DurableCallResult(
                tuple(item.call_id for item in active), usage, digests
            )
        }

    def settle(self, state: DurableBudgetState) -> dict[str, Any]:
        result = state.durable_call_result
        if result is None:
            raise ValueError("A durable call result is required before settle.")
        snapshot = self.controller.settle(self._snapshot(state), result.usage)
        return {
            "budget": snapshot,
            "durable_call_result": None,
        }

    @staticmethod
    def after_settle(state: DurableBudgetState, normal_route: str) -> str:
        return "end" if state.runtime_error_code is not None else normal_route

    @staticmethod
    def may_dispatch(state: DurableBudgetState) -> str:
        return "dispatch" if state.runtime_error_code is None else "end"

    @staticmethod
    def _snapshot(state: DurableBudgetState) -> BudgetSnapshot:
        if state.budget is None:
            raise ValueError("Durable graph state has no budget snapshot.")
        return state.budget

    @staticmethod
    def _decision_update(decision: Any) -> dict[str, Any]:
        return {
            "budget": decision.snapshot,
            "durable_call_result": None,
            "runtime_error_code": decision.error_code,
            "runtime_message": decision.message,
        }


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_safe_json,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_json(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "get_secret_value"):
        return "<secret>"
    return {"type": type(value).__name__}
