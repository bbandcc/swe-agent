"""Deterministic, workspace-bound text editing with file transactions."""

import stat
from dataclasses import replace
from pathlib import Path

from agent.editing.models import (
    EditErrorCode,
    EditProposal,
    EditResult,
    EditStatus,
    TransactionResult,
    WorkspaceSnapshot,
    WorkspaceTransaction,
)
from agent.editing.files import create_new, replace_existing
from agent.editing.staging import stage_transaction
from agent.editing.text import make_diff, sha256
from agent.workspace import (
    PathResolution,
    WorkspacePathErrorCode,
    WorkspacePathResolver,
)
from agent.runtime.secrets import KnownSecretFilter, SENSITIVE_DATA_MESSAGE


class WorkspaceEditor:
    """Validate edits in memory and commit one final version of each file."""

    def __init__(
        self,
        root: str | Path,
        *,
        secret_filter: KnownSecretFilter | None = None,
    ) -> None:
        self._resolver = WorkspacePathResolver(root)
        self._secret_filter = secret_filter

    def snapshot(self, path: str) -> WorkspaceSnapshot:
        """Read one UTF-8 file through the same boundary used for writes."""
        resolution = self._resolver.resolve_file(
            path, must_exist=False, allow_absolute=False
        )
        if not resolution.ok:
            return WorkspaceSnapshot(
                path=path,
                exists=False,
                error_code=_map_resolution_error(resolution),
                message=resolution.message,
            )
        target = resolution.path
        assert target is not None
        relative_path = resolution.relative_path or path
        if not target.exists():
            return WorkspaceSnapshot(path=relative_path, exists=False)
        try:
            content_bytes = target.read_bytes()
        except OSError as error:
            return WorkspaceSnapshot(
                path=relative_path,
                exists=True,
                error_code=EditErrorCode.READ_FAILED,
                message=f"Could not read the target file: {error}",
            )
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return WorkspaceSnapshot(
                path=relative_path,
                exists=True,
                error_code=EditErrorCode.ENCODING_ERROR,
                message="Only UTF-8 text files can be read.",
            )
        if self._has_sensitive_code(content):
            return WorkspaceSnapshot(
                path=relative_path,
                exists=True,
                error_code=EditErrorCode.SENSITIVE_DATA_DETECTED,
                message=SENSITIVE_DATA_MESSAGE,
            )
        return WorkspaceSnapshot(
            path=relative_path,
            exists=True,
            content=content,
            content_hash=sha256(content_bytes),
        )

    def begin(self, path: str) -> TransactionResult:
        """Capture the original file once and create an in-memory working copy."""
        resolution = self._resolver.resolve_file(
            path, must_exist=False, allow_absolute=False
        )
        if not resolution.ok:
            return TransactionResult(
                edit_result=_rejected_from_resolution(path, resolution)
            )
        target = resolution.path
        assert target is not None
        relative_path = resolution.relative_path or path
        if not target.exists():
            return TransactionResult(
                transaction=WorkspaceTransaction(
                    path=relative_path,
                    existed=False,
                    original_content="",
                    working_content="",
                    base_hash=None,
                    original_mode=None,
                )
            )
        try:
            original_bytes = target.read_bytes()
            original_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError as error:
            return TransactionResult(
                edit_result=EditResult(
                    status=EditStatus.REJECTED,
                    path=relative_path,
                    error_code=EditErrorCode.READ_FAILED,
                    message=f"Could not read the target file: {error}",
                )
            )
        before_hash = sha256(original_bytes)
        try:
            original = original_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return TransactionResult(
                edit_result=EditResult(
                    status=EditStatus.REJECTED,
                    path=relative_path,
                    error_code=EditErrorCode.ENCODING_ERROR,
                    message="Only UTF-8 text files can be edited.",
                    before_hash=before_hash,
                )
            )
        if self._has_sensitive_code(original):
            return TransactionResult(
                edit_result=EditResult(
                    status=EditStatus.REJECTED,
                    path=relative_path,
                    error_code=EditErrorCode.SENSITIVE_DATA_DETECTED,
                    message=SENSITIVE_DATA_MESSAGE,
                    before_hash=before_hash,
                )
            )
        return TransactionResult(
            transaction=WorkspaceTransaction(
                path=relative_path,
                existed=True,
                original_content=original,
                working_content=original,
                base_hash=before_hash,
                original_mode=original_mode,
            )
        )

    def stage(
        self, transaction: WorkspaceTransaction, proposal: EditProposal
    ) -> TransactionResult:
        """Apply one proposal to a working copy without touching the file."""
        if self._secret_filter is not None and (
            self._has_sensitive_code(transaction.original_content)
            or self._has_sensitive_code(transaction.working_content)
            or self._has_sensitive_code(proposal.old_text)
            or self._has_sensitive_code(proposal.new_text)
        ):
            return TransactionResult(
                edit_result=_transaction_error(
                    transaction,
                    EditErrorCode.SENSITIVE_DATA_DETECTED,
                    SENSITIVE_DATA_MESSAGE,
                    task_id=proposal.task_id,
                )
            )
        return stage_transaction(transaction, proposal)

    def commit(self, transaction: WorkspaceTransaction) -> EditResult:
        """Recheck the baseline and replace or create the target exactly once."""
        if self._has_sensitive_code(transaction.original_content) or self._has_sensitive_code(
            transaction.working_content
        ):
            return _transaction_error(
                transaction,
                EditErrorCode.SENSITIVE_DATA_DETECTED,
                SENSITIVE_DATA_MESSAGE,
            )
        if not transaction.task_ids:
            return EditResult(
                status=EditStatus.NOOP,
                path=transaction.path,
                message="The transaction contains no staged edits.",
                before_hash=transaction.base_hash,
                after_hash=transaction.base_hash,
            )
        resolution = self._resolver.resolve_file(
            transaction.path, must_exist=False, allow_absolute=False
        )
        if not resolution.ok:
            result = _rejected_from_resolution(transaction.path, resolution)
            return replace(result, task_ids=transaction.task_ids)
        target = resolution.path
        assert target is not None

        current_bytes: bytes | None = None
        if transaction.existed:
            if not target.is_file():
                return _transaction_error(
                    transaction,
                    EditErrorCode.FILE_NOT_FOUND,
                    "The target file no longer exists.",
                )
            try:
                current_bytes = target.read_bytes()
            except OSError as error:
                return _transaction_error(
                    transaction,
                    EditErrorCode.READ_FAILED,
                    f"Could not re-read the target file: {error}",
                )
            current_hash = sha256(current_bytes)
            if current_hash != transaction.base_hash:
                return _transaction_error(
                    transaction,
                    EditErrorCode.HASH_MISMATCH,
                    "The file changed while edits were staged.",
                    before_hash=current_hash,
                )
        elif target.exists():
            return _transaction_error(
                transaction,
                EditErrorCode.FILE_EXISTS,
                "The target file was created while edits were staged.",
            )

        try:
            updated_bytes = transaction.working_content.encode("utf-8")
        except UnicodeEncodeError:
            return _transaction_error(
                transaction,
                EditErrorCode.ENCODING_ERROR,
                "New file content must be valid UTF-8 text.",
            )
        if transaction.existed and current_bytes == updated_bytes:
            return EditResult(
                status=EditStatus.NOOP,
                path=transaction.path,
                before_hash=transaction.base_hash,
                after_hash=sha256(updated_bytes),
                diff="",
                task_ids=transaction.task_ids,
            )
        write_failure = (
            replace_existing(target, updated_bytes, transaction)
            if transaction.existed
            else create_new(target, updated_bytes)
        )
        if write_failure is not None:
            error_code, message = write_failure
            return _transaction_error(
                transaction,
                error_code,
                message,
            )
        return EditResult(
            status=EditStatus.APPLIED,
            path=transaction.path,
            before_hash=transaction.base_hash,
            after_hash=sha256(updated_bytes),
            diff=make_diff(
                transaction.path,
                transaction.original_content,
                transaction.working_content,
                existed=transaction.existed,
            ),
            task_ids=transaction.task_ids,
        )

    def _has_sensitive_code(self, value: str | None) -> bool:
        return bool(
            self._secret_filter is not None
            and isinstance(value, str)
            and self._secret_filter.sanitize(
                value, code_bearing=True
            ).code_bearing
        )

    def apply(self, proposal: EditProposal) -> EditResult:
        """Convenience interface for a one-proposal file transaction."""
        started = self.begin(proposal.path)
        if not started.ok:
            assert started.edit_result is not None
            return replace(started.edit_result, task_ids=(proposal.task_id,))
        assert started.transaction is not None
        staged = self.stage(started.transaction, proposal)
        if not staged.ok:
            assert staged.edit_result is not None
            return staged.edit_result
        assert staged.transaction is not None
        return self.commit(staged.transaction)

