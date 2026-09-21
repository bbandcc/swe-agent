import io
import hashlib
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent.verification.runner as runner_module
from agent.artifacts import ArtifactErrorCode, ArtifactStore
from agent.runtime import KnownSecretFilter, REDACTION_MARKER
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

    def test_streamed_artifacts_are_redacted_complete_and_hashed(self) -> None:
        canary = "S3_STREAM_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            executable = getattr(sys, "_base_executable", sys.executable)
            code = (
                "import sys,time; "
                f"sys.stdout.buffer.write(b'prefix {canary[:7]}'); sys.stdout.flush(); "
                "time.sleep(0.03); "
                f"sys.stdout.buffer.write(b'{canary[7:]} ' + b'X' * 400); "
                "sys.stdout.flush(); "
                f"sys.stderr.buffer.write(b'error {canary[:4]}'); sys.stderr.flush(); "
                "time.sleep(0.03); "
                f"sys.stderr.buffer.write(b'{canary[4:]} ' + b'Y' * 400); "
                "sys.stderr.flush()"
            )
            result = VerificationRunner(
                root,
                runtime_root=runtime_root,
                secret_filter=KnownSecretFilter((canary,)),
            ).run(
                VerificationSpec(
                    name="artifact-output",
                    argv=(executable, "-c", code),
                    max_output_bytes=128,
                )
            )

            self.assertEqual(result.status, VerificationCheckStatus.PASS)
            self.assertTrue(result.stdout_truncated)
            self.assertTrue(result.stderr_truncated)
            self.assertIn(REDACTION_MARKER, result.stdout)
            self.assertIn(REDACTION_MARKER, result.stderr)
            self.assertNotIn(canary, result.stdout)
            self.assertNotIn(canary, result.stderr)
            for reference in (result.stdout_artifact, result.stderr_artifact):
                self.assertIsNotNone(reference)
                assert reference is not None
                self.assertFalse(reference.truncated)
                self.assertTrue(reference.redacted)
                payload = (runtime_root / reference.relative_path).read_bytes()
                self.assertNotIn(canary.encode("utf-8"), payload)
                self.assertEqual(reference.size, len(payload))
                self.assertEqual(reference.sha256, hashlib.sha256(payload).hexdigest())
                self.assertGreater(len(payload), len(result.stdout.encode("utf-8")))

            raw_stdout = (
                f"prefix {canary} ".encode("utf-8") + b"X" * 400
            )
            self.assertEqual(
                result.stdout_digest,
                hashlib.sha256(raw_stdout).hexdigest(),
            )

    def test_timeout_still_publishes_drained_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            executable = getattr(sys, "_base_executable", sys.executable)
            result = VerificationRunner(
                root, runtime_root=runtime_root
            ).run(
                VerificationSpec(
                    name="timeout-artifact",
                    argv=(
                        executable,
                        "-c",
                        "import sys,time; "
                        "sys.stdout.write('before-timeout'); sys.stdout.flush(); "
                        "time.sleep(5)",
                    ),
                    timeout_seconds=0.1,
                )
            )

            self.assertEqual(result.status, VerificationCheckStatus.TIMEOUT)
            self.assertIsNotNone(result.stdout_artifact)
            assert result.stdout_artifact is not None
            self.assertIn(
                b"before-timeout",
                (runtime_root / result.stdout_artifact.relative_path).read_bytes(),
            )

    def test_artifact_quota_is_structured_evidence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = VerificationRunner(
                root,
                runtime_root=root / "runtime",
                artifact_quota_bytes=8,
            ).run(
                VerificationSpec(
                    name="quota",
                    argv=(sys.executable, "-c", "print('output larger than quota')"),
                )
            )

            self.assertEqual(result.status, VerificationCheckStatus.PASS)
            self.assertEqual(
                result.artifact_error_code,
                ArtifactErrorCode.QUOTA_EXCEEDED.value,
            )
            self.assertIsNone(result.stdout_artifact)
            self.assertIn("incomplete", result.message.lower())

    def test_artifact_cleanup_failure_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), max_bytes=1)
            with patch(
                "agent.artifacts.Path.unlink",
                side_effect=OSError("cleanup failed"),
            ):
                result = store.put_bytes(
                    kind="verification_stdout",
                    data=b"too large",
                    media_type="text/plain",
                )

            self.assertEqual(
                result.error_code,
                ArtifactErrorCode.CLEANUP_FAILED.value,
            )

    def test_rejects_external_artifacts_symlink_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            external = root / "external"
            runtime.mkdir()
            external.mkdir()
            link = runtime / "artifacts"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            result = ArtifactStore(runtime).put_bytes(
                kind="verification_stdout",
                data=b"must stay inside",
                media_type="text/plain",
            )

            self.assertEqual(result.error_code, ArtifactErrorCode.INVALID.value)
            self.assertEqual(list(external.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows path type")
    def test_rejects_external_artifacts_junction_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            external = root / "external"
            runtime.mkdir()
            external.mkdir()
            junction = runtime / "artifacts"
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                self.skipTest(
                    f"junction creation unavailable: {completed.stderr.strip()}"
                )

            result = ArtifactStore(runtime).put_bytes(
                kind="verification_stdout",
                data=b"must stay inside",
                media_type="text/plain",
            )

            self.assertEqual(result.error_code, ArtifactErrorCode.INVALID.value)
            self.assertEqual(list(external.iterdir()), [])

    def test_artifact_publish_failure_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "agent.artifacts.os.replace",
                side_effect=OSError("publish failed"),
            ):
                result = VerificationRunner(
                    root, runtime_root=root / "runtime"
                ).run(
                    VerificationSpec(
                        name="publish-failure",
                        argv=(sys.executable, "-c", "print('ok')"),
                    )
                )

            self.assertEqual(result.status, VerificationCheckStatus.PASS)
            self.assertEqual(
                result.artifact_error_code,
                ArtifactErrorCode.PUBLISH_FAILED.value,
            )
            self.assertIsNone(result.stdout_artifact)

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

    def test_rewrites_only_matching_junit_destination(self) -> None:
        captured: list[tuple[str, ...]] = []

        class FakeProcess:
            stdout = io.BytesIO()
            stderr = io.BytesIO()

            def wait(self, timeout: float) -> int:
                return 0

            def poll(self) -> int:
                return 0

        def fake_popen(argv: list[str], **_: object) -> FakeProcess:
            captured.append(tuple(argv))
            return FakeProcess()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = VerificationSpec(
                name="rewrite",
                argv=(
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/report.xml_case.py",
                    "-k",
                    "report.xml",
                    "--junitxml",
                    "report.xml",
                ),
                report_path="report.xml",
            )
            with patch.object(
                runner_module.subprocess, "Popen", side_effect=fake_popen
            ):
                result = VerificationRunner(root).run(spec)

        self.assertEqual(result.status, VerificationCheckStatus.PASS)
        self.assertEqual(len(captured), 1)
        effective = captured[0]
        self.assertEqual(effective[3], "tests/report.xml_case.py")
        self.assertEqual(effective[5], "report.xml")
        self.assertEqual(effective[6], "--junitxml")
        self.assertNotEqual(effective[7], "report.xml")
        self.assertTrue(effective[7].endswith("report.xml"))

    def test_nonexistent_workspace_report_stays_absent_after_success(self) -> None:
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

    def test_existing_workspace_report_is_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.xml"
            report.write_bytes(b"user-owned")
            before = report.stat()
            (root / "test_report.py").write_text(
                "def test_report():\n    assert True\n",
                encoding="utf-8",
                newline="",
            )

            result = VerificationRunner(root).run(self._pytest_spec())

            after = report.stat()
            self.assertEqual(result.status, VerificationCheckStatus.PASS)
            self.assertEqual(report.read_bytes(), b"user-owned")
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

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

    def test_interrupt_propagates_after_owned_report_cleanup(self) -> None:
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    report = root / "report.xml"
                    report.write_bytes(b"original")
                    before = report.stat()
                    with patch.object(
                        runner_module.subprocess,
                        "Popen",
                        side_effect=interruption,
                    ):
                        with self.assertRaises(interruption):
                            VerificationRunner(root).run(self._pytest_spec())

                    after = report.stat()
                    self.assertEqual(report.read_bytes(), b"original")
                    self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

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
