import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent.developer.editing import DeveloperEditExecutor
from agent.editing import (
    CommittedEdit,
    EditOperation,
    EditProposal,
    EditResult,
    EditStatus,
    RecoveryReconciler,
    RecoveryStatus,
    WorkspaceEditor,
    WorkspaceTransaction,
    WriteIntent,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class WriteRecoveryTests(unittest.TestCase):
    def test_committed_edit_is_stable_checkpoint_safe_and_content_free(self):
        transaction = WorkspaceTransaction(
            path="app.py",
            existed=True,
            original_content="value = 1\n",
            working_content="value = 2\n",
            base_hash=_sha(b"value = 1\n"),
            original_mode=None,
            task_ids=("task-1.step-1",),
        )
        result = EditResult(
            status=EditStatus.APPLIED,
            path="app.py",
            before_hash=transaction.base_hash,
            after_hash=_sha(b"value = 2\n"),
            diff="@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            task_ids=transaction.task_ids,
        )
        record = CommittedEdit.create(
            run_id="run-1",
            task_id="task-1",
            task_index=0,
            repair_attempt=0,
            transaction=transaction,
            result=result,
        )
        payload = record.to_dict()
        self.assertNotIn("value = 1", repr(payload))
        self.assertNotIn("value = 2", repr(payload))
        self.assertEqual(CommittedEdit.from_dict(payload), record)
        self.assertEqual(
            CommittedEdit.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=0,
                transaction=transaction,
                result=result,
            ).write_id,
            record.write_id,
        )

    def test_edit_reconciles_safe_then_already_applied_without_second_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = b"value = 1\n"
            updated = b"value = 2\n"
            target.write_bytes(original)
            editor = WorkspaceEditor(root)
            started = editor.begin("app.py")
            self.assertTrue(started.ok)
            transaction = started.transaction
            assert transaction is not None
            staged = editor.stage(
                transaction,
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash=_sha(original),
                    old_text="1",
                    new_text="2",
                ),
            )
            self.assertTrue(staged.ok)
            transaction = staged.transaction
            assert transaction is not None
            intent = WriteIntent.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=0,
                transaction=transaction,
                expected_after_hash=_sha(updated),
            )
            reconciler = RecoveryReconciler(editor)

            safe = reconciler.reconcile(transaction, intent)
            self.assertEqual(safe.status, RecoveryStatus.SAFE_TO_APPLY)
            self.assertIsNone(safe.edit_result)

            applied = editor.commit(transaction)
            self.assertEqual(applied.status, EditStatus.APPLIED)
            mtime = target.stat().st_mtime_ns
            already = reconciler.reconcile(transaction, intent)
            self.assertEqual(already.status, RecoveryStatus.ALREADY_APPLIED)
            self.assertIsNotNone(already.edit_result)
            self.assertEqual(already.edit_result.status, EditStatus.APPLIED)
            self.assertEqual(target.read_bytes(), updated)
            self.assertEqual(target.stat().st_mtime_ns, mtime)

    def test_external_third_value_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = b"value = 1\n"
            target.write_bytes(original)
            editor = WorkspaceEditor(root)
            started = editor.begin("app.py")
            assert started.transaction is not None
            transaction = started.transaction
            staged = editor.stage(
                transaction,
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash=_sha(original),
                    old_text="1",
                    new_text="2",
                ),
            )
            assert staged.transaction is not None
            transaction = staged.transaction
            intent = WriteIntent.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=0,
                transaction=transaction,
                expected_after_hash=_sha(b"value = 2\n"),
            )
            target.write_bytes(b"value = 3\n")

            result = RecoveryReconciler(editor).reconcile(transaction, intent)

            self.assertEqual(result.status, RecoveryStatus.CONFLICT)
            self.assertIsNone(result.edit_result)
            self.assertEqual(target.read_bytes(), b"value = 3\n")

    def test_create_missing_file_is_safe_and_existing_expected_is_already_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editor = WorkspaceEditor(root)
            started = editor.begin("empty.txt")
            self.assertTrue(started.ok)
            transaction = started.transaction
            assert transaction is not None
            staged = editor.stage(
                transaction,
                EditProposal(
                    task_id="task-1.step-1",
                    path="empty.txt",
                    operation=EditOperation.CREATE,
                    new_text="",
                ),
            )
            self.assertTrue(staged.ok)
            transaction = staged.transaction
            assert transaction is not None
            intent = WriteIntent.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=0,
                transaction=transaction,
                expected_after_hash=_sha(b""),
            )
            reconciler = RecoveryReconciler(editor)
            self.assertEqual(
                reconciler.reconcile(transaction, intent).status,
                RecoveryStatus.SAFE_TO_APPLY,
            )
            self.assertEqual(editor.commit(transaction).status, EditStatus.APPLIED)
            self.assertTrue((root / "empty.txt").exists())
            self.assertEqual(
                reconciler.reconcile(transaction, intent).status,
                RecoveryStatus.ALREADY_APPLIED,
            )

    def test_intent_serialization_contains_hashes_not_file_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = "secret source\n"
            target.write_text(original, encoding="utf-8", newline="")
            editor = WorkspaceEditor(root)
            transaction = editor.begin("app.py").transaction
            assert transaction is not None
            staged = editor.stage(
                transaction,
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash=_sha(original.encode()),
                    old_text="secret",
                    new_text="safe",
                ),
            )
            assert staged.transaction is not None
            transaction = staged.transaction
            intent = WriteIntent.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=0,
                transaction=transaction,
                expected_after_hash=_sha(b"safe source\n"),
            )
            payload = intent.to_dict()
            self.assertNotIn(original, repr(payload))
            restored = WriteIntent.from_dict(payload)
            self.assertEqual(restored, intent)
            self.assertTrue(
                intent.matches_identity(
                    run_id="run-1",
                    task_id="task-1",
                    task_index=0,
                    repair_attempt=0,
                    path="app.py",
                    operation=EditOperation.EDIT,
                )
            )
            self.assertFalse(
                intent.matches_identity(
                    run_id="run-1",
                    task_id="task-1",
                    task_index=1,
                    repair_attempt=0,
                    path="app.py",
                    operation=EditOperation.EDIT,
                )
            )
            tampered = replace(intent)
            object.__setattr__(tampered, "write_id", "0" * 64)
            self.assertFalse(
                tampered.matches_identity(
                    run_id="run-1",
                    task_id="task-1",
                    task_index=0,
                    repair_attempt=0,
                    path="app.py",
                    operation=EditOperation.EDIT,
                )
            )

            alternate_transaction = replace(
                transaction, working_content="different expected bytes"
            )
            alternate = WriteIntent.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=0,
                transaction=alternate_transaction,
                expected_after_hash=_sha(b"different expected bytes"),
            )
            self.assertEqual(alternate.write_id, intent.write_id)
            repair_intent = WriteIntent.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=1,
                transaction=transaction,
                expected_after_hash=_sha(b"safe source\n"),
            )
            self.assertNotEqual(repair_intent.write_id, intent.write_id)

    def test_directory_link_replacement_is_a_recovery_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            target = nested / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            editor = WorkspaceEditor(root)
            started = editor.begin("nested/app.py")
            assert started.transaction is not None
            transaction = started.transaction
            staged = editor.stage(
                transaction,
                EditProposal(
                    task_id="task-1.step-1",
                    path="nested/app.py",
                    operation=EditOperation.EDIT,
                    base_hash=_sha(b"value = 1\n"),
                    old_text="1",
                    new_text="2",
                ),
            )
            assert staged.transaction is not None
            transaction = staged.transaction
            intent = WriteIntent.create(
                run_id="run-1",
                task_id="task-1",
                task_index=0,
                repair_attempt=0,
                transaction=transaction,
                expected_after_hash=_sha(b"value = 2\n"),
            )

            external = root / "external"
            external.mkdir()
            external_file = external / "app.py"
            external_file.write_text("outside\n", encoding="utf-8", newline="")
            shutil.rmtree(nested)
            try:
                if os.name == "nt":
                    completed = subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(nested), str(external)],
                        capture_output=True,
                        text=True,
                    )
                    if completed.returncode != 0:
                        self.skipTest("directory junctions are unavailable")
                else:
                    nested.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory links are unavailable: {error}")

            result = RecoveryReconciler(editor).reconcile(transaction, intent)

            self.assertEqual(result.status, RecoveryStatus.CONFLICT)
            self.assertEqual(external_file.read_text(encoding="utf-8"), "outside\n")


if __name__ == "__main__":
    unittest.main()
