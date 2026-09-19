"""Load trusted verification command configuration from the environment."""

import json
import math
import os
from collections.abc import Mapping
from typing import Any

from agent.verification.contracts import VerificationSpec

VERIFICATION_CHECKS_ENV = "SWE_AGENT_VERIFICATION_CHECKS"
_ALLOWED_KEYS = {
    "name",
    "argv",
    "cwd",
    "timeout_seconds",
    "max_output_bytes",
    "report_path",
    "allowed_failure_case_ids",
}


def configured_verification_specs(
    environ: Mapping[str, str] | None = None,
) -> tuple[VerificationSpec, ...]:
    """Parse configured argv checks; an absent value deliberately means none."""
    source = os.environ if environ is None else environ
    raw = source.get(VERIFICATION_CHECKS_ENV, "").strip()
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{VERIFICATION_CHECKS_ENV} must be valid JSON: {error.msg}."
        ) from error
    if not isinstance(values, list):
        raise ValueError(f"{VERIFICATION_CHECKS_ENV} must be a JSON list.")
    return tuple(_parse_spec(value, index) for index, value in enumerate(values))


def _parse_spec(value: Any, index: int) -> VerificationSpec:
    label = f"{VERIFICATION_CHECKS_ENV}[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    unknown = set(value) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"{label} contains unknown keys: {sorted(unknown)}.")

    name = value.get("name")
    argv = value.get("argv")
    cwd = value.get("cwd", ".")
    timeout = value.get("timeout_seconds", 60.0)
    output_limit = value.get("max_output_bytes", 20_000)
    report_path = value.get("report_path")
    allowed_failure_case_ids = value.get("allowed_failure_case_ids", [])
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label}.name must be a non-empty string.")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise ValueError(f"{label}.argv must be a non-empty JSON string array.")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError(f"{label}.cwd must be a non-empty string.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError(f"{label}.timeout_seconds must be a number.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            f"{label}.timeout_seconds must be a finite positive number."
        )
    if isinstance(output_limit, bool) or not isinstance(output_limit, int):
        raise ValueError(f"{label}.max_output_bytes must be an integer.")
    if report_path is not None and (
        not isinstance(report_path, str) or not report_path.strip()
    ):
        raise ValueError(f"{label}.report_path must be a non-empty string or null.")
    if (
        not isinstance(allowed_failure_case_ids, list)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in allowed_failure_case_ids
        )
        or len(set(allowed_failure_case_ids)) != len(allowed_failure_case_ids)
    ):
        raise ValueError(
            f"{label}.allowed_failure_case_ids must be a unique string array."
        )
    return VerificationSpec(
        name=name,
        argv=tuple(argv),
        cwd=cwd,
        timeout_seconds=float(timeout),
        max_output_bytes=output_limit,
        report_path=report_path,
        allowed_failure_case_ids=tuple(allowed_failure_case_ids),
    )
