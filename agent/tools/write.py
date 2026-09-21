"""Workspace structure and compatibility write tools."""

import os
from pathlib import Path

from langchain_core.tools import tool

from agent.editing import EditOperation, EditProposal, EditStatus, WorkspaceEditor
from agent.tools.results import (
    tool_access_denied,
    tool_error,
    tool_policy_denial,
    tool_rejection,
    tool_success,
)
from agent.workspace import (
    WorkspaceAccessPolicy,
    current_workspace_access_policy,
    default_workspace_resolver,
    has_single_regular_file_link,
)


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _safe_tree(
    directory: Path,
    display_root: str,
    *,
    workspace_root: Path,
    access_policy: WorkspaceAccessPolicy | None,
) -> str:
    lines = [display_root]
    for root, directories, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        directories[:] = sorted(
            name
            for name in directories
            if not _is_link_or_junction(root_path / name)
            and not _is_protected(
                root_path / name, workspace_root, access_policy
            )
        )
        files = sorted(
            name
            for name in files
            if not _is_link_or_junction(root_path / name)
            and has_single_regular_file_link(root_path / name)
            and not _is_protected(
                root_path / name, workspace_root, access_policy
            )
        )
        relative = root_path.relative_to(directory)
        depth = len(relative.parts)
        if relative.parts:
            lines.append(f"{'  ' * depth}{relative.name}/")
        lines.extend(f"{'  ' * (depth + 1)}{name}" for name in files)
    return "\n".join(lines)


def _is_protected(
    path: Path,
    workspace_root: Path,
    access_policy: WorkspaceAccessPolicy | None,
) -> bool:
    if access_policy is None:
        return False
    try:
        relative = path.relative_to(workspace_root).as_posix()
    except ValueError:
        return True
    return access_policy.is_protected(relative)


@tool(parse_docstring=True)
def create_file(path: str, content: str) -> dict[str, object]:
    """Create a UTF-8 text file through the validated workspace editor.

    Args:
        path: Workspace-relative destination path.
        content: Complete UTF-8 text content.
    """
    resolver = default_workspace_resolver()
    root = resolver.resolve_directory(".")
    if not root.ok or root.path is None:
        return tool_rejection(root)
    result = WorkspaceEditor(
        root.path,
        access_policy=current_workspace_access_policy(),
    ).apply(
        EditProposal(
            task_id="tool.create_file",
            path=path,
            operation=EditOperation.CREATE,
            new_text=content,
        )
    )
    if result.status is not EditStatus.APPLIED:
        return tool_error(
            path,
            result.error_code.value if result.error_code else result.status.value,
            result.message,
        )
    return tool_success(path, result.diff)


@tool(parse_docstring=True)
def write_to_file(path: str, content: str) -> dict[str, object]:
    """Reject unsafe whole-file overwrite requests.

    Args:
        path: Workspace file that would be overwritten.
        content: Proposed complete replacement content.
    """
    return tool_error(
        path,
        "unsupported_operation",
        "Whole-file overwrite is disabled; use a validated SEARCH/REPLACE proposal.",
    )


@tool(parse_docstring=True)
def get_files_structure(directory: str = ".") -> dict[str, object]:
    """Return a workspace-bound file tree without following links.

    Args:
        directory: Workspace directory, relative or absolute within the workspace.
    """
    resolver = default_workspace_resolver()
    early_denial = tool_policy_denial(
        resolver, directory, current_workspace_access_policy()
    )
    if early_denial is not None:
        return early_denial
    resolution = resolver.resolve_directory(directory)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    access_policy = current_workspace_access_policy()
    relative = resolution.relative_path or directory
    if access_policy is not None:
        decision = access_policy.check_read(relative)
        if not decision.allowed:
            return tool_access_denied(
                decision.error_code.value,
                decision.message,
            )
    workspace_root = resolver.resolve_directory(".")
    if not workspace_root.ok or workspace_root.path is None:
        return tool_rejection(workspace_root)
    content = _safe_tree(
        resolution.path,
        resolution.relative_path or ".",
        workspace_root=workspace_root.path,
        access_policy=access_policy,
    )
    return tool_success(resolution.relative_path or ".", content)


write_tools = [create_file, write_to_file, get_files_structure]
write_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in write_tools}
