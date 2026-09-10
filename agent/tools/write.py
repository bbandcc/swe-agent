"""Workspace structure and compatibility write tools."""

import os
from pathlib import Path

from langchain_core.tools import tool

from agent.editing import EditOperation, EditProposal, EditStatus, WorkspaceEditor
from agent.tools.results import tool_error, tool_rejection, tool_success
from agent.workspace import default_workspace_resolver


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _safe_tree(directory: Path, display_root: str) -> str:
    lines = [display_root]
    for root, directories, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        directories[:] = sorted(
            name
            for name in directories
            if not _is_link_or_junction(root_path / name)
        )
        files = sorted(
            name
            for name in files
            if not _is_link_or_junction(root_path / name)
        )
        relative = root_path.relative_to(directory)
        depth = len(relative.parts)
        if relative.parts:
            lines.append(f"{'  ' * depth}{relative.name}/")
        lines.extend(f"{'  ' * (depth + 1)}{name}" for name in files)
    return "\n".join(lines)


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
    result = WorkspaceEditor(root.path).apply(
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
    resolution = default_workspace_resolver().resolve_directory(directory)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    content = _safe_tree(resolution.path, resolution.relative_path or ".")
    return tool_success(resolution.relative_path or ".", content)


write_tools = [create_file, write_to_file, get_files_structure]
write_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in write_tools}
