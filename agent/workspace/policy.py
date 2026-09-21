"""Small, trusted workspace read/write authorization contract.

The policy is deliberately path based.  It is not a general ACL or glob
engine: callers provide a finite set of relative path prefixes and the
runtime adds a small fixed set of credential and VCS paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable


class WorkspaceAccessErrorCode(str, Enum):
    READ_DENIED = "read_denied"
    WRITE_DENIED = "write_denied"


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    error_code: WorkspaceAccessErrorCode | None = None
    message: str = ""


_FIXED_PROTECTED_PREFIXES = (
    ".git",
    ".ssh",
    ".aws",
    "credentials",
)
_FIXED_PROTECTED_FILES = {
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}


@dataclass(frozen=True, slots=True)
class WorkspaceAccessPolicy:
    """Trusted, immutable path policy used by one run.

    ``hidden_paths`` and ``oracle_paths`` are protected from model reads and
    writes.  They are names supplied by trusted configuration, not model
    output.  All configured values are relative path prefixes; arbitrary glob
    syntax is intentionally unsupported.
    """

    hidden_paths: tuple[str, ...] = ()
    oracle_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        hidden = _normalize_paths(self.hidden_paths, "hidden_paths")
        oracle = _normalize_paths(self.oracle_paths, "oracle_paths")
        object.__setattr__(self, "hidden_paths", tuple(sorted(hidden)))
        object.__setattr__(self, "oracle_paths", tuple(sorted(oracle)))

    @classmethod
    def default(cls) -> "WorkspaceAccessPolicy":
        return cls()

    @property
    def configured_paths(self) -> tuple[str, ...]:
        return (*self.hidden_paths, *self.oracle_paths)

    def is_protected(self, relative_path: str) -> bool:
        normalized = _normalize_one(relative_path, allow_dot=True)
        if normalized is None:
            return True
        if normalized == ".":
            return False
        parts = tuple(normalized.split("/"))
        if any(part in _FIXED_PROTECTED_PREFIXES for part in parts):
            return True
        basename = parts[-1]
        if (
            basename == ".env"
            or basename.startswith(".env.")
            or basename in _FIXED_PROTECTED_FILES
        ):
            return True
        return any(
            normalized == prefix or normalized.startswith(prefix + "/")
            for prefix in self.configured_paths
        )

    def check_read(self, relative_path: str) -> AccessDecision:
        if self.is_protected(relative_path):
            return AccessDecision(
                False,
                WorkspaceAccessErrorCode.READ_DENIED,
                "Workspace read access is denied by the trusted policy.",
            )
        return AccessDecision(True)

    def check_write(self, relative_path: str) -> AccessDecision:
        if self.is_protected(relative_path):
            return AccessDecision(
                False,
                WorkspaceAccessErrorCode.WRITE_DENIED,
                "Workspace write access is denied by the trusted policy.",
            )
        return AccessDecision(True)

    def to_digest_dict(self) -> dict[str, list[str]]:
        """Return only semantic, non-secret policy values for config hashing."""
        return {
            "hidden_paths": list(self.hidden_paths),
            "oracle_paths": list(self.oracle_paths),
        }


def _normalize_paths(value: Iterable[str], name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{name} must be a sequence of relative paths.")
    try:
        values = tuple(value)
    except TypeError as error:
        raise ValueError(f"{name} must be a sequence of relative paths.") from error
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"{name} must contain strings.")
        path = _normalize_one(item)
        if path is None:
            raise ValueError(f"{name} must contain safe relative paths.")
        if path in normalized:
            raise ValueError(f"{name} must not contain duplicate paths.")
        normalized.append(path)
    return tuple(normalized)


def _normalize_one(value: str, *, allow_dot: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    normalized = value.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if (
        not normalized
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
    ):
        return "." if allow_dot and normalized in {"", "."} else None
    normalized = normalized.strip("/")
    parts = [part for part in posix.parts if part not in {"", "."}]
    if not parts:
        return "." if allow_dot else None
    return os.path.normcase("/".join(parts)).replace("\\", "/")


def parse_configured_paths(value: str, name: str) -> tuple[str, ...]:
    """Parse a trusted JSON array of path prefixes from environment config."""
    import json

    if not value.strip():
        return ()
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a JSON array of relative paths.") from error
    if not isinstance(decoded, list):
        raise ValueError(f"{name} must be a JSON array of relative paths.")
    return _normalize_paths(decoded, name)


def configured_workspace_access_policy(environ=None) -> WorkspaceAccessPolicy:
    """Load only trusted path policy values for the non-durable graph entry."""
    import os as _os

    source = _os.environ if environ is None else environ
    return WorkspaceAccessPolicy(
        hidden_paths=parse_configured_paths(
            source.get("SWE_AGENT_HIDDEN_PATHS", ""),
            "SWE_AGENT_HIDDEN_PATHS",
        ),
        oracle_paths=parse_configured_paths(
            source.get("SWE_AGENT_ORACLE_PATHS", ""),
            "SWE_AGENT_ORACLE_PATHS",
        ),
    )
