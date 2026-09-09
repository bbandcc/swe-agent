import difflib
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from agent.editing.models import (
    EditErrorCode,
    EditOperation,
    EditProposal,
    EditResult,
    EditStatus,
    WorkspaceSnapshot,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _line_ending(text: str) -> str | None:
    without_crlf = text.replace("\r\n", "")
    has_crlf = "\r\n" in text
    has_lf = "\n" in without_crlf
    has_cr = "\r" in without_crlf
    if sum((has_crlf, has_lf, has_cr)) > 1:
        return None
    if has_crlf:
        return "\r\n"
    if has_cr:
        return "\r"
    return "\n"


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


class WorkspaceEditor:
    """Validate and apply a single deterministic workspace edit."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve(strict=True)

    def snapshot(self, path: str) -> WorkspaceSnapshot:
        """Read one workspace file using the same path boundary as edits."""
        target = self._resolve_target(path)
        if target is None:
            return WorkspaceSnapshot(
                path=path,
                exists=False,
                error_code=EditErrorCode.PATH_INVALID,
                message="The path must stay inside the configured workspace.",
            )
        if not target.exists():
            return WorkspaceSnapshot(path=path, exists=False)
        if not target.is_file():
            return WorkspaceSnapshot(
                path=path,
                exists=False,
                error_code=EditErrorCode.READ_FAILED,
                message="The workspace path is not a regular file.",
            )

        try:
            content_bytes = target.read_bytes()
        except OSError as error:
            return WorkspaceSnapshot(
                path=path,
                exists=True,
                error_code=EditErrorCode.READ_FAILED,
                message=f"Could not read the target file: {error}",
            )
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return WorkspaceSnapshot(
                path=path,
                exists=True,
                error_code=EditErrorCode.ENCODING_ERROR,
                message="Only UTF-8 text files can be read.",
            )
        return WorkspaceSnapshot(
            path=path,
            exists=True,
            content=content,
            content_hash=_sha256(content_bytes),
        )

    def apply(self, proposal: EditProposal) -> EditResult:
        target = self._resolve_target(proposal.path)
        if target is None:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.PATH_INVALID,
                message="The path must stay inside the configured workspace.",
            )
        if proposal.operation is EditOperation.CREATE:
            return self._create(target, proposal)

        if not target.is_file():
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.FILE_NOT_FOUND,
                message="The target file does not exist.",
            )

        try:
            original_bytes = target.read_bytes()
            original_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError as error:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.WRITE_FAILED,
                message=f"Could not read the target file: {error}",
            )
        before_hash = _sha256(original_bytes)
        if before_hash != proposal.base_hash:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.HASH_MISMATCH,
                message="The file changed after the edit proposal was created.",
                before_hash=before_hash,
            )

        try:
            original = original_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.ENCODING_ERROR,
                message="Only UTF-8 text files can be edited.",
                before_hash=before_hash,
            )
        if not proposal.old_text:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.EMPTY_OLD_TEXT,
                message="old_text must not be empty.",
                before_hash=before_hash,
            )

        line_ending = _line_ending(original)
        if line_ending is None:
            searchable = original
            old_text = proposal.old_text
            new_text = proposal.new_text
        else:
            searchable = _normalize_line_endings(original)
            old_text = _normalize_line_endings(proposal.old_text)
            new_text = _normalize_line_endings(proposal.new_text)

        occurrences = searchable.count(old_text)
        if occurrences == 0:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.MATCH_NOT_FOUND,
                message="old_text was not found in the current file.",
                before_hash=before_hash,
            )
        if occurrences > 1:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.MATCH_AMBIGUOUS,
                message=f"old_text appears {occurrences} times; include more surrounding context.",
                before_hash=before_hash,
            )

        updated = searchable.replace(old_text, new_text, 1)
        if line_ending not in (None, "\n"):
            updated = updated.replace("\n", line_ending)
        updated_bytes = updated.encode("utf-8")
        if updated_bytes == original_bytes:
            return EditResult(
                status=EditStatus.NOOP,
                path=proposal.path,
                message="The proposal does not change the file.",
                before_hash=before_hash,
                after_hash=before_hash,
            )

        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=proposal.path,
                tofile=proposal.path,
            )
        )

        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}."
            )
        except OSError as error:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.WRITE_FAILED,
                message=f"Could not prepare the edited file: {error}",
                before_hash=before_hash,
            )

        try:
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(updated_bytes)
                    temporary.flush()
                    os.fsync(temporary.fileno())

                current_hash = _sha256(target.read_bytes())
                if current_hash != before_hash:
                    return EditResult(
                        status=EditStatus.REJECTED,
                        path=proposal.path,
                        error_code=EditErrorCode.HASH_MISMATCH,
                        message="The file changed while the edit was being prepared.",
                        before_hash=current_hash,
                    )

                os.chmod(temporary_name, original_mode)
                os.replace(temporary_name, target)
            except OSError as error:
                return EditResult(
                    status=EditStatus.REJECTED,
                    path=proposal.path,
                    error_code=EditErrorCode.WRITE_FAILED,
                    message=f"Could not replace the target file: {error}",
                    before_hash=before_hash,
                )
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

        return EditResult(
            status=EditStatus.APPLIED,
            path=proposal.path,
            before_hash=before_hash,
            after_hash=_sha256(updated_bytes),
            diff=diff,
        )

    def _create(self, target: Path, proposal: EditProposal) -> EditResult:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = proposal.new_text.encode("utf-8")
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}."
            )
        except UnicodeEncodeError:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.ENCODING_ERROR,
                message="New file content must be valid UTF-8 text.",
            )
        except OSError as error:
            return EditResult(
                status=EditStatus.REJECTED,
                path=proposal.path,
                error_code=EditErrorCode.WRITE_FAILED,
                message=f"Could not prepare the new file: {error}",
            )

        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.link(temporary_name, target)
            except FileExistsError:
                return EditResult(
                    status=EditStatus.REJECTED,
                    path=proposal.path,
                    error_code=EditErrorCode.FILE_EXISTS,
                    message="The target file already exists.",
                )
            except OSError as error:
                return EditResult(
                    status=EditStatus.REJECTED,
                    path=proposal.path,
                    error_code=EditErrorCode.WRITE_FAILED,
                    message=f"Could not create the new file: {error}",
                )
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

        diff = "".join(
            difflib.unified_diff(
                [],
                proposal.new_text.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=proposal.path,
            )
        )
        return EditResult(
            status=EditStatus.APPLIED,
            path=proposal.path,
            after_hash=_sha256(content),
            diff=diff,
        )

    def _resolve_target(self, relative_path: str) -> Path | None:
        requested = Path(relative_path)
        if not relative_path or requested.is_absolute() or ".." in requested.parts:
            return None

        target = self._root.joinpath(requested)
        try:
            resolved = target.resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_relative_to(self._root):
            return None

        current = self._root
        for part in requested.parts:
            current = current / part
            if current.is_symlink() or (
                hasattr(current, "is_junction") and current.is_junction()
            ):
                return None
        return resolved
