"""Cooperative OS-process admission for one durable workspace."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
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
    runtime_thread_key: str = ""

    @classmethod
    def from_identity(
        cls,
        identity: RunIdentity,
        runtime_root: str | Path | None = None,
    ) -> "AdmissionKey":
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
        runtime_material = {
            "canonical_runtime_root": (
                canonical_path_key(runtime_root) if runtime_root is not None else ""
            ),
            "thread_id": identity.thread_id,
        }
        return cls(
            workspace_key=workspace_key,
            stable_key=stable_key,
            thread_id=identity.thread_id,
            runtime_thread_key=_digest(runtime_material),
        )


class WorkspaceAdmissionLock:
    """Two non-blocking OS locks for one durable admission.

    The workspace lock is process-shared and independent of ``runtime_root``;
    the runtime/thread lock is scoped to the configured runtime root.  Both
    lock files are deliberately retained after release.  Their byte-range
    locks are owned by the process and released by the operating system when
    that process exits, so stale files cannot permanently block later runs.
    """

    LOCK_DIRECTORY = "locks"
    WORKSPACE_LOCK_DIRECTORY = "workspaces"
    SHARED_LOCK_ROOT = "swe-agent-admission"

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
        self.key = AdmissionKey.from_identity(identity, self.runtime_root)
        try:
            shared_root = canonicalize_root_path(
                Path(tempfile.gettempdir()) / self.SHARED_LOCK_ROOT,
                must_exist=False,
            )
        except WorkspaceRootError as error:
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The shared admission lock root is invalid.",
            ) from error
        self.shared_lock_root = shared_root
        self.workspace_lock_directory = (
            shared_root / self.WORKSPACE_LOCK_DIRECTORY
        )
        self.workspace_lock_path = (
            self.workspace_lock_directory / f"{self.key.workspace_key}.lock"
        )
        self.runtime_lock_directory = self.runtime_root / self.LOCK_DIRECTORY
        self.runtime_lock_path = (
            self.runtime_lock_directory
            / f"{self.key.runtime_thread_key}.lock"
        )
        # Keep the original names as aliases for callers that only observed
        # the first runtime-root lock seam.
        self.lock_directory = self.runtime_lock_directory
        self.lock_path = self.runtime_lock_path
        self._workspace_handle: BinaryIO | None = None
        self._runtime_handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._workspace_handle is not None and self._runtime_handle is not None

    def acquire(self) -> AdmissionStatus:
        """Try both locks once without waiting; return BUSY on contention."""
        if self.acquired:
            return AdmissionStatus.ACQUIRED
        if self._workspace_handle is not None or self._runtime_handle is not None:
            self.release()
        self._prepare_paths()
        workspace_handle = self._acquire_file(self.workspace_lock_path)
        if workspace_handle is None:
            return AdmissionStatus.BUSY
        self._workspace_handle = workspace_handle
        try:
            runtime_handle = self._acquire_file(self.runtime_lock_path)
        except BaseException:
            self.release()
            raise
        if runtime_handle is None:
            self.release()
            return AdmissionStatus.BUSY
        self._runtime_handle = runtime_handle
        return AdmissionStatus.ACQUIRED

    def release(self) -> None:
        """Release both OS locks in reverse acquisition order; safe to repeat."""
        runtime_handle = self._runtime_handle
        workspace_handle = self._workspace_handle
        self._runtime_handle = None
        self._workspace_handle = None
        first_unexpected: BaseException | None = None
        for handle in (runtime_handle, workspace_handle):
            if handle is None:
                continue
            try:
                self._unlock_os(handle)
            except (AdmissionLockError, OSError):
                # Closing the descriptor is itself an OS-level release.  Keep
                # expected cleanup failures from masking the run result.
                pass
            except BaseException as error:
                if first_unexpected is None:
                    first_unexpected = error
            finally:
                _close_quietly(handle)
        if first_unexpected is not None:
            raise first_unexpected

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

    def _prepare_paths(self) -> None:
        self._prepare_lock_path(
            self.shared_lock_root,
            self.workspace_lock_directory,
            self.workspace_lock_path,
        )
        self._prepare_lock_path(
            self.runtime_root,
            self.runtime_lock_directory,
            self.runtime_lock_path,
        )

    def _prepare_lock_path(
        self,
        root: Path,
        directory: Path,
        lock_path: Path,
    ) -> None:
        if _is_link_or_junction(root):
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The admission lock root must not be a symlink or junction.",
            )
        if _is_link_or_junction(directory):
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The admission lock directory must not be a symlink or junction.",
            )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if _is_link_or_junction(directory):
                raise AdmissionLockError(
                    AdmissionErrorCode.INVALID,
                    "The admission lock directory must not be a symlink or junction.",
                )
            canonical_root = root.resolve(strict=False)
            canonical_directory = directory.resolve(strict=False)
            canonical_directory.relative_to(canonical_root)
            if _is_link_or_junction(lock_path):
                raise AdmissionLockError(
                    AdmissionErrorCode.INVALID,
                    "The final admission lock file must not be a symlink or junction.",
                )
            canonical_lock = lock_path.resolve(strict=False)
            canonical_lock.relative_to(canonical_directory)
        except AdmissionLockError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise AdmissionLockError(
                AdmissionErrorCode.INVALID,
                "The admission lock path could not be resolved safely.",
            ) from error

    def _acquire_file(self, lock_path: Path) -> BinaryIO | None:
        handle: BinaryIO | None = None
        try:
            if _is_link_or_junction(lock_path):
                raise AdmissionLockError(
                    AdmissionErrorCode.INVALID,
                    "The final admission lock file must not be a symlink or junction.",
                )
            flags = os.O_RDWR | os.O_CREAT
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                flags |= nofollow
            descriptor = os.open(lock_path, flags, 0o600)
            handle = os.fdopen(descriptor, "r+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            self._try_os_lock(handle)
        except _AdmissionBusy:
            _close_quietly(handle)
            return None
        except AdmissionLockError:
            _close_quietly(handle)
            raise
        except OSError as error:
            _close_quietly(handle)
            if _is_link_error(error):
                raise AdmissionLockError(
                    AdmissionErrorCode.INVALID,
                    "The final admission lock file must not be a symlink or junction.",
                ) from error
            if _is_busy_error(error):
                return None
            raise AdmissionLockError(
                AdmissionErrorCode.IO_ERROR,
                "Unable to acquire the admission lock.",
            ) from error
        except BaseException:
            _close_quietly(handle)
            raise
        return handle

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


def _is_link_error(error: OSError) -> bool:
    return error.errno == getattr(errno, "ELOOP", object())


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
