"""Shared constructors for public RunConfig contract tests."""

import unittest
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from agent.config import ModelSettings
from agent.runtime import RunConfig, TokenPricing
from agent.verification import VerificationSpec


def model_settings(api_key: str = "secret-one") -> ModelSettings:
    return ModelSettings(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/",
        api_key=SecretStr(api_key),
    )


def verification_spec(name: str, argument: str) -> VerificationSpec:
    return VerificationSpec(
        name=name,
        argv=("python", "-m", argument),
        cwd=".",
        timeout_seconds=30,
        max_output_bytes=4096,
    )


class RunConfigTestCase(unittest.TestCase):
    def make_config(
        self,
        root: Path,
        **changes: object,
    ) -> RunConfig:
        workspace = root / "workspace"
        runtime = root / "runtime"
        workspace.mkdir(exist_ok=True)
        values = {
            "workspace_root": workspace,
            "runtime_root": runtime,
            "model": model_settings(),
            "model_max_output_tokens": 4096,
            "verification_specs": (
                verification_spec("unit", "unittest"),
                verification_spec("compile", "compileall"),
            ),
            "timeout_seconds": 900.0,
            "max_steps": 40,
            "max_cost_usd": Decimal("2.50"),
            "pricing": TokenPricing(
                input_cost_per_million_tokens=Decimal("0.25"),
                output_cost_per_million_tokens=Decimal("0.50"),
                source="deepseek-2026-09-15",
            ),
        }
        values.update(changes)
        return RunConfig(**values)
