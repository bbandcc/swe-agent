"""Fixed limits and opaque continuation cursors for workspace read tools."""

import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_SEARCH_FILES = 128
MAX_SEARCH_RESULTS = 100
MAX_SEARCH_BYTES = 1_048_576
MAX_SEARCH_TERM_CHARS = 256
MAX_SEARCH_SCAN_ENTRIES = 4_096
MAX_CURSOR_CHARS = 131_072
MAX_RAW_PAGE_BYTES = 32_768
MAX_TREE_DEPTH = 8
MAX_TREE_ENTRIES = 128
MAX_TREE_SCAN_ENTRIES = 4_096
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cache",
        "node_modules", "bower_components", "vendor", "build", "dist",
        "target", "coverage", ".next", ".turbo", "site-packages",
    }
)
_CURSOR_SCHEMA = 1
_CURSOR_CHECKSUM_PREFIX = b"swe-agent-read-cursor-v1\0"


def content_version(path: Path) -> str:
    """Return a stable metadata fingerprint without reading unbounded content."""
    metadata = path.stat(follow_symlinks=False)
    fields = {
        "device": getattr(metadata, "st_dev", None),
        "inode": getattr(metadata, "st_ino", None),
        "size": metadata.st_size,
        "modified_ns": getattr(metadata, "st_mtime_ns", None),
        "changed_ns": getattr(metadata, "st_ctime_ns", None),
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def encode_cursor(kind: str, values: Mapping[str, Any]) -> str:
    payload = {"schema": _CURSOR_SCHEMA, "kind": kind, **dict(values)}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    checksum = hashlib.sha256(_CURSOR_CHECKSUM_PREFIX + encoded).hexdigest()[:32]
    token = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    return f"{token}.{checksum}"


def decode_cursor(value: str, expected_kind: str) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_CHARS:
        return None
    try:
        encoded_part, checksum = value.split(".", 1)
        padding = "=" * ((4 - len(encoded_part) % 4) % 4)
        encoded = base64.urlsafe_b64decode((encoded_part + padding).encode("ascii"))
        expected = hashlib.sha256(_CURSOR_CHECKSUM_PREFIX + encoded).hexdigest()[:32]
        if checksum != expected:
            return None
        payload = json.loads(encoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _CURSOR_SCHEMA
        or payload.get("kind") != expected_kind
    ):
        return None
    return payload


def tool_failure(
    path: str | None,
    error_code: str,
    message: str,
    *,
    stale: bool = False,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "ok": False,
        "path": path,
        "error_code": error_code,
        "message": message,
        "stale": stale,
    }
    if warnings is not None:
        result["warnings"] = warnings
    return result


def clip_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError as error:
            clipped = clipped[: error.start]
    return "", True


def stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MAX_RAW_PAGE_BYTES",
    "MAX_CURSOR_CHARS",
    "MAX_SEARCH_BYTES",
    "MAX_SEARCH_FILES",
    "MAX_SEARCH_RESULTS",
    "MAX_SEARCH_SCAN_ENTRIES",
    "MAX_SEARCH_TERM_CHARS",
    "MAX_TREE_DEPTH",
    "MAX_TREE_ENTRIES",
    "MAX_TREE_SCAN_ENTRIES",
    "IGNORED_DIRECTORY_NAMES",
    "clip_utf8",
    "content_version",
    "decode_cursor",
    "encode_cursor",
    "stable_digest",
    "tool_failure",
]
