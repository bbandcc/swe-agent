"""Crash-resistant final writes for validated workspace transactions."""

import os
import tempfile
from pathlib import Path

from agent.editing.models import EditErrorCode, WorkspaceTransaction
from agent.editing.text import sha256

WriteFailure = tuple[EditErrorCode, str]


def replace_existing(
    target: Path,
    content: bytes,
    transaction: WorkspaceTransaction,
) -> WriteFailure | None:
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}."
        )
    except OSError as error:
        return (
            EditErrorCode.WRITE_FAILED,
            f"Could not prepare the edited file: {error}",
        )
    try:
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                current_hash = sha256(target.read_bytes())
            except OSError as error:
                return (
                    EditErrorCode.READ_FAILED,
                    f"Could not re-read the target before replacement: {error}",
                )
            if current_hash != transaction.base_hash:
                return (
                    EditErrorCode.HASH_MISMATCH,
                    "The file changed while the final write was being prepared.",
                )
            if transaction.original_mode is not None:
                os.chmod(temporary_name, transaction.original_mode)
            os.replace(temporary_name, target)
        except OSError as error:
            return (
                EditErrorCode.WRITE_FAILED,
                f"Could not replace the target file: {error}",
            )
    finally:
        _remove_temporary_file(temporary_name)
    return None


def create_new(target: Path, content: bytes) -> WriteFailure | None:
    created_directories = _missing_directories(target.parent)
    temporary_name: str | None = None
    try:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}."
            )
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
        except OSError as error:
            return (
                EditErrorCode.WRITE_FAILED,
                f"Could not prepare the new file: {error}",
            )
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            return (EditErrorCode.FILE_EXISTS, "The target file already exists.")
        except OSError as error:
            return (
                EditErrorCode.WRITE_FAILED,
                f"Could not create the new file: {error}",
            )
    finally:
        if temporary_name is not None:
            _remove_temporary_file(temporary_name)
        if not target.exists():
            _remove_empty_directories(created_directories)
    return None


def _missing_directories(parent: Path) -> list[Path]:
    missing: list[Path] = []
    current = parent
    while not current.exists():
        missing.append(current)
        next_parent = current.parent
        if next_parent == current:
            break
        current = next_parent
    return missing


def _remove_empty_directories(directories: list[Path]) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _remove_temporary_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        os.unlink(path)
    except OSError:
        pass
