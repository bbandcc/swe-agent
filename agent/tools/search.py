"""Workspace-bound literal text search."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool

from agent.tools.results import (
    tool_access_denied,
    tool_error,
    tool_policy_denial,
    tool_rejection,
    tool_success,
)
from agent.workspace import (
    WorkspaceAccessPolicy,
    WorkspacePathResolver,
    current_workspace_access_policy,
    default_workspace_resolver,
    has_single_regular_file_link,
)


@dataclass(frozen=True, slots=True)
class _DirectorySearchResult:
    content: str
    warnings: list[str]
    skipped_files: list[str]


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _search_in_file(
    file_path: Path, search_term: str, context: int
) -> list[tuple[int, list[tuple[int, str]]]]:
    with file_path.open("r", encoding="utf-8") as source:
        lines = source.readlines()
    pattern = re.compile(re.escape(search_term), re.IGNORECASE)
    results = []
    for index, line in enumerate(lines):
        if pattern.search(line):
            start = max(index - context, 0)
            end = min(index + context + 1, len(lines))
            snippet = [
                (line_index + 1, lines[line_index].rstrip("\r\n"))
                for line_index in range(start, end)
            ]
            results.append((index + 1, snippet))
    return results


def _search_directory(
    resolver: WorkspacePathResolver,
    directory: Path,
    search_term: str,
    context: int,
    *,
    workspace_root: Path,
    access_policy: WorkspaceAccessPolicy | None,
) -> _DirectorySearchResult:
    output: list[str] = []
    warnings: list[str] = []
    skipped_files: list[str] = []
    for root, directories, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name
            for name in directories
            if not _is_link_or_junction(root_path / name)
            and not _is_protected(
                root_path / name, workspace_root, access_policy
            )
        ]
        for name in files:
            if not name.endswith(".py"):
                continue
            file_path = root_path / name
            if _is_link_or_junction(file_path):
                continue
            if not has_single_regular_file_link(file_path):
                continue
            if _is_protected(file_path, workspace_root, access_policy):
                continue
            resolved_file = resolver.resolve_file(str(file_path))
            if not resolved_file.ok or resolved_file.path is None:
                skipped = resolved_file.relative_path or name
                skipped_files.append(skipped)
                warnings.append(
                    f"Skipped {skipped}: {resolved_file.message or 'unsafe path.'}"
                )
                continue
            try:
                matches = _search_in_file(
                    resolved_file.path, search_term, context
                )
            except (OSError, UnicodeError) as error:
                skipped = resolved_file.relative_path or name
                skipped_files.append(skipped)
                warnings.append(
                    f"Skipped {skipped}: {type(error).__name__}: {error}"
                )
                continue
            for match_line, snippet in matches:
                output.append(f"\nFile: {resolved_file.relative_path}")
                output.append(f"Match found at line: {match_line}")
                output.extend(
                    f"{line_number:4} | {code}"
                    for line_number, code in snippet
                )
                output.append("-" * 50)
    return _DirectorySearchResult(
        content="\n".join(output) if output else "No matches found.",
        warnings=warnings,
        skipped_files=skipped_files,
    )


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
def search_keyword_in_directory(
    directory: str, search_term: str, context: int = 2
) -> dict[str, object]:
    """Search Python files for a case-insensitive literal keyword.

    Args:
        directory: Workspace directory, relative or absolute within the workspace.
        search_term: Literal term to find; it must contain at least 3 characters.
        context: Number of surrounding lines, between 0 and 20.
    """
    if len(search_term) < 3:
        return tool_error(
            directory, "invalid_request", "search_term must be at least 3 characters."
        )
    if not 0 <= context <= 20:
        return tool_error(
            directory, "invalid_request", "context must be between 0 and 20."
        )
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
    result = _search_directory(
        resolver,
        resolution.path,
        search_term,
        context,
        workspace_root=workspace_root.path,
        access_policy=access_policy,
    )
    return tool_success(
        resolution.relative_path or ".",
        result.content,
        warnings=result.warnings,
        skipped_files=result.skipped_files,
    )


search_tools = [search_keyword_in_directory]
search_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in search_tools}