def _map_resolution_error(resolution: PathResolution) -> EditErrorCode:
    mapping = {
        WorkspacePathErrorCode.WORKSPACE_NOT_FOUND: (
            EditErrorCode.WORKSPACE_NOT_FOUND
        ),
        WorkspacePathErrorCode.WORKSPACE_INVALID: EditErrorCode.WORKSPACE_INVALID,
        WorkspacePathErrorCode.EXPECTED_FILE: EditErrorCode.READ_FAILED,
    }
    return mapping.get(resolution.error_code, EditErrorCode.PATH_INVALID)


def _rejected_from_resolution(
    path: str, resolution: PathResolution
) -> EditResult:
    return EditResult(
        status=EditStatus.REJECTED,
        path=path,
        error_code=_map_resolution_error(resolution),
        message=resolution.message,
    )


def _transaction_error(
    transaction: WorkspaceTransaction,
    error_code: EditErrorCode,
    message: str,
    *,
    before_hash: str | None = None,
    task_id: str | None = None,
) -> EditResult:
    task_ids = transaction.task_ids
    if task_id is not None:
        task_ids = (*task_ids, task_id)
    return EditResult(
        status=EditStatus.REJECTED,
        path=transaction.path,
        error_code=error_code,
        message=message,
        before_hash=(
            transaction.base_hash if before_hash is None else before_hash
        ),
        task_ids=task_ids,
    )
