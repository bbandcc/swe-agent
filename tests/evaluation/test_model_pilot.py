"""Public preparation and fail-closed execution; never call a provider."""

import json
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.evaluation.model_pilot import prepare_trial, execute_trial
from scripts.pilot_model import main


class ModelPilotPreparationTests(unittest.TestCase):
    def test_check_reads_an_explicit_external_env_without_copying_or_exposing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            config = Path(directory) / "owner.env.local"
            content = "DEEPSEEK_API_KEY=never-send-this-external-canary\n"
            config.write_text(content, encoding="utf-8")
            prepare_trial(root)
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
                code = main(["--output", str(root), "--check", "--env-file", str(config)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["not_started_reason"],
                             "CHECK_ONLY_NO_PAID_DISPATCH")
            self.assertEqual(config.read_text(encoding="utf-8"), content)
            self.assertNotIn("external-canary", output.getvalue())
            self.assertFalse((root / ".env.local").exists())

    def test_missing_explicit_env_is_rejected_before_trial_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--output", str(root), "--env-file",
                             str(Path(directory) / "missing.env")])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue()),
                             {"status": "REJECTED", "reason": "FileNotFoundError"})
            self.assertFalse(root.exists())

    def test_process_environment_can_disable_a_file_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            config = Path(directory) / "owner.env.local"
            config.write_text("DEEPSEEK_API_KEY=never-send-this-canary\n", encoding="utf-8")
            prepare_trial(root)
            output = io.StringIO()
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=True), redirect_stdout(output):
                code = main(["--output", str(root), "--check", "--env-file", str(config)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["not_started_reason"], "MISSING_CREDENTIAL")

    def test_prepare_freezes_one_trial_without_a_secret_or_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            result = prepare_trial(root)
            self.assertEqual(result["status"], "PREPARED")
            self.assertEqual(result["scheduled"], 1)
            self.assertEqual(result["started"], 0)
            self.assertEqual(result["model"]["model"], "deepseek-flash")
            self.assertEqual(result["budget"], {"currency": "CNY", "hard_limit": "1"})
            self.assertIn("helpers/prompts.py", result["source_pins"])
            self.assertIn("scripts/pilot_model.py", result["source_pins"])
            self.assertEqual([p.name for p in (root / "G").iterdir()], ["main.py"])
            self.assertNotIn("api_key", (root / "plan.json").read_text(encoding="utf-8").lower())

    def test_execution_records_not_started_without_a_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            prepare_trial(root)
            result = execute_trial(root, environ={})
            self.assertEqual(result["not_started_reason"], "MISSING_CREDENTIAL")
            self.assertEqual(result["started"], 0)
            self.assertIsNone(result["cost_cny"])

    def test_one_yuan_hard_cap_cannot_be_bypassed_by_a_present_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            prepare_trial(root)
            result = execute_trial(root, environ={"DEEPSEEK_API_KEY": "never-send-this-canary"})
            self.assertEqual(result["not_started_reason"], "CHECK_ONLY_NO_PAID_DISPATCH")
            self.assertFalse(result["resolved"])
            self.assertNotIn("never-send", (root / "execution.json").read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                execute_trial(root, environ={})

    def test_duplicate_or_mutated_trial_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            prepare_trial(root)
            with self.assertRaises(FileExistsError):
                prepare_trial(root)
            (root / "G" / "main.py").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                execute_trial(root, environ={})
            self.assertFalse((root / "execution.json").exists())

    def test_invalid_limits_fail_before_creating_a_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            for options in ({"timeout_seconds": float("nan")}, {"timeout_seconds": 0},
                            {"max_steps": True}, {"max_steps": 101}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    prepare_trial(root, **options)
                self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
