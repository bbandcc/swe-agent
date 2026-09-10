from dataclasses import dataclass
from enum import Enum


class EditOperation(str, Enum):
    EDIT = "edit"
    CREATE = "create"


class EditStatus(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    NOOP = "noop"


class EditErrorCode(str, Enum):
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_INVALID = "workspace_invalid"
    PATH_INVALID = "path_invalid"
    TASK_ID_INVALID = "task_id_invalid"
    READ_FAILED = "read_failed"
    FILE_NOT_FOUND = "file_not_found"
    FILE_EXISTS = "file_exists"
    HASH_MISMATCH = "hash_mismatch"
    EMPTY_OLD_TEXT = "empty_old_text"
    MATCH_NOT_FOUND = "match_not_found"
    MATCH_AMBIGUOUS = "match_ambiguous"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    ENCODING_ERROR = "encoding_error"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True, slots=True)
class EditProposal:
    task_id: str
    path: str
    operation: EditOperation
    new_text: str
    base_hash: str | None = None
    old_text: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    path: str
    exists: bool
    content: str | None = None
    content_hash: str | None = None
    error_code: EditErrorCode | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class EditResult:
    status: EditStatus
    path: str
    error_code: EditErrorCode | None = None
    message: str = ""
    before_hash: str | None = None
    after_hash: str | None = None
    diff: str = ""
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceTransaction:
    path: str
    existed: bool
    original_content: str
    working_content: str
    base_hash: str | None
    original_mode: int | None
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransactionResult:
    transaction: WorkspaceTransaction | None = None
    edit_result: EditResult | None = None

    @property
    def ok(self) -> bool:
        return self.transaction is not None and self.edit_result is None
