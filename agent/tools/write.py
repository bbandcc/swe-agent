"""Workspace structure and compatibility write tools."""

import os
from pathlib import Path

from agent.tools.read_contract import (
    IGNORED_DIRECTORY_NAMES,
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    MAX_TREE_SCAN_ENTRIES,
    decode_cursor,
    encode_cursor,
    stable_digest,
    tool_failure,
)
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


def _bounded_tree_entries(
    resolver,
    directory: Path,
    *,
    workspace_root: Path,
    access_policy: WorkspaceAccessPolicy | None,
    max_depth: int,
) -> tuple[list[dict[str, object]], list[str], bool]:
    entries: list[dict[str, object]] = []
    warnings: list[str] = []
    scan_truncated = False
    scanned_entries = 0
    scan_errors: list[OSError] = []
    pending_directories: list[tuple[Path, int]] = [(directory, 0)]

    while pending_directories:
        root_path, depth = pending_directories.pop()
        if depth >= max_depth:
            continue

        # Retain only names/types from entries already charged to the global
        # budget.  A single extra DirEntry may be consumed to prove truncation.
        candidates: list[tuple[str, str | None]] = []
        try:
            with os.scandir(root_path) as directory_entries:
                while scanned_entries < MAX_TREE_SCAN_ENTRIES:
                    try:
                        item = next(directory_entries)
                    except StopIteration:
                        break
                    scanned_entries += 1
                    name = item.name
                    path = root_path / name
                    if _is_protected(path, workspace_root, access_policy):
                        candidates.append((name, None))
                        continue
                    try:
                        kind = (
                            "directory"
                            if item.is_dir(follow_symlinks=False)
                            else "file"
                        )
                    except OSError as error:
                        scan_errors.append(error)
                        candidates.append((name, None))
                        continue
                    candidates.append((name, kind))

                if scanned_entries >= MAX_TREE_SCAN_ENTRIES:
                    try:
                        next(directory_entries)
                    except StopIteration:
                        pass
                    else:
                        scan_truncated = True
        except OSError as error:
            scan_errors.append(error)

        safe_directories: list[tuple[Path, int]] = []
        # Reverse-pop keeps only the bounded candidate list live while entries
        # are rendered; the final result is sorted independently below.
        candidates.sort(reverse=True)
        while candidates:
            name, kind = candidates.pop()
            if kind is None:
                continue
            path = root_path / name
            if kind == "directory":
                if (
                    name.casefold() in IGNORED_DIRECTORY_NAMES
                    or _is_link_or_junction(path)
                ):
                    continue
                resolution = resolver.resolve_directory(str(path))
            else:
                if (
                    _is_link_or_junction(path)
                    or not has_single_regular_file_link(path)
                ):
                    continue
                resolution = resolver.resolve_file(str(path))
            if not resolution.ok or resolution.path is None:
                warnings.append("tree_entry_skipped_unsafe_path")
                continue
            try:
                metadata = resolution.path.stat(follow_symlinks=False)
            except OSError:
                warnings.append("tree_entry_metadata_unavailable")
                continue
            relative = resolution.relative_path or name
            entries.append(
                {
                    "path": relative,
                    "kind": kind,
                    "depth": depth + 1,
                    "size": metadata.st_size if kind == "file" else None,
                    "mtime_ns": getattr(metadata, "st_mtime_ns", None),
                    "ctime_ns": getattr(metadata, "st_ctime_ns", None),
                    "device": getattr(metadata, "st_dev", None),
                    "inode": getattr(metadata, "st_ino", None),
                }
            )
            if kind == "directory" and depth + 1 < max_depth:
                safe_directories.append((path, depth + 1))

        # candidates were consumed in ascending name order, so reverse insertion
        # makes the next depth-first directory the lexicographically first one.
        pending_directories.extend(reversed(safe_directories))
        if scan_truncated:
            break
        if scanned_entries >= MAX_TREE_SCAN_ENTRIES:
            if pending_directories:
                next_directory, _next_depth = pending_directories.pop()
                try:
                    with os.scandir(next_directory) as remaining_entries:
                        try:
                            next(remaining_entries)
                        except StopIteration:
                            if pending_directories:
                                scan_truncated = True
                        else:
                            scan_truncated = True
                except OSError as error:
                    scan_errors.append(error)
            break

    if scan_errors:
        warnings.extend(
            f"tree_scan_error:{type(error).__name__}" for error in scan_errors
        )
    entries.sort(key=lambda item: str(item["path"]))
    return entries, warnings, scan_truncated


