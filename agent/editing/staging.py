"""Pure in-memory validation and staging for one file transaction."""

from dataclasses import replace

from agent.editing.models import (
    EditErrorCode,
    EditOperation,
    EditProposal,
    EditResult,
    EditStatus,
    TransactionResult,
    WorkspaceTransaction,
)
from agent.editing.text import apply_unique_replacement, sha256


def stage_transaction(
    transaction: WorkspaceTransaction, proposal: EditProposal
) -> TransactionResult:
    task_ids = (*transaction.task_ids, proposal.task_id)
    if not proposal.task_id.strip() or proposal.task_id in transaction.task_ids:
        return _failed(
            transaction,
            task_ids,
            EditErrorCode.TASK_ID_INVALID,
            "Each staged edit must have a non-empty, unique task_id.",
        )
    if proposal.path != transaction.path:
        return _failed(
            transaction,
            task_ids,
            EditErrorCode.PATH_INVALID,
            "Every proposal in a file transaction must target the same path.",
        )

    is_first_create = not transaction.existed and not transaction.task_ids
    if proposal.operation is EditOperation.CREATE:
        if not is_first_create:
            error_code = (
                EditErrorCode.FILE_EXISTS
                if transaction.existed
                else EditErrorCode.INVALID_MODEL_RESPONSE
            )
            return _failed(
                transaction,
                task_ids,
                error_code,
                "CREATE is only valid as the first edit for a missing file.",
            )
        updated = proposal.new_text
    else:
        if not transaction.existed and not transaction.task_ids:
            return _failed(
                transaction,
                task_ids,
                EditErrorCode.FILE_NOT_FOUND,
                "The target file does not exist.",
            )
        if transaction.existed and proposal.base_hash != transaction.base_hash:
            return _failed(
                transaction,
                task_ids,
                EditErrorCode.HASH_MISMATCH,
                "The proposal was not based on the transaction's original file.",
            )
        staged = apply_unique_replacement(transaction.working_content, proposal)
        if isinstance(staged, EditErrorCode):
            messages = {
                EditErrorCode.EMPTY_OLD_TEXT: "old_text must not be empty.",
                EditErrorCode.MATCH_NOT_FOUND: (
                    "old_text was not found in the current working copy."
                ),
                EditErrorCode.MATCH_AMBIGUOUS: (
                    "old_text is ambiguous in the current working copy; "
                    "include more surrounding context."
                ),
            }
            return _failed(transaction, task_ids, staged, messages[staged])
        updated = staged

    try:
        updated_bytes = updated.encode("utf-8")
    except UnicodeEncodeError:
        return _failed(
            transaction,
            task_ids,
            EditErrorCode.ENCODING_ERROR,
            "New file content must be valid UTF-8 text.",
        )
    if updated == transaction.working_content and not (
        is_first_create and proposal.operation is EditOperation.CREATE
    ):
        return TransactionResult(
            edit_result=EditResult(
                status=EditStatus.NOOP,
                path=transaction.path,
                message="The proposal does not change the working copy.",
                before_hash=transaction.base_hash,
                after_hash=sha256(updated_bytes),
                task_ids=task_ids,
            )
        )
    return TransactionResult(
        transaction=replace(
            transaction,
            working_content=updated,
            task_ids=task_ids,
        )
    )


def _failed(
    transaction: WorkspaceTransaction,
    task_ids: tuple[str, ...],
    error_code: EditErrorCode,
    message: str,
) -> TransactionResult:
    return TransactionResult(
        edit_result=EditResult(
            status=EditStatus.REJECTED,
            path=transaction.path,
            error_code=error_code,
            message=message,
            before_hash=transaction.base_hash,
            task_ids=task_ids,
        )
    )
