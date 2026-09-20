"""LangGraph node helpers for durable reserve, dispatch, and settle boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
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
from agent.runtime.config import DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class DurableCallResult:
    call_ids: tuple[str, ...]
    usage: tuple[UsageRecord, ...]
    response_digests: tuple[str, ...]
    failed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "call_ids", _sequence_tuple(self.call_ids, "call_ids")
        )
        object.__setattr__(self, "usage", _sequence_tuple(self.usage, "usage"))
        object.__setattr__(
            self,
            "response_digests",
            _sequence_tuple(self.response_digests, "response_digests"),
        )
        if not isinstance(self.failed, bool):
            raise ValueError("failed must be a boolean.")
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
        model_request_timeout_seconds: float = DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not run_id:
            raise ValueError("run_id must be non-empty.")
        self.run_id = run_id
        self.controller = controller or BudgetController()
        self.clock = clock
        if (
            isinstance(model_request_timeout_seconds, bool)
            or not isinstance(model_request_timeout_seconds, (int, float))
            or not math.isfinite(model_request_timeout_seconds)
            or model_request_timeout_seconds <= 0
        ):
            raise ValueError(
                "model_request_timeout_seconds must be a finite positive number."
            )
        self.model_request_timeout_seconds = float(model_request_timeout_seconds)

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
        snapshot = self._snapshot(state)
        active = snapshot.active
        if len(active) != 1:
            raise ValueError("A model result requires one active reservation.")
        failed = False
        if isinstance(response, ModelCallResult):
            value = response.value
            usage = response.usage.for_call(active[0].call_id)
            digest = response.response_digest
            failed = response.error_code is not None or response.error is not None
            if response.error_code is not None:
                error_update = {
                    "runtime_error_code": response.error_code,
                    "runtime_message": response.error or response.error_code.value,
                }
            elif response.error is not None:
                error_update = {
                    "runtime_error_code": BudgetErrorCode.MODEL_OUTPUT_INVALID,
                    "runtime_message": response.error,
                }
            else:
                error_update = {}
        else:
            value = response
            usage = UsageRecord.unknown(active[0].call_id)
            digest = _digest({"type": type(response).__name__})
            error_update = {}
        return value, {
            **error_update,
            "durable_call_result": DurableCallResult(
                (active[0].call_id,),
                (usage,),
                (digest,),
                failed=failed,
            ),
            **self._deadline_overrun_update(snapshot),
        }

    def guard_dispatch(self, state: DurableBudgetState) -> dict[str, Any]:
        """Recheck the persisted deadline immediately before an external call."""
        snapshot = self._snapshot(state)
        if self.clock() < snapshot.deadline_at:
            return {}
        return self._deadline_dispatch_update(snapshot)

    def model_dispatch_timeout(
        self, state: DurableBudgetState
    ) -> tuple[float | None, dict[str, Any]]:
        """Return the effective provider timeout at the dispatch boundary."""
        snapshot = self._snapshot(state)
        remaining = snapshot.deadline_at - self.clock()
        if remaining <= 0:
            return None, self._deadline_dispatch_update(snapshot)
        return min(self.model_request_timeout_seconds, remaining), {}

    @staticmethod
    def _deadline_dispatch_update(snapshot: BudgetSnapshot) -> dict[str, Any]:
        active = snapshot.active
        if not active:
            raise ValueError("A dispatch guard requires active reservations.")
        usage = tuple(UsageRecord.unknown(item.call_id) for item in active)
        digests = tuple(
            _digest(
                {
                    "call_id": item.call_id,
                    "status": "deadline_expired_before_dispatch",
                }
            )
            for item in active
        )
        return {
            "durable_call_result": DurableCallResult(
                tuple(item.call_id for item in active),
                usage,
                digests,
                failed=True,
            ),
            "runtime_error_code": BudgetErrorCode.TIMEOUT_OVERRUN,
            "runtime_message": (
                "The absolute run deadline expired after reservation and "
                "before dispatch; no external call was made."
            ),
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
            ),
            **self._deadline_overrun_update(self._snapshot(state)),
        }

    def check_deadline(
        self, state: DurableBudgetState, *, message: str | None = None
    ) -> dict[str, Any]:
        """Guard deterministic side effects without consuming a budget step."""
        return self._deadline_overrun_update(self._snapshot(state), message)

    def settle(self, state: DurableBudgetState) -> dict[str, Any]:
        result = state.durable_call_result
        if result is None:
            raise ValueError("A durable call result is required before settle.")
        snapshot = self.controller.settle(
            self._snapshot(state), result.usage, failed=result.failed
        )
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

    def _deadline_overrun_update(
        self, snapshot: BudgetSnapshot, message: str | None = None
    ) -> dict[str, Any]:
        if self.clock() < snapshot.deadline_at:
            return {}
        return {
            "runtime_error_code": BudgetErrorCode.TIMEOUT_OVERRUN,
            "runtime_message": message
            or (
                "The external call crossed the absolute run deadline; "
                "its result was recorded but later side effects were blocked."
            ),
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


def _sequence_tuple(value: object, name: str) -> tuple:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise ValueError(f"{name} must be a non-string sequence.")
    return tuple(value)
