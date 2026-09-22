from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from agent.workspace.paths import canonical_workspace_relative_path


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
    READ_DENIED = "read_denied"
    WRITE_DENIED = "write_denied"
    SENSITIVE_DATA_DETECTED = "sensitive_data_detected"
    RECOVERY_CONFLICT = "recovery_conflict"


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

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "task_ids", _sequence_tuple(self.task_ids, "task_ids")
        )


@dataclass(frozen=True, slots=True)
class WorkspaceTransaction:
    path: str
    existed: bool
    original_content: str
    working_content: str
    base_hash: str | None
    original_mode: int | None
    task_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "task_ids", _sequence_tuple(self.task_ids, "task_ids")
        )


class RecoveryStatus(str, Enum):
    """Filesystem state observed while reconciling a pending write."""

    SAFE_TO_APPLY = "safe_to_apply"
    ALREADY_APPLIED = "already_applied"
    CONFLICT = "conflict"


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WRITE_INTENT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class WriteIntent:
    """Checkpoint-safe description of one file write, without file content."""

    schema_version: int
    write_id: str
    run_id: str
    task_id: str
    task_index: int
    repair_attempt: int
    path: str
    operation: EditOperation
    existed: bool
    before_hash: str | None
    expected_after_hash: str
    task_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_ids", _sequence_tuple(self.task_ids, "task_ids"))
        if not isinstance(self.operation, EditOperation):
            try:
                object.__setattr__(self, "operation", EditOperation(self.operation))
            except (TypeError, ValueError) as error:
                raise ValueError("operation must be a valid EditOperation.") from error
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != WRITE_INTENT_SCHEMA_VERSION
        ):
            raise ValueError("Unsupported WriteIntent schema version.")
        for name in ("write_id", "run_id", "task_id", "path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string.")
        if (
            isinstance(self.task_index, bool)
            or not isinstance(self.task_index, int)
            or self.task_index < 0
        ):
            raise ValueError("task_index must be a non-negative integer.")
        if (
            isinstance(self.repair_attempt, bool)
            or not isinstance(self.repair_attempt, int)
            or self.repair_attempt < 0
        ):
            raise ValueError("repair_attempt must be a non-negative integer.")
        canonical = canonical_workspace_relative_path(
            self.path, allow_root=False
        )
        if canonical != self.path:
            raise ValueError("WriteIntent path must be canonical and relative.")
        if not isinstance(self.existed, bool):
            raise ValueError("existed must be a boolean.")
        if self.operation is EditOperation.EDIT and not self.existed:
            raise ValueError("Edit intents must describe an existing file.")
        if self.operation is EditOperation.CREATE and self.existed:
            raise ValueError("Create intents must describe a missing file.")
        if self.before_hash is not None:
            _validate_hash(self.before_hash, "before_hash")
        _validate_hash(self.expected_after_hash, "expected_after_hash")
        if self.existed and self.before_hash == self.expected_after_hash:
            raise ValueError("A net-zero edit must not create a WriteIntent.")
        if not self.task_ids or any(
            not isinstance(task_id, str) or not task_id for task_id in self.task_ids
        ):
            raise ValueError("task_ids must contain non-empty strings.")
        expected_id = self._stable_write_id()
        if self.write_id != expected_id:
            raise ValueError("write_id does not match the durable write identity.")

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        task_id: str,
        task_index: int,
        repair_attempt: int,
        transaction: WorkspaceTransaction,
        expected_after_hash: str,
    ) -> "WriteIntent":
        path = canonical_workspace_relative_path(
            transaction.path, allow_root=False
        )
        if path is None:
            raise ValueError("Transaction path is not a canonical workspace path.")
        computed_after_hash = hashlib.sha256(
            transaction.working_content.encode("utf-8")
        ).hexdigest()
        if expected_after_hash != computed_after_hash:
            raise ValueError(
                "expected_after_hash must match the transaction working content."
            )
        operation = (
            EditOperation.EDIT if transaction.existed else EditOperation.CREATE
        )
        seed = {
            "schema_version": WRITE_INTENT_SCHEMA_VERSION,
            "run_id": run_id,
            "task_id": task_id,
            "task_index": task_index,
            "repair_attempt": repair_attempt,
            "path": path,
            "operation": operation.value,
        }
        write_id = hashlib.sha256(_canonical_json(seed)).hexdigest()
        return cls(
            schema_version=WRITE_INTENT_SCHEMA_VERSION,
            write_id=write_id,
            run_id=run_id,
            task_id=task_id,
            task_index=task_index,
            repair_attempt=repair_attempt,
            path=path,
            operation=operation,
            existed=transaction.existed,
            before_hash=transaction.base_hash,
            expected_after_hash=expected_after_hash,
            task_ids=transaction.task_ids,
        )

    def _stable_write_id(self) -> str:
        seed = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "task_index": self.task_index,
            "repair_attempt": self.repair_attempt,
            "path": self.path,
            "operation": self.operation.value,
        }
        return hashlib.sha256(_canonical_json(seed)).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "write_id": self.write_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "task_index": self.task_index,
            "repair_attempt": self.repair_attempt,
            "path": self.path,
            "operation": self.operation.value,
            "existed": self.existed,
            "before_hash": self.before_hash,
            "expected_after_hash": self.expected_after_hash,
            "task_ids": list(self.task_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "WriteIntent":
        if not isinstance(value, Mapping):
            raise ValueError("WriteIntent must be a mapping.")
        required = {
            "schema_version",
            "write_id",
            "run_id",
            "task_id",
            "task_index",
            "repair_attempt",
            "path",
            "operation",
            "existed",
            "before_hash",
            "expected_after_hash",
            "task_ids",
        }
        if set(value) != required:
            raise ValueError("WriteIntent fields are incomplete or unknown.")
        operation = value["operation"]
        try:
            operation = EditOperation(operation)
        except (TypeError, ValueError) as error:
            raise ValueError("WriteIntent operation is invalid.") from error
        return cls(
            schema_version=value["schema_version"],
            write_id=value["write_id"],
            run_id=value["run_id"],
            task_id=value["task_id"],
            task_index=value["task_index"],
            repair_attempt=value["repair_attempt"],
            path=value["path"],
            operation=operation,
            existed=value["existed"],
            before_hash=value["before_hash"],
            expected_after_hash=value["expected_after_hash"],
            task_ids=value["task_ids"],
        )


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    status: RecoveryStatus
    path: str
    current_hash: str | None = None
    expected_after_hash: str | None = None
    error_code: EditErrorCode | None = None
    message: str = ""
    edit_result: EditResult | None = None


def _validate_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 hex string.")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TransactionResult:
    transaction: WorkspaceTransaction | None = None
    edit_result: EditResult | None = None

    @property
    def ok(self) -> bool:
        return self.transaction is not None and self.edit_result is None


def _sequence_tuple(value: object, name: str) -> tuple:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise ValueError(f"{name} must be a non-string sequence.")
    return tuple(value)
