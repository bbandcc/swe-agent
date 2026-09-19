"""Validated, secret-safe configuration for one agent run."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import SecretStr

from agent.config import ModelSettings, model_settings
from agent.verification import VerificationSpec
from agent.verification.config import configured_verification_specs
from agent.workspace import (
    WorkspaceRootError,
    canonical_path_key,
    canonicalize_root_path,
)

DEFAULT_RUNTIME_ROOT = "./.swe-agent-runtime"
DEFAULT_MODEL_MAX_OUTPUT_TOKENS = 4096
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
DEFAULT_MAX_STEPS = 100


class RunConfigErrorCode(str, Enum):
    INVALID_VALUE = "invalid_value"
    INVALID_ROOT = "invalid_root"
    ROOTS_OVERLAP = "roots_overlap"
    INVALID_BASE_URL = "invalid_base_url"


class RunConfigError(ValueError):
    """Structured configuration validation failure."""

    def __init__(self, code: RunConfigErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TokenPricing:
    input_cost_per_million_tokens: Decimal
    output_cost_per_million_tokens: Decimal
    source: str

    def __post_init__(self) -> None:
        _require_positive_decimal(
            self.input_cost_per_million_tokens,
            "input token pricing",
        )
        _require_positive_decimal(
            self.output_cost_per_million_tokens,
            "output token pricing",
        )
        if not isinstance(self.source, str) or not self.source.strip():
            raise RunConfigError(
                RunConfigErrorCode.INVALID_VALUE,
                "Token pricing source must be a non-empty string.",
            )
        object.__setattr__(self, "source", self.source.strip())


@dataclass(frozen=True, slots=True)
class RunConfig:
    workspace_root: Path
    runtime_root: Path
    model: ModelSettings
    model_max_output_tokens: int
    verification_specs: tuple[VerificationSpec, ...]
    timeout_seconds: float
    max_steps: int
    max_cost_usd: Decimal | None = None
    pricing: TokenPricing | None = None

    def __post_init__(self) -> None:
        workspace = _validated_root(self.workspace_root, must_exist=True)
        runtime = _validated_root(self.runtime_root, must_exist=False)
        if _roots_overlap(workspace, runtime):
            raise RunConfigError(
                RunConfigErrorCode.ROOTS_OVERLAP,
                "workspace_root and runtime_root must be disjoint.",
            )

        model = _validate_model(self.model)
        _require_positive_integer(
            self.model_max_output_tokens,
            "model_max_output_tokens",
        )
        _require_positive_number(self.timeout_seconds, "timeout_seconds")
        _require_positive_integer(self.max_steps, "max_steps")
        if self.max_cost_usd is not None:
            _require_positive_decimal(self.max_cost_usd, "max_cost_usd")
        if self.pricing is not None and not isinstance(
            self.pricing, TokenPricing
        ):
            raise RunConfigError(
                RunConfigErrorCode.INVALID_VALUE,
                "pricing must be TokenPricing or None.",
            )

        try:
            configured_specs = tuple(self.verification_specs)
        except TypeError as error:
            raise RunConfigError(
                RunConfigErrorCode.INVALID_VALUE,
                "verification_specs must be an iterable of specifications.",
            ) from error
        specs: list[VerificationSpec] = []
        for spec in configured_specs:
            _validate_verification_spec(spec)
            specs.append(replace(spec, argv=tuple(spec.argv)))

        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "runtime_root", runtime)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "verification_specs", tuple(specs))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


def load_run_config(environ: Mapping[str, str]) -> RunConfig:
    """Parse one complete run config from an explicit mapping."""
    try:
        configured_model = model_settings(environ)
        verification_specs = configured_verification_specs(environ)
        output_tokens = _parse_integer(
            environ.get(
                "AGENT_MODEL_MAX_OUTPUT_TOKENS",
                str(DEFAULT_MODEL_MAX_OUTPUT_TOKENS),
            ),
            "AGENT_MODEL_MAX_OUTPUT_TOKENS",
        )
        timeout = _parse_float(
            environ.get(
                "SWE_AGENT_RUN_TIMEOUT_SECONDS",
                str(DEFAULT_RUN_TIMEOUT_SECONDS),
            ),
            "SWE_AGENT_RUN_TIMEOUT_SECONDS",
        )
        max_steps = _parse_integer(
            environ.get("SWE_AGENT_MAX_STEPS", str(DEFAULT_MAX_STEPS)),
            "SWE_AGENT_MAX_STEPS",
        )
        max_cost = _parse_optional_decimal(
            environ.get("SWE_AGENT_MAX_COST_USD", ""),
            "SWE_AGENT_MAX_COST_USD",
        )
        pricing = _load_pricing(environ)
    except RunConfigError:
        raise
    except (TypeError, ValueError) as error:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            str(error),
        ) from error

    return RunConfig(
        workspace_root=_configured_root(
            environ, "SWE_AGENT_WORKSPACE", "./workspace_repo"
        ),
        runtime_root=_configured_root(
            environ, "SWE_AGENT_RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT
        ),
        model=configured_model,
        model_max_output_tokens=output_tokens,
        verification_specs=verification_specs,
        timeout_seconds=timeout,
        max_steps=max_steps,
        max_cost_usd=max_cost,
        pricing=pricing,
    )


def _validate_model(settings: ModelSettings) -> ModelSettings:
    if not isinstance(settings, ModelSettings):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "model must be ModelSettings.",
        )
    if settings.provider not in {"deepseek", "anthropic"}:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Model provider must be deepseek or anthropic.",
        )
    if settings.api_key is not None and not isinstance(
        settings.api_key, SecretStr
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Model API key must use SecretStr.",
        )
    model_name = settings.model.strip() if isinstance(settings.model, str) else ""
    if not model_name:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Model name must be a non-empty string.",
        )
    base_url = settings.base_url
    if settings.provider == "deepseek" and not base_url:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_BASE_URL,
            "DeepSeek requires a base URL.",
        )
    normalized_url = _normalize_base_url(base_url) if base_url else None
    return replace(settings, model=model_name, base_url=normalized_url)


def _normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(
        character.isspace() for character in value
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_BASE_URL,
            "Model base URL is invalid.",
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_BASE_URL,
            "Model base URL is invalid.",
        ) from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_BASE_URL,
            "Model base URL must not contain credentials, query, or fragment.",
        )
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _validated_root(value: str | Path, *, must_exist: bool) -> Path:
    try:
        return canonicalize_root_path(value, must_exist=must_exist)
    except WorkspaceRootError as error:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_ROOT,
            str(error),
        ) from error


def _roots_overlap(first: Path, second: Path) -> bool:
    first_key = canonical_path_key(first)
    second_key = canonical_path_key(second)
    try:
        common = os.path.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in {first_key, second_key}


def _validate_verification_spec(spec: VerificationSpec) -> None:
    if not isinstance(spec, VerificationSpec):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "verification_specs must contain VerificationSpec values.",
        )
    if (
        not isinstance(spec.name, str)
        or not spec.name.strip()
        or not isinstance(spec.cwd, str)
        or not spec.cwd
        or "\x00" in spec.cwd
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Verification name and cwd must be valid non-empty strings.",
        )
    if not isinstance(spec.argv, (tuple, list)) or not spec.argv or any(
        not isinstance(item, str) or not item or "\x00" in item
        for item in spec.argv
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Verification argv must contain non-empty strings.",
        )
    _require_positive_number(
        spec.timeout_seconds,
        "verification timeout_seconds",
    )
    _require_positive_integer(spec.max_output_bytes, "max_output_bytes")
    if spec.report_path is not None and (
        not isinstance(spec.report_path, str)
        or not spec.report_path.strip()
        or "\x00" in spec.report_path
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Verification report_path must be a valid path.",
        )
    if any(
        not isinstance(item, str) or not item.strip()
        for item in spec.allowed_failure_case_ids
    ) or len(set(spec.allowed_failure_case_ids)) != len(
        spec.allowed_failure_case_ids
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Verification allowed failure case IDs must be unique strings.",
        )


def _load_pricing(environ: Mapping[str, str]) -> TokenPricing | None:
    names = (
        "AGENT_MODEL_INPUT_COST_PER_MILLION_USD",
        "AGENT_MODEL_OUTPUT_COST_PER_MILLION_USD",
        "AGENT_MODEL_PRICING_SOURCE",
    )
    values = tuple(str(environ.get(name, "")).strip() for name in names)
    if not any(values):
        return None
    if not all(values):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            "Model pricing requires input, output, and source values.",
        )
    return TokenPricing(
        _parse_decimal(values[0], names[0]),
        _parse_decimal(values[1], names[1]),
        values[2],
    )


def _parse_integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            f"{name} must be an integer.",
        )
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            f"{name} must be an integer.",
        ) from error
    return parsed


def _parse_float(value: Any, name: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as error:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            f"{name} must be a number.",
        ) from error


def _parse_optional_decimal(value: Any, name: str) -> Decimal | None:
    text = str(value).strip()
    return _parse_decimal(text, name) if text else None


def _parse_decimal(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            f"{name} must be a decimal number.",
        ) from error


def _require_positive_integer(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            f"{name} must be a positive integer.",
        )


def _require_positive_number(value: Any, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            f"{name} must be a finite positive number.",
        )


def _require_positive_decimal(value: Any, name: str) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value <= 0
    ):
        raise RunConfigError(
            RunConfigErrorCode.INVALID_VALUE,
            f"{name} must be a finite positive Decimal.",
        )


def _configured_root(
    environ: Mapping[str, str], name: str, default: str
) -> Path:
    value = environ.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise RunConfigError(
            RunConfigErrorCode.INVALID_ROOT,
            f"{name} must be a non-empty path.",
        )
    return Path(value)
