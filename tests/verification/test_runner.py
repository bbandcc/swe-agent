import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent.verification.runner as runner_module
from agent.verification import (
    VerificationCheckStatus,
    VerificationRunner,
    VerificationSpec,
)
from agent.verification.report import VerificationReportError


class VerificationRunnerTests(unittest.TestCase):
    @staticmethod
    def _pytest_spec() -> VerificationSpec:
        return VerificationSpec(
            name="pytest-report",
            argv=(
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--junitxml=report.xml",
            ),
            report_path="report.xml",
        )

    def test_spec_rejects_non_finite_or_non_positive_timeout(self) -> None:
        for timeout in (math.nan, math.inf, -math.inf, 0, -1):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "finite positive"):
                    VerificationSpec(
                        name="invalid-timeout",
                        argv=(sys.executable, "-c", "pass"),
                        timeout_seconds=timeout,
                    )

    def test_reports_pass_and_bounded_failure_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = VerificationRunner(root)
            passed = runner.run(
                VerificationSpec(
                    name="pass",
                    argv=(sys.executable, "-c", "print('ok')"),
                )
            )
            failed = runner.run(
                VerificationSpec(
                    name="fail",
                    argv=(
                        sys.executable,
                        "-c",
                        "import sys; print('A' * 400); "
                        "print('B' * 400 + 'TAIL', file=sys.stderr); "
                        "sys.exit(7)",
                    ),
                    max_output_bytes=128,
                )
            )

            self.assertEqual(passed.status, VerificationCheckStatus.PASS)
            self.assertEqual(passed.exit_code, 0)
            self.assertEqual(passed.stdout.splitlines(), ["ok"])
            self.assertFalse(passed.stdout_truncated)
            self.assertIsNone(passed.failure_id)
            self.assertEqual(failed.status, VerificationCheckStatus.FAIL)
            self.assertEqual(failed.exit_code, 7)
            self.assertTrue(failed.stdout_truncated)
            self.assertTrue(failed.stderr_truncated)
            self.assertLessEqual(
                len(failed.stdout.encode("utf-8")), 128
            )
            self.assertLessEqual(
                len(failed.stderr.encode("utf-8")), 128
            )
            self.assertIn("truncated", failed.stdout)
            self.assertIn("TAIL", failed.stderr)
            self.assertIsNotNone(failed.failure_id)

    def test_reports_timeout_without_treating_it_as_test_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = getattr(sys, "_base_executable", sys.executable)
            result = VerificationRunner(directory).run(
                VerificationSpec(
                    name="slow",
                    argv=(
                        executable,
                        "-c",
                        "import time; print('started', flush=True); time.sleep(5)",
                    ),
                    timeout_seconds=0.2,
                )
            )

            self.assertEqual(result.status, VerificationCheckStatus.TIMEOUT)
            self.assertIsNone(result.exit_code)
            self.assertIn("started", result.stdout)
            self.assertIn("timed out", result.message.lower())

    def test_timeout_terminates_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = getattr(sys, "_base_executable", sys.executable)
            child_code = (
                "import time; from pathlib import Path; time.sleep(0.8); "
                "Path('child-survived.txt').write_text('alive')"
            )
            parent_code = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(5)"
            )

            result = VerificationRunner(root).run(
                VerificationSpec(
                    name="process-tree",
                    argv=(executable, "-c", parent_code),
                    timeout_seconds=0.1,
                )
            )
            time.sleep(1)

            self.assertEqual(result.status, VerificationCheckStatus.TIMEOUT)
            self.assertLess(result.duration_seconds, 4)
            self.assertFalse((root / "child-survived.txt").exists())

    def test_reports_process_start_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = VerificationRunner(directory).run(
                VerificationSpec(
                    name="missing",
                    argv=("command-that-does-not-exist-s2",),
                )
            )

            self.assertEqual(
                result.status, VerificationCheckStatus.EXECUTION_ERROR
            )
            self.assertIsNone(result.exit_code)
            self.assertIn("could not start", result.message.lower())

    def test_rejects_non_pytest_junit_producer_at_runner_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = VerificationRunner(directory).run(
                VerificationSpec(
                    name="untrusted-report",
                    argv=(sys.executable, "-c", "pass"),
                    report_path="report.xml",
                )
            )

            self.assertEqual(result.status, VerificationCheckStatus.EXECUTION_ERROR)
            self.assertIn("pytest --junitxml", result.message)

    def test_nonexistent_report_path_is_removed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_report.py").write_text(
                "def test_report():\n    assert True\n",
                encoding="utf-8",
                newline="",
            )

            result = VerificationRunner(root).run(self._pytest_spec())

            self.assertEqual(result.status, VerificationCheckStatus.PASS)
            self.assertFalse((root / "report.xml").exists())

    def test_stale_workspace_report_is_not_reused_as_new_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.xml"
            report.write_text("stale", encoding="utf-8")
            spec = VerificationSpec(
                name="pytest-help",
                argv=(
                    sys.executable,
                    "-m",
                    "pytest",
                    "--junitxml=report.xml",
                    "--help",
                ),
                report_path="report.xml",
            )

            result = VerificationRunner(root).run(spec)

            self.assertEqual(result.status, VerificationCheckStatus.PASS)
            self.assertIsNone(result.report)
            self.assertIn("missing", result.message)
            self.assertEqual(report.read_text(encoding="utf-8"), "stale")

    def test_malformed_junit_report_is_evidence_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_report.py").write_text(
                "def test_report():\n    assert True\n",
                encoding="utf-8",
                newline="",
            )
            with patch.object(
                runner_module,
                "parse_junit_xml",
                side_effect=VerificationReportError("malformed report"),
            ):
                result = VerificationRunner(root).run(self._pytest_spec())

            self.assertEqual(result.status, VerificationCheckStatus.PASS)
            self.assertIsNone(result.report)
            self.assertIn("invalid", result.message.lower())

    def test_restore_failure_is_structured_execution_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.xml").write_text("original", encoding="utf-8")
            (root / "test_report.py").write_text(
                "def test_report():\n    assert True\n",
                encoding="utf-8",
                newline="",
            )
            real_copy2 = runner_module.shutil.copy2
            copy_count = 0

            def fail_restore(source: Path, target: Path, *args: object, **kwargs: object):
                nonlocal copy_count
                copy_count += 1
                if copy_count == 2:
                    raise OSError("restore failed")
                return real_copy2(source, target, *args, **kwargs)

            with patch.object(runner_module.shutil, "copy2", side_effect=fail_restore):
                result = VerificationRunner(root).run(self._pytest_spec())

            self.assertEqual(result.status, VerificationCheckStatus.EXECUTION_ERROR)
            self.assertIn("restore", result.message.lower())

    def test_cleanup_failure_is_structured_execution_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_report.py").write_text(
                "def test_report():\n    assert True\n",
                encoding="utf-8",
                newline="",
            )
            with patch.object(
                runner_module.shutil,
                "rmtree",
                side_effect=OSError("cleanup failed"),
            ):
                result = VerificationRunner(root).run(self._pytest_spec())

            self.assertEqual(result.status, VerificationCheckStatus.EXECUTION_ERROR)
            self.assertIn("cleanup", result.message.lower())

    def test_interrupt_propagates_after_report_restore(self) -> None:
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    report = root / "report.xml"
                    report.write_text("original", encoding="utf-8")
                    with patch.object(
                        runner_module.subprocess,
                        "Popen",
                        side_effect=interruption,
                    ):
                        with self.assertRaises(interruption):
                            VerificationRunner(root).run(self._pytest_spec())

                    self.assertEqual(report.read_text(encoding="utf-8"), "original")

    def test_does_not_interpret_a_shell_command_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = VerificationRunner(root).run(
                VerificationSpec(
                    name="no-shell",
                    argv=("echo injected > marker.txt",),
                )
            )

            self.assertEqual(
                result.status, VerificationCheckStatus.EXECUTION_ERROR
            )
            self.assertFalse((root / "marker.txt").exists())

    def test_failure_identity_uses_output_hidden_by_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VerificationRunner(directory)
            results = [
                runner.run(
                    VerificationSpec(
                        name="bounded-failure",
                        argv=(
                            sys.executable,
                            "-c",
                            "import sys; "
                            f"print('H' * 100 + '{middle}' * 100 + 'T' * 100); "
                            "sys.exit(1)",
                        ),
                        max_output_bytes=96,
                    )
                )
                for middle in ("A", "B")
            ]

            self.assertEqual(results[0].stdout, results[1].stdout)
            self.assertNotEqual(results[0].failure_id, results[1].failure_id)

    def test_rejects_working_directory_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            result = VerificationRunner(root).run(
                VerificationSpec(
                    name="escape",
                    argv=(sys.executable, "-c", "print('must not run')"),
                    cwd="..",
                )
            )

            self.assertEqual(
                result.status, VerificationCheckStatus.EXECUTION_ERROR
            )
            self.assertIn("workspace", result.message.lower())
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