def _render_tree_page(
    display_root: str,
    entries: list[dict[str, object]],
    start: int,
    end: int,
) -> str:
    lines = [display_root]
    for entry in entries[start:end]:
        relative = Path(str(entry["path"]))
        depth = int(entry["depth"])
        indent = "  " * max(0, depth - 1)
        name = relative.name + ("/" if entry["kind"] == "directory" else "")
        lines.append(f"{indent}{name}")
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
def get_files_structure(
    directory: str = ".",
    max_depth: int = 4,
    max_entries: int = 100,
    cursor: str | None = None,
) -> dict[str, object]:
    """Return a bounded, paged workspace tree without following links.

    Args:
        directory: Workspace directory, relative or absolute within the workspace.
        max_depth: Requested tree depth, between 0 and the fixed hard limit.
        max_entries: Entries in this page, between 1 and the fixed hard limit.
        cursor: Continuation from the prior page for the same directory/options.
    """
    if (
        isinstance(max_depth, bool)
        or not isinstance(max_depth, int)
        or not 0 <= max_depth <= MAX_TREE_DEPTH
    ):
        return tool_failure(
            directory,
            "invalid_request",
            f"max_depth must be between 0 and {MAX_TREE_DEPTH}.",
        )
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or not 1 <= max_entries <= MAX_TREE_ENTRIES
    ):
        return tool_failure(
            directory,
            "invalid_request",
            f"max_entries must be between 1 and {MAX_TREE_ENTRIES}.",
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
    relative = resolution.relative_path or "."
    try:
        entries, warnings, scan_truncated = _bounded_tree_entries(
            resolver,
            resolution.path,
            workspace_root=workspace_root.path,
            access_policy=access_policy,
            max_depth=max_depth,
        )
    except OSError:
        return tool_failure(
            relative, "scan_failed", "Workspace tree scan failed."
        )
    if any(warning.startswith("tree_scan_error") for warning in warnings):
        return tool_failure(
            relative,
            "scan_failed",
            "Workspace tree scan was incomplete.",
            warnings=warnings,
        )
    inventory_hash = stable_digest(entries)
    offset = 0
    if cursor is not None:
        token = decode_cursor(cursor, "workspace_tree")
        if token is None:
            return tool_failure(
                relative, "invalid_cursor", "Continuation cursor is invalid."
            )
        query = {
            "path": relative,
            "max_depth": max_depth,
            "max_entries": max_entries,
        }
        if any(token.get(key) != value for key, value in query.items()):
            return tool_failure(
                relative,
                "stale_cursor",
                "Continuation cursor does not match this tree request.",
                stale=True,
            )
        if token.get("inventory_hash") != inventory_hash:
            return tool_failure(
                relative,
                "stale_cursor",
                "Workspace tree changed after the previous page.",
                stale=True,
            )
        offset = token.get("next_offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= len(entries):
            return tool_failure(
                relative,
                "invalid_cursor",
                "Continuation cursor is invalid.",
            )
    end = min(offset + max_entries, len(entries))
    has_more = end < len(entries)
    continuation = (
        encode_cursor(
            "workspace_tree",
            {
                "path": relative,
                "max_depth": max_depth,
                "max_entries": max_entries,
                "inventory_hash": inventory_hash,
                "next_offset": end,
            },
        )
        if has_more
        else None
    )
    if scan_truncated:
        warnings.append("tree_scan_entry_limit_reached")
    return {
        "ok": True,
        "path": relative,
        "content": _render_tree_page(relative, entries, offset, end),
        "range": {
            "start_entry": offset,
            "end_entry": end,
            "entry_count": end - offset,
            "total_entries": len(entries),
        },
        "content_hash": inventory_hash,
        "content_version": inventory_hash,
        "truncated": has_more or scan_truncated,
        "warnings": warnings,
        "continuation": continuation,
        "limits": {
            "max_depth": MAX_TREE_DEPTH,
            "max_entries": MAX_TREE_ENTRIES,
            "max_scanned_entries": MAX_TREE_SCAN_ENTRIES,
        },
    }


write_tools = [create_file, write_to_file, get_files_structure]
write_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in write_tools}
