"""Small content-addressed artifact storage for durable runtime evidence."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO

ARTIFACT_SCHEMA_VERSION = 1
ARTIFACTS_DIRECTORY = "artifacts"
DEFAULT_ARTIFACT_QUOTA_BYTES = 64 * 1024 * 1024
_KIND_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ArtifactErrorCode(str, Enum):
    INVALID = "invalid_artifact"
    OPEN_FAILED = "artifact_open_failed"
    QUOTA_EXCEEDED = "artifact_quota_exceeded"
    WRITE_FAILED = "artifact_write_failed"
    PUBLISH_FAILED = "artifact_publish_failed"
    CLEANUP_FAILED = "artifact_cleanup_failed"


class ArtifactStoreError(RuntimeError):
    """Structured failure from the local artifact store."""

    def __init__(self, code: ArtifactErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Versioned, content-addressed reference to a runtime-root file."""

    kind: str
    path: str
    sha256: str
    size: int
    media_type: str
    redacted: bool
    truncated: bool
    schema_version: int = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError("Unsupported artifact schema version.")
        if (
            not isinstance(self.kind, str)
            or not self.kind
            or not _KIND_PATTERN.fullmatch(self.kind)
        ):
            raise ValueError("Artifact kind must be a safe identifier.")
        if not isinstance(self.path, str) or not self.path or "\x00" in self.path:
            raise ValueError("Artifact path must be non-empty.")
        relative = Path(self.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Artifact path must be relative to runtime_root.")
        if (
            not isinstance(self.sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
        ):
            raise ValueError("Artifact sha256 must be a lowercase digest.")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("Artifact size must be a non-negative integer.")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("Artifact media_type must be non-empty.")
        if not isinstance(self.redacted, bool) or not isinstance(self.truncated, bool):
            raise ValueError("Artifact flags must be boolean.")

    @property
    def relative_path(self) -> str:
        return self.path

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
            "redacted": self.redacted,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ArtifactRef":
        return cls(
            kind=value["kind"],  # type: ignore[arg-type]
            path=value["path"],  # type: ignore[arg-type]
            sha256=value["sha256"],  # type: ignore[arg-type]
            size=value["size"],  # type: ignore[arg-type]
            media_type=value["media_type"],  # type: ignore[arg-type]
            redacted=value["redacted"],  # type: ignore[arg-type]
            truncated=value["truncated"],  # type: ignore[arg-type]
            schema_version=value.get("schema_version", ARTIFACT_SCHEMA_VERSION),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    ref: ArtifactRef | None = None
    error_code: str | None = None
    message: str = ""

    @property
    def success(self) -> bool:
        return self.ref is not None and self.error_code is None


class ArtifactStore:
    """Write streamed evidence under one runtime root with safe filenames."""

    def __init__(
        self,
        runtime_root: str | Path,
        *,
        max_bytes: int = DEFAULT_ARTIFACT_QUOTA_BYTES,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError("Artifact quota must be a non-negative integer.")
        self.runtime_root = Path(runtime_root)
        self.max_bytes = max_bytes

    def open_stream(
        self,
        *,
        kind: str,
        media_type: str,
        max_bytes: int | None = None,
    ) -> "ArtifactWriter":
        _validate_kind(kind)
        if not isinstance(media_type, str) or not media_type.strip():
            raise ArtifactStoreError(
                ArtifactErrorCode.INVALID,
                "Artifact media_type must be non-empty.",
            )
        quota = self.max_bytes if max_bytes is None else max_bytes
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
            raise ArtifactStoreError(
                ArtifactErrorCode.INVALID,
                "Artifact quota must be a non-negative integer.",
            )
        directory = self.runtime_root / ARTIFACTS_DIRECTORY
        try:
            directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".verification-", suffix=".tmp", dir=directory
            )
            handle = os.fdopen(descriptor, "wb")
        except (OSError, ValueError) as error:
            raise ArtifactStoreError(
                ArtifactErrorCode.OPEN_FAILED,
                "Unable to open an artifact spool.",
            ) from error
        return ArtifactWriter(
            store=self,
            kind=kind,
            media_type=media_type,
            quota=quota,
            temporary=Path(temporary),
            handle=handle,
        )

    def put_bytes(
        self,
        *,
        kind: str,
        data: bytes,
        media_type: str,
        redacted: bool = False,
        truncated: bool = False,
        run_id: str | None = None,
    ) -> ArtifactWriteResult:
        del run_id  # identity never contributes to an artifact filename
        try:
            writer = self.open_stream(kind=kind, media_type=media_type)
        except ArtifactStoreError as error:
            return ArtifactWriteResult(error_code=error.code.value, message=str(error))
        writer.write(data)
        return writer.finalize(redacted=redacted, truncated=truncated)


class ArtifactWriter:
    """Incremental writer used by pipe-drain threads."""

    def __init__(
        self,
        *,
        store: ArtifactStore,
        kind: str,
        media_type: str,
        quota: int,
        temporary: Path,
        handle: BinaryIO,
    ) -> None:
        self._store = store
        self._kind = kind
        self._media_type = media_type
        self._quota = quota
        self._temporary = temporary
        self._handle = handle
        self._digest = hashlib.sha256()
        self._size = 0
        self._error: tuple[ArtifactErrorCode, str] | None = None
        self._result: ArtifactWriteResult | None = None

    def write(self, data: bytes) -> None:
        if self._result is not None:
            return
        if not isinstance(data, (bytes, bytearray, memoryview)):
            self._set_error(ArtifactErrorCode.WRITE_FAILED, "Artifact data must be bytes.")
            return
        chunk = bytes(data)
        self._digest.update(chunk)
        self._size += len(chunk)
        if self._error is not None:
            return
        if self._size > self._quota:
            self._set_error(
                ArtifactErrorCode.QUOTA_EXCEEDED,
                "Verification artifact quota was exceeded.",
            )
            return
        try:
            self._handle.write(chunk)
        except (OSError, ValueError) as error:
            self._set_error(
                ArtifactErrorCode.WRITE_FAILED,
                f"Verification artifact write failed: {error}",
            )

    def finalize(self, *, redacted: bool, truncated: bool) -> ArtifactWriteResult:
        if self._result is not None:
            return self._result
        if self._error is not None:
            self._result = self._failure(*self._error)
            return self._result
        digest = self._digest.hexdigest()
        target = self._store.runtime_root / ARTIFACTS_DIRECTORY / f"{digest}.artifact"
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            os.replace(self._temporary, target)
        except (OSError, ValueError) as error:
            self._result = self._failure(
                ArtifactErrorCode.PUBLISH_FAILED,
                f"Verification artifact publish failed: {error}",
            )
            return self._result
        relative = target.relative_to(self._store.runtime_root).as_posix()
        self._result = ArtifactWriteResult(
            ref=ArtifactRef(
                kind=self._kind,
                path=relative,
                sha256=digest,
                size=self._size,
                media_type=self._media_type,
                redacted=redacted,
                truncated=truncated,
            )
        )
        return self._result

    def abort(self) -> ArtifactWriteResult:
        if self._result is None:
            self._result = self._failure(
                ArtifactErrorCode.CLEANUP_FAILED,
                "Verification artifact spool was aborted.",
            )
        return self._result

    def _set_error(self, code: ArtifactErrorCode, message: str) -> None:
        if self._error is None:
            self._error = (code, message)

    def _failure(self, code: ArtifactErrorCode, message: str) -> ArtifactWriteResult:
        try:
            try:
                self._handle.close()
            except (OSError, ValueError):
                pass
            self._temporary.unlink(missing_ok=True)
        except OSError as error:
            return ArtifactWriteResult(
                error_code=ArtifactErrorCode.CLEANUP_FAILED.value,
                message=f"{message} Temporary spool cleanup failed: {error}",
            )
        return ArtifactWriteResult(error_code=code.value, message=message)


def _validate_kind(kind: str) -> None:
    if not isinstance(kind, str) or not _KIND_PATTERN.fullmatch(kind):
        raise ArtifactStoreError(
            ArtifactErrorCode.INVALID,
            "Artifact kind must be a safe identifier.",
        )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ARTIFACTS_DIRECTORY",
    "DEFAULT_ARTIFACT_QUOTA_BYTES",
    "ArtifactErrorCode",
    "ArtifactRef",
    "ArtifactStore",
    "ArtifactStoreError",
    "ArtifactWriteResult",
    "ArtifactWriter",
]
