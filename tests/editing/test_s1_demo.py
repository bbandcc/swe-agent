"""Exact, author-controlled tests; review the S1 import closure before running."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.demo_s1_editor import main, run_demo


class S1DemoTests(unittest.TestCase):
    def test_two_stage_commit_has_exact_bytes_hashes_and_task_ids(self) -> None:
        report = run_demo()
        case = next(item for item in report.scenarios if item.name == "two_stage_commit")
        expected = "label=完成\ncount=2\n".encode("utf-8")
        self.assertTrue(report.passed)
        self.assertEqual(case.actual_status, "APPLIED")
        self.assertIsNone(case.actual_error_code)
        self.assertEqual(case.content_after, expected)
        self.assertEqual(case.result_after_hash, hashlib.sha256(expected).hexdigest())
        self.assertEqual(case.task_ids, ("commit.label", "commit.count"))
        self.assertTrue(case.checks["staging_kept_original_bytes"])

    def test_stale_hash_rejects_without_modifying_current_bytes(self) -> None:
        report = run_demo()
        case = next(item for item in report.scenarios if item.name == "stale_hash")
        self.assertTrue(case.passed)
        self.assertEqual(case.actual_status, "REJECTED")
        self.assertEqual(case.actual_error_code, "HASH_MISMATCH")
        self.assertEqual(case.content_after, "label=初始\ncount=1\n".encode("utf-8"))
        self.assertEqual(case.content_after, case.content_before)

    def test_ambiguous_match_rejects_without_modifying_bytes(self) -> None:
        report = run_demo()
        case = next(item for item in report.scenarios if item.name == "ambiguous_match")
        self.assertTrue(case.passed)
        self.assertEqual(case.actual_status, "REJECTED")
        self.assertEqual(case.actual_error_code, "MATCH_AMBIGUOUS")
        self.assertEqual(case.content_after, "value=重复\nvalue=重复\n".encode("utf-8"))
        self.assertEqual(case.content_after, case.content_before)

    def test_net_zero_is_noop_with_original_bytes_and_both_task_ids(self) -> None:
        report = run_demo()
        case = next(item for item in report.scenarios if item.name == "net_zero")
        self.assertTrue(case.passed)
        self.assertEqual(case.actual_status, "NOOP")
        self.assertIsNone(case.actual_error_code)
        self.assertEqual(case.content_after, "label=初始\ncount=1\n".encode("utf-8"))
        self.assertEqual(case.result_before_hash, case.result_after_hash)
        self.assertEqual(case.task_ids, ("noop.change", "noop.restore"))

    def test_main_emits_actual_evidence_and_zero_only_for_success(self) -> None:
        output = io.StringIO()
        code = main(output=output)
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result["passed"])
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(len(result["scenarios"]), 4)
        self.assertTrue(all(item["passed"] for item in result["scenarios"]))

    def test_main_returns_nonzero_on_real_fixture_write_failure(self) -> None:
        output = io.StringIO()
        # Fault injection at the filesystem boundary, not a fake editor result.
        with patch.object(Path, "write_bytes", side_effect=PermissionError("PRIVATE_PATH")):
            code = main(output=output)
        result = json.loads(output.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["errors"], ["two_stage_commit:PermissionError"])
        self.assertNotIn("PRIVATE_PATH", output.getvalue())

    def test_owned_temporary_workspace_is_removed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            report = run_demo(temporary_parent=Path(parent))
            self.assertTrue(report.passed)
            self.assertTrue(report.cleanup_complete)
            self.assertEqual(list(Path(parent).iterdir()), [])

    def test_actual_content_mismatch_produces_nonzero_even_after_applied(self) -> None:
        read_bytes = Path.read_bytes
        expected = "label=完成\ncount=2\n".encode("utf-8")

        def corrupt_observation(path: Path) -> bytes:
            actual = read_bytes(path)
            return b"\xff" if actual == expected else actual

        output = io.StringIO()
        with patch.object(Path, "read_bytes", corrupt_observation):
            code = main(output=output)
        result = json.loads(output.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["scenarios"][0]["actual_status"], "APPLIED")
        self.assertEqual(result["scenarios"][0]["actual_bytes_hex"], "ff")
        self.assertFalse(result["scenarios"][0]["checks"]["bytes_match"])

    def test_repeated_successful_runs_emit_identical_path_free_evidence(self) -> None:
        first, second = io.StringIO(), io.StringIO()
        self.assertEqual(main(output=first), 0)
        self.assertEqual(main(output=second), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        # Every generated file path is relative; random temporary roots are absent.
        self.assertNotIn("s1-editor-demo-", first.getvalue())

    def test_owned_temporary_workspace_is_removed_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            with patch.object(Path, "write_bytes", side_effect=PermissionError):
                report = run_demo(temporary_parent=Path(parent))
            self.assertFalse(report.passed)
            self.assertTrue(report.cleanup_complete)
            self.assertEqual(list(Path(parent).iterdir()), [])

    def test_cleanup_failure_cannot_be_reported_as_success(self) -> None:
        cleanup = tempfile.TemporaryDirectory.cleanup

        def fail_after_removal(directory: tempfile.TemporaryDirectory) -> None:
            cleanup(directory)
            raise OSError("PRIVATE_PATH")

        with tempfile.TemporaryDirectory() as parent:
            with patch.object(tempfile.TemporaryDirectory, "cleanup", fail_after_removal):
                report = run_demo(temporary_parent=Path(parent))
            self.assertFalse(report.passed)
            self.assertFalse(report.cleanup_complete)
            self.assertIn("cleanup:OSError", report.errors)
            self.assertEqual(list(Path(parent).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
