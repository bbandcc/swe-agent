"""Immutable, checkpoint-safe call budget contracts."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING
from enum import Enum

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MICRO_USD = Decimal("1000000")


class CallKind(str, Enum):
    MODEL = "model"
    TOOL = "tool"


class CallStatus(str, Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class UsageStatus(str, Enum):
    KNOWN = "known"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class BudgetErrorCode(str, Enum):
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    MAX_COST_EXCEEDED = "max_cost_exceeded"
    USAGE_UNKNOWN = "budget_usage_unknown"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TIMEOUT_OVERRUN = "timeout_overrun"
    CALL_IN_FLIGHT = "call_in_flight"
    OUTCOME_UNKNOWN = "outcome_unknown"
    MODEL_OUTPUT_INVALID = "model_output_invalid"


@dataclass(frozen=True, slots=True)
class UsageRecord:
    call_id: str
    status: UsageStatus
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_microusd: int | None
    cost_source: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ValueError("call_id must be a non-empty string.")
        if not isinstance(self.status, UsageStatus):
            raise ValueError("status must be UsageStatus.")
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_microusd",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None.")
        if self.status is UsageStatus.UNKNOWN and any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
                self.cost_microusd,
            )
        ):
            raise ValueError("UNKNOWN usage must preserve all numeric values as None.")
        if self.status is UsageStatus.KNOWN and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.total_tokens is None
            or self.cost_microusd is None
            or not self.cost_source
        ):
            raise ValueError("KNOWN usage requires complete tokens and cost.")
        if self.cost_source is not None and not self.cost_source.strip():
            raise ValueError("cost_source must be non-empty or None.")

    @classmethod
    def unknown(cls, call_id: str) -> "UsageRecord":
        return cls(call_id, UsageStatus.UNKNOWN, None, None, None, None, None)


@dataclass(frozen=True, slots=True)
class CallReservation:
    step_id: str
    call_id: str
    kind: CallKind
    status: CallStatus
    request_digest: str
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.step_id or not self.call_id:
            raise ValueError("step_id and call_id must be non-empty.")
        if not isinstance(self.kind, CallKind) or not isinstance(
            self.status, CallStatus
        ):
            raise ValueError("Invalid call reservation enum value.")
        if not _DIGEST.fullmatch(self.request_digest):
            raise ValueError("request_digest must be a SHA-256 hex digest.")
        if self.kind is CallKind.TOOL and not self.tool_call_id:
            raise ValueError("Tool reservations require tool_call_id.")
        if self.kind is CallKind.MODEL and self.tool_call_id is not None:
            raise ValueError("Model reservations must not contain tool_call_id.")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    max_steps: int
    max_cost_microusd: int | None
    deadline_at: float
    reservations: tuple[CallReservation, ...] = ()
    usage: tuple[UsageRecord, ...] = ()
    cost_microusd: int = 0
    cost_unknown: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reservations",
            _sequence_tuple(self.reservations, "reservations"),
        )
        object.__setattr__(self, "usage", _sequence_tuple(self.usage, "usage"))
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps <= 0
        ):
            raise ValueError("max_steps must be a positive integer.")
        if self.max_cost_microusd is not None and (
            isinstance(self.max_cost_microusd, bool)
            or not isinstance(self.max_cost_microusd, int)
            or self.max_cost_microusd <= 0
        ):
            raise ValueError("max_cost_microusd must be positive or None.")
        if not isinstance(self.deadline_at, (int, float)) or not math.isfinite(
            self.deadline_at
        ):
            raise ValueError("deadline_at must be finite.")
        if self.steps_used > self.max_steps:
            raise ValueError("Reservations exceed max_steps.")

    @classmethod
    def create(
        cls,
        *,
        max_steps: int,
        max_cost_usd: Decimal | None,
        deadline_at: float,
    ) -> "BudgetSnapshot":
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or max_steps <= 0
        ):
            raise ValueError("max_steps must be a positive integer.")
        if not isinstance(deadline_at, (int, float)) or not math.isfinite(
            deadline_at
        ):
            raise ValueError("deadline_at must be finite.")
        max_cost = None
        if max_cost_usd is not None:
            if (
                not isinstance(max_cost_usd, Decimal)
                or not max_cost_usd.is_finite()
                or max_cost_usd <= 0
            ):
                raise ValueError("max_cost_usd must be a finite positive Decimal.")
            max_cost = int(
                (max_cost_usd * _MICRO_USD).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
        return cls(max_steps, max_cost, float(deadline_at))

    @property
    def steps_used(self) -> int:
        return len(self.reservations)

    @property
    def active(self) -> tuple[CallReservation, ...]:
        return tuple(
            reservation
            for reservation in self.reservations
            if reservation.status is CallStatus.IN_FLIGHT
        )

    @property
    def has_unknown_outcome(self) -> bool:
        return any(
            reservation.status is CallStatus.OUTCOME_UNKNOWN
            for reservation in self.reservations
        )


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    snapshot: BudgetSnapshot
    reservations: tuple[CallReservation, ...] = ()
    error_code: BudgetErrorCode | None = None
    message: str = ""


class BudgetController:
    """Pure budget state transitions; persistence belongs to LangGraph."""

    def reserve_model(
        self,
        snapshot: BudgetSnapshot,
        *,
        run_id: str,
        request_digest: str,
        now: float,
    ) -> BudgetDecision:
        return self._reserve(
            snapshot,
            run_id=run_id,
            requests=((CallKind.MODEL, None, request_digest),),
            now=now,
        )

    def reserve_tools(
        self,
        snapshot: BudgetSnapshot,
        *,
        run_id: str,
        tool_calls: tuple[tuple[str, str], ...],
        now: float,
    ) -> BudgetDecision:
        if not tool_calls:
            raise ValueError("tool_calls must not be empty.")
        tool_call_ids = tuple(call_id for call_id, _ in tool_calls)
        if any(not call_id for call_id in tool_call_ids) or len(
            set(tool_call_ids)
        ) != len(tool_call_ids):
            raise ValueError("Tool call ids must be non-empty and unique per batch.")
        return self._reserve(
            snapshot,
            run_id=run_id,
            requests=tuple(
                (CallKind.TOOL, call_id, digest)
                for call_id, digest in tool_calls
            ),
            now=now,
        )

    def _reserve(
        self,
        snapshot: BudgetSnapshot,
        *,
        run_id: str,
        requests: tuple[tuple[CallKind, str | None, str], ...],
        now: float,
    ) -> BudgetDecision:
        denial = self._preflight(
            snapshot,
            len(requests),
            now,
            enforce_cost=requests[0][0] is CallKind.MODEL,
        )
        if denial is not None:
            return denial
        if not run_id:
            raise ValueError("run_id must be non-empty.")
        first_ordinal = snapshot.steps_used + 1
        reservations = tuple(
            CallReservation(
                step_id=f"{run_id}:step:{first_ordinal + offset}",
                call_id=(
                    f"{run_id}:call:{first_ordinal + offset}:model"
                    if kind is CallKind.MODEL
                    else f"{run_id}:call:{first_ordinal + offset}:tool:{tool_call_id}"
                ),
                kind=kind,
                status=CallStatus.IN_FLIGHT,
                request_digest=digest,
                tool_call_id=tool_call_id,
            )
            for offset, (kind, tool_call_id, digest) in enumerate(requests)
        )
        updated = replace(
            snapshot,
            reservations=tuple(snapshot.reservations) + reservations,
        )
        return BudgetDecision(True, updated, reservations)

    def _preflight(
        self,
        snapshot: BudgetSnapshot,
        count: int,
        now: float,
        *,
        enforce_cost: bool,
    ) -> BudgetDecision | None:
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("now must be finite.")
        if snapshot.has_unknown_outcome:
            return self._denied(snapshot, BudgetErrorCode.OUTCOME_UNKNOWN)
        if snapshot.active:
            return self._denied(snapshot, BudgetErrorCode.CALL_IN_FLIGHT)
        if now >= snapshot.deadline_at:
            return self._denied(snapshot, BudgetErrorCode.DEADLINE_EXCEEDED)
        if snapshot.steps_used + count > snapshot.max_steps:
            return self._denied(snapshot, BudgetErrorCode.MAX_STEPS_EXCEEDED)
        if enforce_cost and snapshot.max_cost_microusd is not None:
            if snapshot.cost_unknown:
                return self._denied(snapshot, BudgetErrorCode.USAGE_UNKNOWN)
            if snapshot.cost_microusd >= snapshot.max_cost_microusd:
                return self._denied(snapshot, BudgetErrorCode.MAX_COST_EXCEEDED)
        return None

    @staticmethod
    def _denied(snapshot: BudgetSnapshot, code: BudgetErrorCode) -> BudgetDecision:
        return BudgetDecision(False, snapshot, error_code=code, message=code.value)

    def settle(
        self,
        snapshot: BudgetSnapshot,
        usage: tuple[UsageRecord, ...],
        *,
        failed: bool = False,
    ) -> BudgetSnapshot:
        active = snapshot.active
        if not active or tuple(item.call_id for item in usage) != tuple(
            item.call_id for item in active
        ):
            raise ValueError("Usage must match every active reservation in order.")
        usage_by_call = {item.call_id: item for item in usage}
        final_status = CallStatus.FAILED if failed else CallStatus.COMPLETED
        reservations = tuple(
            replace(item, status=final_status)
            if item.call_id in usage_by_call
            else item
            for item in snapshot.reservations
        )
        known_cost = sum(item.cost_microusd or 0 for item in usage)
        kind_by_call = {item.call_id: item.kind for item in active}
        cost_unknown = snapshot.cost_unknown or any(
            item.cost_microusd is None
            and kind_by_call[item.call_id] is CallKind.MODEL
            for item in usage
        )
        return replace(
            snapshot,
            reservations=reservations,
            usage=tuple(snapshot.usage) + usage,
            cost_microusd=snapshot.cost_microusd + known_cost,
            cost_unknown=cost_unknown,
        )

    @staticmethod
    def mark_outcome_unknown(snapshot: BudgetSnapshot) -> BudgetSnapshot:
        if not snapshot.active:
            return snapshot
        return replace(
            snapshot,
            reservations=tuple(
                replace(item, status=CallStatus.OUTCOME_UNKNOWN)
                if item.status is CallStatus.IN_FLIGHT
                else item
                for item in snapshot.reservations
            ),
        )


def _sequence_tuple(value: object, name: str) -> tuple:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise ValueError(f"{name} must be a non-string sequence.")
    return tuple(value)
