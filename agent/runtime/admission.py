"""Cooperative OS-process admission for one durable workspace."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from agent.runtime.identity import RunIdentity
from agent.workspace import (
    WorkspaceRootError,
    canonical_path_key,
    canonicalize_root_path,
)


class AdmissionStatus(str, Enum):
    ACQUIRED = "acquired"
    BUSY = "busy"


class AdmissionErrorCode(str, Enum):
    BUSY = "busy"
    INVALID = "admission_invalid"
    UNSUPPORTED = "admission_unsupported"
    IO_ERROR = "admission_io_error"


class AdmissionLockError(RuntimeError):
    """Structured failure while establishing or using an admission lock."""

    def __init__(self, code: AdmissionErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AdmissionKey:
    """Stable, secret-free identity for one workspace admission attempt.

    ``stable_key`` includes the thread identity for diagnostics and event
    correlation.  ``workspace_key`` intentionally omits it because the OS
    lock serializes every thread that targets the same canonical workspace.
    """

    workspace_key: str
    stable_key: str
    thread_id: str

    @classmethod
    def from_identity(cls, identity: RunIdentity) -> "AdmissionKey":
        if not isinstance(identity, RunIdentity):
            raise ValueError("identity must be RunIdentity.")
        workspace = identity.workspace
        workspace_material = {
            "canonical_root": canonical_path_key(workspace.canonical_root),
            "root_digest": workspace.root_digest,
        }
        workspace_key = _digest(workspace_material)
        stable_key = _digest(
            {
                **workspace_material,
                "thread_id": identity.thread_id,
            }
        )
        return cls(
            workspace_key=workspace_key,
            stable_key=stable_key,
            thread_id=identity.thread_id,
        )


class WorkspaceAdmissionLock:
    """A non-blocking OS lock scoped to one canonical workspace.

    The lock file is deliberately retained after release.  Its byte-range
    lock is owned by the process and is released by the operating system when
    that process exits, so a stale file cannot permanently block a later run.
    """

    LOCK_DIRECTORY = "locks"

    def __init__(self, runtime_root: str | Path, identity: RunIdentity) -> None:
        if not isinstance(identity, RunIdentity):
            raise ValueError("identity must be RunIdentity.")
        try:
            self.runtime_root = canonicalize_root_path(
                runtime_root,
                must_exist=False,
            )
        except WorkspaceRootError as error:
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The admission runtime root is invalid.",
            ) from error
        self.key = AdmissionKey.from_identity(identity)
        self.lock_directory = self.runtime_root / self.LOCK_DIRECTORY
        self.lock_path = self.lock_directory / f"{self.key.workspace_key}.lock"
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> AdmissionStatus:
        """Try once without waiting; return BUSY for another live holder."""
        if self._handle is not None:
            return AdmissionStatus.ACQUIRED
        self._prepare_directory()
        handle: BinaryIO | None = None
        try:
            handle = self.lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            self._try_os_lock(handle)
        except _AdmissionBusy:
            _close_quietly(handle)
            return AdmissionStatus.BUSY
        except AdmissionLockError:
            _close_quietly(handle)
            raise
        except OSError as error:
            _close_quietly(handle)
            if _is_busy_error(error):
                return AdmissionStatus.BUSY
            raise AdmissionLockError(
                AdmissionErrorCode.IO_ERROR,
                "Unable to acquire the workspace admission lock.",
            ) from error
        except BaseException:
            # An unexpected platform error must propagate, but never leave a
            # partially acquired descriptor behind in the current process.
            _close_quietly(handle)
            raise
        self._handle = handle
        return AdmissionStatus.ACQUIRED

    def release(self) -> None:
        """Release the OS lock and close the descriptor; safe to repeat."""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            self._unlock_os(handle)
        except (AdmissionLockError, OSError):
            # Closing the descriptor is itself an OS-level release.  Keep
            # cleanup best-effort so an application exception is not masked.
            pass
        finally:
            _close_quietly(handle)

    def __enter__(self) -> "WorkspaceAdmissionLock":
        status = self.acquire()
        if status is AdmissionStatus.BUSY:
            raise AdmissionLockError(
                AdmissionErrorCode.BUSY,
                "The workspace is already admitted by another process.",
            )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.release()
        return False

    def _prepare_directory(self) -> None:
        if _is_link_or_junction(self.runtime_root):
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The admission runtime root must not be a symlink or junction.",
            )
        if _is_link_or_junction(self.lock_directory):
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The admission lock directory must not be a symlink or junction.",
            )
        try:
            self.lock_directory.mkdir(parents=True, exist_ok=True)
            if _is_link_or_junction(self.lock_directory):
                raise AdmissionLockError(
                    AdmissionErrorCode.INVALID,
                    "The admission lock directory must not be a symlink or junction.",
                )
            root = self.runtime_root.resolve(strict=False)
            directory = self.lock_directory.resolve(strict=False)
            directory.relative_to(root)
        except AdmissionLockError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The admission lock path could not be resolved safely.",
            ) from error

    def _try_os_lock(self, handle: BinaryIO) -> None:
        if os.name == "nt":
            try:
                import msvcrt
            except ImportError as error:
                raise AdmissionLockError(
                    AdmissionErrorCode.UNSUPPORTED,
                    "Windows admission locking is unavailable.",
                ) from error
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if _is_busy_error(error):
                    raise _AdmissionBusy from error
                raise
            return
        try:
            import fcntl
        except ImportError as error:
            raise AdmissionLockError(
                AdmissionErrorCode.UNSUPPORTED,
                "This platform has no supported admission lock.",
            ) from error
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if _is_busy_error(error):
                raise _AdmissionBusy from error
            raise

    def _unlock_os(self, handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _AdmissionBusy(Exception):
    pass


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_busy_error(error: OSError) -> bool:
    return isinstance(error, BlockingIOError) or error.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
    }


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )
    except OSError:
        return True


def _close_quietly(handle: BinaryIO | None) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except (OSError, ValueError):
        pass


__all__ = [
    "AdmissionErrorCode",
    "AdmissionKey",
    "AdmissionLockError",
    "AdmissionStatus",
    "WorkspaceAdmissionLock",
]
