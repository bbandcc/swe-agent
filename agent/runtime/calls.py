"""Small model-result seam that captures usage before output parsing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Generic, TypeVar

from langchain_core.messages import AIMessage

from agent.runtime.budget import BudgetErrorCode, UsageRecord, UsageStatus
from agent.runtime.config import TokenPricing

T = TypeVar("T")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class UsageMeasurement:
    status: UsageStatus
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_microusd: int | None = None
    cost_source: str | None = None

    def for_call(self, call_id: str) -> UsageRecord:
        return UsageRecord(
            call_id=call_id,
            status=self.status,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            cost_microusd=self.cost_microusd,
            cost_source=self.cost_source,
        )


@dataclass(frozen=True, slots=True)
class ModelCallResult(Generic[T]):
    value: T | None
    usage: UsageMeasurement
    response_digest: str
    error: str | None = None
    error_code: BudgetErrorCode | None = None

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.response_digest):
            raise ValueError("response_digest must be a SHA-256 hex digest.")
        if self.error is not None and not self.error.strip():
            raise ValueError("error must be non-empty or None.")
        if self.error_code is not None and not isinstance(
            self.error_code, BudgetErrorCode
        ):
            raise ValueError("error_code must be BudgetErrorCode or None.")


def capture_model_result(
    value: T,
    raw_message: AIMessage,
    pricing: TokenPricing | None,
) -> ModelCallResult[T]:
    """Keep parsed value plus bounded accounting metadata, never raw content."""
    return ModelCallResult(
        value=value,
        usage=measure_usage(raw_message, pricing),
        response_digest=_response_digest(raw_message),
    )


def capture_model_failure(
    raw_message: AIMessage,
    pricing: TokenPricing | None,
    error: object,
) -> ModelCallResult[None]:
    """Persist usage/digest for a returned response whose parsing failed."""
    return ModelCallResult(
        value=None,
        usage=measure_usage(raw_message, pricing),
        response_digest=_response_digest(raw_message),
        error=str(error) or type(error).__name__,
        error_code=BudgetErrorCode.MODEL_OUTPUT_INVALID,
    )


def capture_model_exception(
    error_code: BudgetErrorCode,
) -> ModelCallResult[None]:
    """Convert a known transport boundary failure into safe durable metadata."""
    if error_code not in {
        BudgetErrorCode.MODEL_REQUEST_TIMEOUT,
        BudgetErrorCode.MODEL_TRANSPORT_ERROR,
    }:
        raise ValueError("error_code must identify a model transport failure.")
    response_digest = hashlib.sha256(
        error_code.value.encode("utf-8")
    ).hexdigest()
    return ModelCallResult(
        value=None,
        usage=UsageMeasurement(UsageStatus.UNKNOWN),
        response_digest=response_digest,
        error=error_code.value,
        error_code=error_code,
    )


def classify_model_exception(error: BaseException) -> BudgetErrorCode | None:
    """Classify only provider timeout/transport failures; let other errors rise."""
    import anthropic
    import httpx
    import openai

    if isinstance(
        error,
        (
            TimeoutError,
            httpx.TimeoutException,
            openai.APITimeoutError,
            anthropic.APITimeoutError,
        ),
    ):
        return BudgetErrorCode.MODEL_REQUEST_TIMEOUT
    if isinstance(
        error,
        (
            httpx.TransportError,
            openai.APIConnectionError,
            anthropic.APIConnectionError,
        ),
    ):
        return BudgetErrorCode.MODEL_TRANSPORT_ERROR
    return None


def measure_usage(
    message: AIMessage,
    pricing: TokenPricing | None,
) -> UsageMeasurement:
    metadata = message.usage_metadata or {}
    input_tokens = _token_value(metadata.get("input_tokens"))
    output_tokens = _token_value(metadata.get("output_tokens"))
    total_tokens = _token_value(metadata.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return UsageMeasurement(UsageStatus.UNKNOWN)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return UsageMeasurement(
            UsageStatus.PARTIAL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    if pricing is None:
        return UsageMeasurement(
            UsageStatus.PARTIAL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    cost = (
        Decimal(input_tokens) * pricing.input_cost_per_million_tokens
        + Decimal(output_tokens) * pricing.output_cost_per_million_tokens
    )
    return UsageMeasurement(
        UsageStatus.KNOWN,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_microusd=int(cost.to_integral_value(rounding=ROUND_CEILING)),
        cost_source="configured_estimate",
    )


def _token_value(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _response_digest(message: AIMessage) -> str:
    bounded_identity = {
        "id": message.id,
        "tool_call_ids": [call.get("id") for call in message.tool_calls],
        "content": message.content,
    }
    encoded = json.dumps(
        bounded_identity, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
