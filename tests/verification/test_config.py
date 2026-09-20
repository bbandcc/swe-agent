import json
import math
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from agent.common.entities import ImplementationPlan, PlanStatus
from agent.developer.state import DeveloperStatus
from agent.graph import (
    WorkflowOutcome,
    create_production_workflow_graph,
)
from agent.verification import VerificationRunner, VerificationStatus
from agent.verification.config import (
    VERIFICATION_CHECKS_ENV,
    configured_verification_specs,
)


class VerificationConfigurationTests(unittest.TestCase):
    def test_missing_configuration_produces_no_checks(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_verification_specs(), ())

    def test_loads_argv_checks_from_external_json_configuration(self) -> None:
        value = json.dumps(
            [
                {
                    "name": "unit",
                    "argv": ["python", "-m", "unittest"],
                    "cwd": ".",
                    "timeout_seconds": 30,
                    "max_output_bytes": 4096,
                }
            ]
        )
        with patch.dict(os.environ, {VERIFICATION_CHECKS_ENV: value}):
            specs = configured_verification_specs()

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].argv, ("python", "-m", "unittest"))
        self.assertEqual(specs[0].timeout_seconds, 30)

    def test_loads_structured_report_and_allowed_failures(self) -> None:
        value = json.dumps(
            [
                {
                    "name": "unit",
                    "argv": ["pytest", "--junitxml=report.xml"],
                    "report_path": "report.xml",
                    "allowed_failure_case_ids": ["legacy"],
                }
            ]
        )
        with patch.dict(os.environ, {VERIFICATION_CHECKS_ENV: value}):
            spec = configured_verification_specs()[0]

        self.assertEqual(spec.report_path, "report.xml")
        self.assertEqual(spec.allowed_failure_case_ids, ("legacy",))

    def test_rejects_shell_command_string_configuration(self) -> None:
        value = json.dumps(
            [{"name": "unsafe", "argv": "pytest && remove-everything"}]
        )
        with patch.dict(os.environ, {VERIFICATION_CHECKS_ENV: value}):
            with self.assertRaisesRegex(ValueError, "argv"):
                configured_verification_specs()

    def test_rejects_duplicate_check_names(self) -> None:
        value = json.dumps(
            [
                {"name": "unit", "argv": ["python", "-m", "unittest"]},
                {"name": "unit", "argv": ["python", "-m", "compileall"]},
            ]
        )
        with patch.dict(os.environ, {VERIFICATION_CHECKS_ENV: value}):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                configured_verification_specs()

    def test_rejects_non_pytest_junit_producer(self) -> None:
        value = json.dumps(
            [
                {
                    "name": "unsafe-report",
                    "argv": [sys.executable, "-c", "pass"],
                    "report_path": "report.xml",
                }
            ]
        )
        with patch.dict(os.environ, {VERIFICATION_CHECKS_ENV: value}):
            with self.assertRaisesRegex(ValueError, "pytest --junitxml"):
                configured_verification_specs()

    def test_rejects_invalid_timeout_from_environment(self) -> None:
        for timeout in (math.nan, math.inf, -math.inf, 0, -1):
            with self.subTest(timeout=timeout):
                value = json.dumps(
                    [
                        {
                            "name": "invalid",
                            "argv": ["test"],
                            "timeout_seconds": timeout,
                        }
                    ]
                )
                with patch.dict(os.environ, {VERIFICATION_CHECKS_ENV: value}):
                    with self.assertRaisesRegex(ValueError, "finite positive"):
                        configured_verification_specs()

    def test_env_example_is_valid_dotenv_and_json(self) -> None:
        example = Path(".env.example").read_text(encoding="utf-8")
        assignment = next(
            line.removeprefix("# ")
            for line in example.splitlines()
            if line.startswith(f"# {VERIFICATION_CHECKS_ENV}=")
        )
        configured_value = assignment.split("=", 1)[1]
        self.assertTrue(configured_value.startswith("'"))
        self.assertTrue(configured_value.endswith("'"))
        values = dotenv_values(stream=StringIO(assignment))

        specs = configured_verification_specs(values)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].argv, ("python", "-m", "unittest"))

    def test_production_graph_executes_configured_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_configured.py").write_text(
                "def test_configured_check():\n    assert True\n",
                encoding="utf-8",
                newline="",
            )
            value = json.dumps(
                [
                    {
                        "name": "production-check",
                        "argv": [
                            os.fspath(Path(sys.executable)),
                            "-m",
                            "pytest",
                            "-q",
                            "--junitxml=report.xml",
                        ],
                        "report_path": "report.xml",
                    }
                ]
            )
            no_changes = ImplementationPlan(
                status=PlanStatus.NO_CHANGES,
                no_change_reason="repository already satisfies the request",
                tasks=[],
            )
            with patch.dict(os.environ, {VERIFICATION_CHECKS_ENV: value}):
                result = create_production_workflow_graph(
                    architect=lambda _: {"implementation_plan": no_changes},
                    developer=lambda _: {
                        "developer_status": DeveloperStatus.NO_CHANGES
                    },
                    verification_runner=VerificationRunner(root),
                ).compile().invoke({})

            self.assertEqual(len(result["baseline_verification"]), 1)
            self.assertEqual(len(result["post_verification"]), 1)
            self.assertEqual(
                result["verification_status"], VerificationStatus.VERIFIED
            )
            self.assertEqual(result["outcome"], WorkflowOutcome.NO_CHANGES)


if __name__ == "__main__":
    unittest.main()
