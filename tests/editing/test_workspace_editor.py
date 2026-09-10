import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.editing import (
    EditErrorCode,
    EditOperation,
    EditProposal,
    EditStatus,
    WorkspaceEditor,
)


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class WorkspaceEditorTests(unittest.TestCase):
    def test_missing_workspace_root_is_a_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_root = Path(directory) / "missing"

            snapshot = WorkspaceEditor(missing_root).snapshot("app.py")
            result = WorkspaceEditor(missing_root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.CREATE,
                    new_text="value = 1\n",
                )
            )

            self.assertEqual(
                snapshot.error_code, EditErrorCode.WORKSPACE_NOT_FOUND
            )
            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(
                result.error_code, EditErrorCode.WORKSPACE_NOT_FOUND
            )

    def test_snapshots_existing_utf8_file_with_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = "标题 = '可靠编辑'\n"
            (root / "app.py").write_text(content, encoding="utf-8", newline="")

            snapshot = WorkspaceEditor(root).snapshot("app.py")

            self.assertTrue(snapshot.exists)
            self.assertEqual(snapshot.path, "app.py")
            self.assertEqual(snapshot.content, content)
            self.assertEqual(snapshot.content_hash, sha256(content))
            self.assertIsNone(snapshot.error_code)

    def test_snapshot_distinguishes_missing_invalid_and_non_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "workspace"
            root.mkdir()
            (root / "binary.dat").write_bytes(b"\xff\xfe")
            editor = WorkspaceEditor(root)

            missing = editor.snapshot("missing.py")
            invalid = editor.snapshot("../secret.txt")
            non_utf8 = editor.snapshot("binary.dat")

            self.assertFalse(missing.exists)
            self.assertIsNone(missing.error_code)
            self.assertEqual(invalid.error_code, EditErrorCode.PATH_INVALID)
            self.assertEqual(non_utf8.error_code, EditErrorCode.ENCODING_ERROR)

    def test_applies_unique_edit_with_matching_base_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = "def answer():\n    return 41\n"
            target.write_text(original, encoding="utf-8", newline="")

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash=sha256(original),
                    old_text="return 41",
                    new_text="return 42",
                )
            )

            expected = "def answer():\n    return 42\n"
            self.assertEqual(result.status, EditStatus.APPLIED)
            self.assertIsNone(result.error_code)
            self.assertEqual(result.before_hash, sha256(original))
            self.assertEqual(result.after_hash, sha256(expected))
            self.assertIn("-    return 41", result.diff)
            self.assertIn("+    return 42", result.diff)
            self.assertEqual(target.read_bytes(), expected.encode("utf-8"))

    def test_rejects_edit_when_file_changed_after_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = "value = 1\n"
            target.write_text("value = 2\n", encoding="utf-8", newline="")

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash=sha256(original),
                    old_text="value = 1",
                    new_text="value = 3",
                )
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.HASH_MISMATCH)
            self.assertEqual(result.before_hash, sha256("value = 2\n"))
            self.assertIsNone(result.after_hash)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_rejects_non_unique_old_text_without_writing(self) -> None:
        cases = [
            ("", EditErrorCode.EMPTY_OLD_TEXT),
            ("missing", EditErrorCode.MATCH_NOT_FOUND),
            ("item", EditErrorCode.MATCH_AMBIGUOUS),
        ]
        for old_text, expected_error in cases:
            with self.subTest(old_text=old_text), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "items.txt"
                original = "item one\nitem two\n"
                target.write_text(original, encoding="utf-8", newline="")

                result = WorkspaceEditor(root).apply(
                    EditProposal(
                        task_id="task-1.step-1",
                        path="items.txt",
                        operation=EditOperation.EDIT,
                        base_hash=sha256(original),
                        old_text=old_text,
                        new_text="changed",
                    )
                )

                self.assertEqual(result.status, EditStatus.REJECTED)
                self.assertEqual(result.error_code, expected_error)
                self.assertEqual(target.read_bytes(), original.encode("utf-8"))

    def test_rejects_path_that_escapes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "workspace"
            root.mkdir()
            outside = parent / "outside.txt"
            original = "private\n"
            outside.write_text(original, encoding="utf-8", newline="")

            for unsafe_path in ("../outside.txt", str(outside)):
                with self.subTest(path=unsafe_path):
                    result = WorkspaceEditor(root).apply(
                        EditProposal(
                            task_id="task-1.step-1",
                            path=unsafe_path,
                            operation=EditOperation.EDIT,
                            base_hash=sha256(original),
                            old_text="private",
                            new_text="changed",
                        )
                    )

                    self.assertEqual(result.status, EditStatus.REJECTED)
                    self.assertEqual(result.error_code, EditErrorCode.PATH_INVALID)
                    self.assertEqual(outside.read_bytes(), original.encode("utf-8"))

    def test_rejects_symlink_even_when_target_is_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.txt"
            link = root / "link.txt"
            original = "value\n"
            target.write_text(original, encoding="utf-8", newline="")
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links are unavailable: {error}")

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="link.txt",
                    operation=EditOperation.EDIT,
                    base_hash=sha256(original),
                    old_text="value",
                    new_text="changed",
                )
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.PATH_INVALID)
            self.assertEqual(target.read_bytes(), original.encode("utf-8"))

    def test_rejects_edit_when_target_file_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = WorkspaceEditor(directory).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="missing.py",
                    operation=EditOperation.EDIT,
                    base_hash=sha256(""),
                    old_text="old",
                    new_text="new",
                )
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.FILE_NOT_FOUND)

    def test_reports_noop_without_rewriting_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = "enabled = True\n"
            target.write_text(original, encoding="utf-8", newline="")
            original_stat = target.stat()

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash=sha256(original),
                    old_text="True",
                    new_text="True",
                )
            )

            self.assertEqual(result.status, EditStatus.NOOP)
            self.assertIsNone(result.error_code)
            self.assertEqual(result.before_hash, result.after_hash)
            self.assertEqual(result.diff, "")
            self.assertEqual(target.stat().st_ino, original_stat.st_ino)

    def test_creates_new_file_in_nested_workspace_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = "from __future__ import annotations\n"

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="package/new_module.py",
                    operation=EditOperation.CREATE,
                    new_text=content,
                )
            )

            self.assertEqual(result.status, EditStatus.APPLIED)
            self.assertIsNone(result.error_code)
            self.assertIsNone(result.before_hash)
            self.assertEqual(result.after_hash, sha256(content))
            self.assertIn("+from __future__ import annotations", result.diff)
            self.assertEqual(
                (root / "package/new_module.py").read_bytes(), content.encode("utf-8")
            )

    def test_rejects_create_when_target_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "config.py"
            original = "DEBUG = False\n"
            target.write_text(original, encoding="utf-8", newline="")

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="config.py",
                    operation=EditOperation.CREATE,
                    new_text="DEBUG = True\n",
                )
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.FILE_EXISTS)
            self.assertEqual(target.read_bytes(), original.encode("utf-8"))

    def test_returns_structured_error_when_create_cannot_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "occupied").write_text("not a directory", encoding="utf-8")

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="occupied/new.py",
                    operation=EditOperation.CREATE,
                    new_text="value = 1\n",
                )
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.WRITE_FAILED)
            self.assertFalse((root / "occupied/new.py").exists())

    def test_rejects_non_utf8_file_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "binary.dat"
            original = b"\xff\xfe\x00"
            target.write_bytes(original)

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="binary.dat",
                    operation=EditOperation.EDIT,
                    base_hash=hashlib.sha256(original).hexdigest(),
                    old_text="old",
                    new_text="new",
                )
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.ENCODING_ERROR)
            self.assertEqual(target.read_bytes(), original)

    def test_edits_crlf_file_without_changing_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "message.py"
            original = "标题 = '旧值'\r\nprint(标题)\r\n"
            target.write_bytes(original.encode("utf-8"))

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="message.py",
                    operation=EditOperation.EDIT,
                    base_hash=sha256(original),
                    old_text="标题 = '旧值'\nprint(标题)",
                    new_text="标题 = '新值'\nprint(标题)",
                )
            )

            expected = "标题 = '新值'\r\nprint(标题)\r\n"
            self.assertEqual(result.status, EditStatus.APPLIED)
            self.assertEqual(target.read_bytes(), expected.encode("utf-8"))

    def test_edit_preserves_missing_eof_newline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "message.py"
            original = "value = 1"
            target.write_bytes(original.encode("utf-8"))

            result = WorkspaceEditor(root).apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="message.py",
                    operation=EditOperation.EDIT,
                    base_hash=sha256(original),
                    old_text="value = 1",
                    new_text="value = 2",
                )
            )

            self.assertEqual(result.status, EditStatus.APPLIED)
            self.assertEqual(target.read_bytes(), b"value = 2")

    def test_apply_classifies_existing_file_read_error_as_read_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            editor = WorkspaceEditor(root)

            with patch.object(Path, "read_bytes", side_effect=OSError("denied")):
                result = editor.apply(
                    EditProposal(
                        task_id="task-1.step-1",
                        path="app.py",
                        operation=EditOperation.EDIT,
                        base_hash=sha256("value = 1\n"),
                        old_text="value = 1",
                        new_text="value = 2",
                    )
                )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.READ_FAILED)

    def test_failed_nested_create_removes_directories_created_by_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editor = WorkspaceEditor(root)

            with patch("agent.editing.files.os.link", side_effect=OSError("denied")):
                result = editor.apply(
                    EditProposal(
                        task_id="task-1.step-1",
                        path="new/package/module.py",
                        operation=EditOperation.CREATE,
                        new_text="value = 1\n",
                    )
                )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code, EditErrorCode.WRITE_FAILED)
            self.assertFalse((root / "new").exists())

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows path type")
    def test_rejects_windows_junction_that_points_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "workspace"
            outside = parent / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text(
                "private\n", encoding="utf-8", newline=""
            )
            junction = root / "linked"
            completed = subprocess.run(
                [
                    "pwsh.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-CommandWithArgs",
                    "New-Item -ItemType Junction -Path $args[0] -Target $args[1] | Out-Null",
                    str(junction),
                    str(outside),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(f"junction creation unavailable: {completed.stderr}")

            result = WorkspaceEditor(root).snapshot("linked/secret.txt")

            self.assertEqual(result.error_code, EditErrorCode.PATH_INVALID)


if __name__ == "__main__":
    unittest.main()
