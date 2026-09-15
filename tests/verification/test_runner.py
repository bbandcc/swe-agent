import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent.verification import (
    VerificationCheckStatus,
    VerificationRunner,
    VerificationSpec,
)


class VerificationRunnerTests(unittest.TestCase):
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
