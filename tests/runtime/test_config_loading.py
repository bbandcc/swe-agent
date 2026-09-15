import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agent.runtime import RunConfigError, RunConfigErrorCode, load_run_config
from tests.runtime._config_support import RunConfigTestCase


class RunConfigLoadingTests(RunConfigTestCase):
    def test_import_does_not_read_environment_or_create_model(self) -> None:
        environment = os.environ.copy()
        environment["AGENT_MODEL_PROVIDER"] = "invalid-at-import"
        environment["SWE_AGENT_WORKSPACE"] = "missing-at-import"

        completed = subprocess.run(
            [sys.executable, "-c", "import agent.runtime; print('import-ok')"],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "import-ok")

    def test_loads_complete_config_from_explicit_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            environment = {
                "SWE_AGENT_WORKSPACE": str(workspace),
                "SWE_AGENT_RUNTIME_ROOT": str(root / "runtime"),
                "AGENT_MODEL_PROVIDER": "deepseek",
                "AGENT_MODEL": "deepseek-v4-flash",
                "AGENT_MODEL_BASE_URL": "https://api.deepseek.com",
                "DEEPSEEK_API_KEY": "mapping-secret",
                "AGENT_MODEL_MAX_OUTPUT_TOKENS": "2048",
                "SWE_AGENT_VERIFICATION_CHECKS": json.dumps(
                    [
                        {
                            "name": "tests",
                            "argv": ["python", "-m", "unittest"],
                            "timeout_seconds": 45,
                            "max_output_bytes": 5000,
                        }
                    ]
                ),
                "SWE_AGENT_RUN_TIMEOUT_SECONDS": "600",
                "SWE_AGENT_MAX_STEPS": "30",
                "SWE_AGENT_MAX_COST_USD": "1.25",
                "AGENT_MODEL_INPUT_COST_PER_MILLION_USD": "0.10",
                "AGENT_MODEL_OUTPUT_COST_PER_MILLION_USD": "0.20",
                "AGENT_MODEL_PRICING_SOURCE": "configured-test",
            }

            config = load_run_config(environment)

            self.assertEqual(config.workspace_root, workspace.resolve())
            self.assertEqual(config.model_max_output_tokens, 2048)
            self.assertEqual(config.verification_specs[0].argv[-1], "unittest")
            self.assertEqual(config.timeout_seconds, 600)
            self.assertEqual(config.max_steps, 30)
            self.assertEqual(config.max_cost_usd, Decimal("1.25"))
            self.assertEqual(config.pricing.source, "configured-test")

    def test_rejects_partial_environment_pricing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            environment = {
                "SWE_AGENT_WORKSPACE": str(workspace),
                "SWE_AGENT_RUNTIME_ROOT": str(root / "runtime"),
                "AGENT_MODEL_INPUT_COST_PER_MILLION_USD": "0.1",
            }

            with self.assertRaises(RunConfigError) as raised:
                load_run_config(environment)

            self.assertEqual(
                raised.exception.code, RunConfigErrorCode.INVALID_VALUE
            )

    def test_loader_rejects_empty_root_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            valid = {
                "SWE_AGENT_WORKSPACE": str(workspace),
                "SWE_AGENT_RUNTIME_ROOT": str(root / "runtime"),
            }
            for name in ("SWE_AGENT_WORKSPACE", "SWE_AGENT_RUNTIME_ROOT"):
                with self.subTest(name=name):
                    environment = {**valid, name: ""}
                    with self.assertRaises(RunConfigError) as raised:
                        load_run_config(environment)
                    self.assertEqual(
                        raised.exception.code, RunConfigErrorCode.INVALID_ROOT
                    )


if __name__ == "__main__":
    unittest.main()
