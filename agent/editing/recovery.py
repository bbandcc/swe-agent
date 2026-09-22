"""Read-only reconciliation for durable file-write intents."""

from __future__ import annotations

from agent.editing.models import (
    EditErrorCode,
    EditResult,
    EditStatus,
    RecoveryResult,
    RecoveryStatus,
    WorkspaceTransaction,
    WriteIntent,
)
from agent.editing.text import make_diff, sha256
from agent.editing.workspace import WorkspaceEditor


class RecoveryReconciler:
    """Compare a pending intent with the live workspace without writing."""

    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    def reconcile(
        self, transaction: WorkspaceTransaction, intent: WriteIntent
    ) -> RecoveryResult:
        if transaction.path != intent.path:
            return self._conflict(
                intent,
                "The pending write path does not match the transaction.",
            )
        if transaction.existed != intent.existed:
            return self._conflict(
                intent,
                "The pending write operation does not match the transaction.",
            )
        if transaction.base_hash != intent.before_hash:
            return self._conflict(
                intent,
                "The pending write baseline does not match the transaction.",
            )
        try:
            expected_from_transaction = sha256(
                transaction.working_content.encode("utf-8")
            )
        except UnicodeEncodeError:
            return self._conflict(
                intent,
                "New file content must be valid UTF-8 text.",
                error_code=EditErrorCode.ENCODING_ERROR,
            )
        if expected_from_transaction != intent.expected_after_hash:
            return self._conflict(
                intent,
                "The pending write expected hash does not match the transaction.",
            )
        snapshot = self._editor.snapshot(intent.path)
        if snapshot.error_code is not None:
            return self._conflict(
                intent,
                "The pending write target could not be safely inspected.",
            )
        current_hash = snapshot.content_hash if snapshot.exists else None
        if current_hash == intent.expected_after_hash:
            return RecoveryResult(
                status=RecoveryStatus.ALREADY_APPLIED,
                path=intent.path,
                current_hash=current_hash,
                expected_after_hash=intent.expected_after_hash,
                edit_result=self._already_applied(transaction, intent),
            )
        if current_hash == intent.before_hash:
            return RecoveryResult(
                status=RecoveryStatus.SAFE_TO_APPLY,
                path=intent.path,
                current_hash=current_hash,
                expected_after_hash=intent.expected_after_hash,
            )
        if not intent.existed and current_hash is None:
            return RecoveryResult(
                status=RecoveryStatus.SAFE_TO_APPLY,
                path=intent.path,
                current_hash=None,
                expected_after_hash=intent.expected_after_hash,
            )
        return self._conflict(
            intent,
            "The pending write target has an unexpected current hash.",
            current_hash=current_hash,
        )

    @staticmethod
    def _already_applied(
        transaction: WorkspaceTransaction, intent: WriteIntent
    ) -> EditResult:
        return EditResult(
            status=EditStatus.APPLIED,
            path=intent.path,
            before_hash=intent.before_hash,
            after_hash=intent.expected_after_hash,
            diff=make_diff(
                intent.path,
                transaction.original_content,
                transaction.working_content,
                existed=transaction.existed,
            ),
            task_ids=intent.task_ids,
        )

    @staticmethod
    def _conflict(
        intent: WriteIntent,
        message: str,
        *,
        current_hash: str | None = None,
        error_code: EditErrorCode = EditErrorCode.RECOVERY_CONFLICT,
    ) -> RecoveryResult:
        return RecoveryResult(
            status=RecoveryStatus.CONFLICT,
            path=intent.path,
            current_hash=current_hash,
            expected_after_hash=intent.expected_after_hash,
            error_code=error_code,
            message=message,
        )
