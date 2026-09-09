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
    PATH_INVALID = "path_invalid"
    READ_FAILED = "read_failed"
    INVALID_PLAN = "invalid_plan"
    INVALID_STATE = "invalid_state"
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
