from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.evaluation.task_manifest import (
    environment_summary,
    load_task_manifest,
    manifest_content_hash,
    validate_task_manifest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TRUSTED_REPOSITORY = "https://github.com/bbandcc/swe-agent.git"
MANIFEST_PATH = REPOSITORY_ROOT / "evals" / "s5a" / "tasks.v1.json"


class TaskManifestTests(unittest.TestCase):
    def _document(self) -> dict[str, object]:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def _single_task_document(self) -> dict[str, object]:
        document = self._document()
        document["tasks"] = [document["tasks"][0]]
        self._rehash(document)
        return document

    def _validate(
        self,
        document: object,
        *,
        expected_environment: dict[str, str] | None = None,
    ):
        return validate_task_manifest(
            document,
            repository_root=REPOSITORY_ROOT,
            expected_repository=TRUSTED_REPOSITORY,
            expected_environment=(
                expected_environment or environment_summary(REPOSITORY_ROOT)
            ),
        )

    @staticmethod
    def _rehash(document: dict[str, object]) -> None:
        document["manifest_sha256"] = manifest_content_hash(document)

    def test_source_backed_seed_manifest_validates_offline(self) -> None:
        result = self._validate(self._document())

        self.assertTrue(result.valid, result.issues)
        self.assertEqual(result.manifest_id, "s5a-history-seed-v1")
        self.assertGreaterEqual(len(result.task_ids), 1)
        self.assertEqual(len(result.task_ids), len(set(result.task_ids)))

    def test_missing_required_budget_field_is_rejected(self) -> None:
        document = self._single_task_document()
        task = document["tasks"][0]
        del task["budget"]["max_steps"]
        self._rehash(document)

        result = self._validate(document)

        self.assertIn("missing_field", {issue.code.value for issue in result.issues})

    def test_task_text_and_manifest_tampering_are_rejected(self) -> None:
        document = self._single_task_document()
        document["tasks"][0]["task_text"] += " altered"

        result = self._validate(document)

        codes = {issue.code.value for issue in result.issues}
        self.assertIn("manifest_hash_mismatch", codes)
        self.assertIn("task_hash_mismatch", codes)

    def test_invalid_unicode_in_manifest_fails_closed(self) -> None:
        document = self._single_task_document()
        document["tasks"][0]["task_text"] = "\ud800"

        result = self._validate(document)

        self.assertFalse(result.valid)
        self.assertTrue(result.issues)

    def test_duplicate_task_ids_are_rejected(self) -> None:
        document = self._document()
        document["tasks"][1]["task_id"] = document["tasks"][0]["task_id"]
        self._rehash(document)

        result = self._validate(document)

        self.assertIn(
            "duplicate_task_id", {issue.code.value for issue in result.issues}
        )

    def test_edit_scope_cannot_overlap_hidden_oracle(self) -> None:
        document = self._single_task_document()
        task = document["tasks"][0]
        oracle_path = task["oracle"]["test_files"][0]["path"]
        task["allowed_edit_scope"].append(
            {"path": oracle_path, "operation": "modify"}
        )
        self._rehash(document)

        result = self._validate(document)

        self.assertIn(
            "scope_oracle_overlap", {issue.code.value for issue in result.issues}
        )

    def test_floating_and_unknown_revisions_are_rejected(self) -> None:
        for revision in ("main", "0" * 40):
            with self.subTest(revision=revision):
                document = self._single_task_document()
                document["tasks"][0]["target"]["revision"] = revision
                self._rehash(document)

                result = self._validate(document)

                self.assertTrue(
                    {issue.code.value for issue in result.issues}
                    & {"invalid_revision", "unknown_revision"}
                )

    def test_noncanonical_or_escaping_scope_is_rejected(self) -> None:
        document = self._single_task_document()
        document["tasks"][0]["allowed_edit_scope"][0]["path"] = "../outside.py"
        self._rehash(document)

        result = self._validate(document)

        self.assertIn("invalid_path", {issue.code.value for issue in result.issues})

    def test_shell_string_or_invalid_check_configuration_is_rejected(self) -> None:
        document = self._single_task_document()
        document["tasks"][0]["checks"]["target"][0]["argv"] = "python -m unittest"
        self._rehash(document)

        result = self._validate(document)

        self.assertIn("invalid_check", {issue.code.value for issue in result.issues})

    def test_environment_mismatch_is_rejected(self) -> None:
        document = self._single_task_document()
        expected = environment_summary(REPOSITORY_ROOT)
        expected["python_version"] = "0.0"

        result = self._validate(document, expected_environment=expected)

        self.assertIn(
            "environment_mismatch", {issue.code.value for issue in result.issues}
        )

    def test_environment_lock_must_match_pinned_target_revision(self) -> None:
        document = self._single_task_document()
        expected = environment_summary(REPOSITORY_ROOT)
        expected["lock_sha256"] = "0" * 64
        document["tasks"][0]["environment"]["lock_sha256"] = expected["lock_sha256"]
        self._rehash(document)

        result = self._validate(document, expected_environment=expected)

        self.assertIn(
            "environment_mismatch", {issue.code.value for issue in result.issues}
        )

    def test_target_repository_mismatch_is_rejected(self) -> None:
        document = self._single_task_document()
        document["tasks"][0]["target"]["repo"] = "https://example.invalid/other.git"
        self._rehash(document)

        result = self._validate(document)

        self.assertIn(
            "repository_mismatch", {issue.code.value for issue in result.issues}
        )

    def test_oracle_file_hash_mismatch_is_rejected(self) -> None:
        document = self._single_task_document()
        document["tasks"][0]["oracle"]["test_files"][0]["sha256"] = "0" * 64
        self._rehash(document)

        result = self._validate(document)

        self.assertIn(
            "oracle_hash_mismatch", {issue.code.value for issue in result.issues}
        )

    def test_malformed_manifest_json_returns_structured_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")

            result = load_task_manifest(
                path,
                repository_root=REPOSITORY_ROOT,
                expected_repository=TRUSTED_REPOSITORY,
                expected_environment=environment_summary(REPOSITORY_ROOT),
            )

        self.assertFalse(result.valid)
        self.assertIn("invalid_json", {issue.code.value for issue in result.issues})

    def test_unknown_environment_and_shell_fields_fail_closed(self) -> None:
        document = self._single_task_document()
        document["tasks"][0]["checks"]["baseline"][0]["shell"] = True
        self._rehash(document)

        result = self._validate(document)

        self.assertIn("invalid_check", {issue.code.value for issue in result.issues})

if __name__ == "__main__":
    unittest.main()
