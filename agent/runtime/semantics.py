"""Versioned, secret-free semantic binding for runtime configuration."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from agent.runtime.config import RunConfig

# Version 5 binds durable pending-write intent/reconciliation semantics to
# checkpoint identity. A checkpoint from an earlier schema cannot be resumed
# under a state model that may contain incomplete file writes. Future semantic
# field or encoding changes must increment this version again.
SEMANTIC_CONFIG_SCHEMA_VERSION = 5


def semantic_config_digest(config: RunConfig) -> str:
    """Hash the versioned canonical JSON semantics of a run config."""
    encoded = json.dumps(
        _semantic_config_payload(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_config_payload(config: RunConfig) -> dict[str, Any]:
    pricing = None
    if config.pricing is not None:
        pricing = {
            "input_cost_per_million_tokens": _decimal_text(
                config.pricing.input_cost_per_million_tokens
            ),
            "output_cost_per_million_tokens": _decimal_text(
                config.pricing.output_cost_per_million_tokens
            ),
            "source": config.pricing.source,
        }
    return {
        "schema_version": SEMANTIC_CONFIG_SCHEMA_VERSION,
        "model": {
            "provider": config.model.provider,
            "model": config.model.model,
            "base_url": config.model.base_url,
            "max_output_tokens": config.model_max_output_tokens,
            "request_timeout_seconds": _number_text(
                config.model_request_timeout_seconds
            ),
            "retry_policy": {
                "max_attempts": config.model_retry_policy.max_attempts,
            },
        },
        "verification_specs": [
            {
                "name": spec.name,
                "argv": list(spec.argv),
                "cwd": spec.cwd,
                "timeout_seconds": _number_text(spec.timeout_seconds),
                "max_output_bytes": spec.max_output_bytes,
                "report_path": spec.report_path,
                "allowed_failure_case_ids": list(spec.allowed_failure_case_ids),
            }
            for spec in config.verification_specs
        ],
        "limits": {
            "timeout_seconds": _number_text(config.timeout_seconds),
            "max_steps": config.max_steps,
            "max_cost_usd": (
                _decimal_text(config.max_cost_usd)
                if config.max_cost_usd is not None
                else None
            ),
        },
        "pricing": pricing,
        "workspace_access_policy": config.access_policy.to_digest_dict(),
    }


def _number_text(value: int | float) -> str:
    return _decimal_text(Decimal(str(value)))


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")
