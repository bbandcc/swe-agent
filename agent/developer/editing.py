"""Translate untrusted Developer model output into deterministic workspace edits."""

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from agent.editing import (
    EditErrorCode,
    EditOperation,
    EditProposal,
    EditResult,
    EditStatus,
    WorkspaceEditor,
    WorkspaceSnapshot,
)

_SEARCH = "<<<<<<< SEARCH"
_SEPARATOR = "======="
_REPLACE = ">>>>>>> REPLACE"


@dataclass(frozen=True, slots=True)
class _SearchReplaceBlock:
    old_text: str
    new_text: str


class DeveloperEditExecutor:
    """Prepare and apply one model-proposed change within a workspace."""

    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    def prepare(self, plan_path: str) -> WorkspaceSnapshot:
        relative_path = _workspace_relative_path(plan_path)
        if relative_path is None:
            return WorkspaceSnapshot(
                path=plan_path,
                exists=False,
                error_code=EditErrorCode.PATH_INVALID,
                message="The implementation plan path must stay inside workspace_repo.",
            )
        return self._editor.snapshot(relative_path)

    def apply(self, snapshot: WorkspaceSnapshot, model_output: str) -> EditResult:
        if snapshot.error_code is not None:
            return EditResult(
                status=EditStatus.REJECTED,
                path=snapshot.path,
                error_code=snapshot.error_code,
                message=snapshot.message,
                before_hash=snapshot.content_hash,
            )
        if not snapshot.exists:
            return self._editor.apply(
                EditProposal(
                    path=snapshot.path,
                    operation=EditOperation.CREATE,
                    new_text=model_output,
                )
            )

        block = _parse_search_replace_block(model_output)
        if block is None:
            return EditResult(
                status=EditStatus.REJECTED,
                path=snapshot.path,
                error_code=EditErrorCode.INVALID_MODEL_RESPONSE,
                message="Expected exactly one complete SEARCH/REPLACE block and no prose.",
                before_hash=snapshot.content_hash,
            )
        return self._editor.apply(
            EditProposal(
                path=snapshot.path,
                operation=EditOperation.EDIT,
                base_hash=snapshot.content_hash,
                old_text=block.old_text,
                new_text=block.new_text,
            )
        )


def _workspace_relative_path(plan_path: str) -> str | None:
    normalized = plan_path.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if (
        not normalized
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
    ):
        return None

    parts = list(posix_path.parts)
    if parts and parts[0] == "workspace_repo":
        parts.pop(0)
    if not parts:
        return None
    return "/".join(parts)


def _parse_search_replace_block(model_output: str) -> _SearchReplaceBlock | None:
    normalized = model_output.replace("\r\n", "\n").replace("\r", "\n").strip()
    prefix = f"{_SEARCH}\n"
    separator = f"\n{_SEPARATOR}\n"
    suffix = f"\n{_REPLACE}"
    if not normalized.startswith(prefix) or not normalized.endswith(suffix):
        return None

    body = normalized[len(prefix) : -len(suffix)]
    if body.count(separator) != 1:
        return None
    old_text, new_text = body.split(separator, 1)
    if not old_text:
        return None
    return _SearchReplaceBlock(old_text=old_text, new_text=new_text)
