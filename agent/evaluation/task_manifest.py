"""Public API for strict offline validation of trusted S5a task manifests."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


MANIFEST_SCHEMA_VERSION = 1
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class ManifestIssueCode(str, Enum):
    INVALID_JSON = "invalid_json"
    INVALID_DOCUMENT = "invalid_document"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_VALUE = "invalid_value"
    INVALID_PATH = "invalid_path"
    INVALID_REVISION = "invalid_revision"
    UNKNOWN_REVISION = "unknown_revision"
    REPOSITORY_MISMATCH = "repository_mismatch"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    MANIFEST_HASH_MISMATCH = "manifest_hash_mismatch"
    TASK_HASH_MISMATCH = "task_hash_mismatch"
    DUPLICATE_TASK_ID = "duplicate_task_id"
    INVALID_CHECK = "invalid_check"
    SCOPE_ORACLE_OVERLAP = "scope_oracle_overlap"
    SCOPE_NOT_IN_CHANGE = "scope_not_in_change"
    ORACLE_FILE_MISSING = "oracle_file_missing"
    ORACLE_HASH_MISMATCH = "oracle_hash_mismatch"
    PROVENANCE_MISMATCH = "provenance_mismatch"


@dataclass(frozen=True, slots=True)
class ManifestIssue:
    code: ManifestIssueCode
    location: str
    message: str


@dataclass(frozen=True, slots=True)
class ManifestValidationResult:
    valid: bool
    manifest_id: str | None
    task_ids: tuple[str, ...]
    issues: tuple[ManifestIssue, ...]


class _DuplicateJsonKey(ValueError):
    pass


def environment_summary(repository_root: Path) -> dict[str, str]:
    """Return the local environment identity used by S5a manifest validation."""
    lock_path = Path(repository_root) / "uv.lock"
    lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    return {
        "os_family": platform.system(),
        "architecture": platform.machine(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "lock_path": "uv.lock",
        "lock_sha256": lock_digest,
    }


def manifest_content_hash(document: Mapping[str, Any]) -> str:
    """Hash canonical JSON excluding its own ``manifest_sha256`` field."""
    content = {key: value for key, value in document.items() if key != "manifest_sha256"}
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_task_manifest(
    path: Path,
    *,
    repository_root: Path,
    expected_repository: str,
    expected_environment: Mapping[str, str],
) -> ManifestValidationResult:
    """Load unique-key UTF-8 JSON and validate it without running task checks."""
    try:
        with Path(path).open("rb") as manifest_file:
            content = manifest_file.read(_MAX_MANIFEST_BYTES + 1)
        if len(content) > _MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds its size limit.")
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        ValueError,
        RecursionError,
    ):
        return ManifestValidationResult(
            False,
            None,
            (),
            (ManifestIssue(ManifestIssueCode.INVALID_JSON, "$", "Manifest JSON is invalid or exceeds the size limit."),),
        )
    return validate_task_manifest(
        document,
        repository_root=repository_root,
        expected_repository=expected_repository,
        expected_environment=expected_environment,
    )


def validate_task_manifest(
    document: object,
    *,
    repository_root: Path,
    expected_repository: str,
    expected_environment: Mapping[str, str],
) -> ManifestValidationResult:
    """Validate a trusted document and pinned evidence; never execute its argv."""
    from agent.evaluation._manifest_validation import validate_task_manifest as validate

    return validate(
        document,
        repository_root=repository_root,
        expected_repository=expected_repository,
        expected_environment=expected_environment,
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")
