import tempfile
import unittest
from pathlib import Path

from agent.developer.editing import DeveloperEditExecutor
from agent.editing import EditErrorCode, EditStatus, WorkspaceEditor


def search_replace_block(old_text: str, new_text: str) -> str:
    return (
        f"<<<<<<< SEARCH\n{old_text}\n=======\n"
        f"{new_text}\n>>>>>>> REPLACE"
    )


class DeveloperEditExecutorTests(unittest.TestCase):
    def test_applies_one_exact_search_replace_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text(
                "def answer():\n    return 41\n", encoding="utf-8", newline=""
            )
            executor = DeveloperEditExecutor(WorkspaceEditor(root))

            snapshot = executor.prepare("./workspace_repo/app.py")
            result = executor.apply(
                snapshot,
                search_replace_block("    return 41", "    return 42"),
            )

            self.assertEqual(result.status, EditStatus.APPLIED)
            self.assertEqual(snapshot.path, "app.py")
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "def answer():\n    return 42\n",
            )

    def test_rejects_malformed_or_multiple_blocks_without_writing(self) -> None:
        invalid_outputs = (
            f"Here is the edit:\n{search_replace_block('old', 'new')}",
            "<<<<<<< SEARCH\nold\n=======\nnew",
            (
                f"{search_replace_block('old', 'new')}\n"
                f"{search_replace_block('old', 'other')}"
            ),
        )
        for model_output in invalid_outputs:
            with self.subTest(model_output=model_output), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "app.py"
                target.write_text("old\n", encoding="utf-8", newline="")
                executor = DeveloperEditExecutor(WorkspaceEditor(root))

                result = executor.apply(executor.prepare("app.py"), model_output)

                self.assertEqual(result.status, EditStatus.REJECTED)
                self.assertEqual(
                    result.error_code, EditErrorCode.INVALID_MODEL_RESPONSE
                )
                self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_rejects_non_unique_search_text_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "items.txt"
            target.write_text("item one\nitem two\n", encoding="utf-8", newline="")
            executor = DeveloperEditExecutor(WorkspaceEditor(root))

            result = executor.apply(
                executor.prepare("items.txt"),
                search_replace_block("item", "entry"),
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.MATCH_AMBIGUOUS)
            self.assertEqual(
                target.read_text(encoding="utf-8"), "item one\nitem two\n"
            )

    def test_rejects_edit_if_file_changed_after_model_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            snapshot = executor.prepare("app.py")
            target.write_text("value = 2\n", encoding="utf-8", newline="")

            result = executor.apply(
                snapshot,
                search_replace_block("value = 1", "value = 3"),
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.HASH_MISMATCH)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_creates_missing_file_from_complete_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = DeveloperEditExecutor(WorkspaceEditor(root))

            result = executor.apply(
                executor.prepare("./workspace_repo/package/new.py"),
                "value = 42\n",
            )

            self.assertEqual(result.status, EditStatus.APPLIED)
            self.assertEqual(
                (root / "package/new.py").read_text(encoding="utf-8"),
                "value = 42\n",
            )

    def test_rejects_plan_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = DeveloperEditExecutor(WorkspaceEditor(root))

            snapshot = executor.prepare("./workspace_repo/../secret.txt")
            result = executor.apply(snapshot, "exposed")

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.PATH_INVALID)
            self.assertFalse((root.parent / "secret.txt").exists())


if __name__ == "__main__":
    unittest.main()
