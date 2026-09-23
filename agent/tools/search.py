"""Workspace-bound literal search with fixed file, result, and byte limits."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool

from agent.tools.read_contract import (
    IGNORED_DIRECTORY_NAMES,
    MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES,
    MAX_SEARCH_RESULTS,
    MAX_SEARCH_TERM_CHARS,
    clip_utf8,
    stable_digest,
    tool_failure,
)
from agent.tools.results import tool_access_denied, tool_policy_denial, tool_rejection
from agent.workspace import (
    WorkspaceAccessPolicy,
    WorkspacePathResolver,
    current_workspace_access_policy,
    default_workspace_resolver,
    has_single_regular_file_link,
)

SUPPORTED_TEXT_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
        ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".conf", ".properties", ".xml", ".html", ".htm", ".css", ".scss",
        ".sql", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".md", ".rst",
        ".txt", ".env.example",
    }
)

@dataclass(frozen=True, slots=True)
class _DirectorySearchResult:
    results: list[dict[str, object]]
    content: str
    content_hash: str
    scanned_bytes: int
    files_scanned: int
    files_considered: int
    warnings: list[str]
    skipped_files: list[str]
    truncated: bool
    evidence_bytes: int


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _is_supported(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() in SUPPORTED_TEXT_SUFFIXES or name.endswith(
        ".env.example"
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


def _search_in_text(
    path: str,
    raw: bytes,
    search_term: str,
    context: int,
) -> list[tuple[dict[str, object], str]]:
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("binary")
    lines = text.splitlines()
    needle = search_term.casefold()
    digest = hashlib.sha256(raw).hexdigest()
    matches: list[tuple[dict[str, object], str]] = []
    for index, line in enumerate(lines):
        if needle not in line.casefold():
            continue
        first = max(0, index - context)
        last = min(len(lines), index + context + 1)
        snippet_lines = [
            f"{line_number + 1:4} | {lines[line_number]}"
            for line_number in range(first, last)
        ]
        metadata: dict[str, object] = {
            "path": path,
            "range": {"start_line": first + 1, "end_line": last},
            "match_line": index + 1,
            "content_hash": digest,
        }
        display = (
            f"File: {path}\nMatch found at line: {index + 1}\n"
            + "\n".join(snippet_lines)
            + "\n"
            + ("-" * 50)
        )
        matches.append((metadata, display))
    return matches


def _search_directory(
    resolver: WorkspacePathResolver,
    directory: Path,
    search_term: str,
    context: int,
    *,
    workspace_root: Path,
    access_policy: WorkspaceAccessPolicy | None,
) -> _DirectorySearchResult:
    results: list[dict[str, object]] = []
    rendered: list[str] = []
    warnings: list[str] = []
    skipped_files: list[str] = []
    hashes: list[tuple[str, str]] = []
    scanned_bytes = 0
    files_scanned = 0
    files_considered = 0
    truncated = False
    scan_errors: list[OSError] = []
    stop = False

    def on_walk_error(error: OSError) -> None:
        scan_errors.append(error)

    try:
        walker = os.walk(
            directory, topdown=True, onerror=on_walk_error, followlinks=False
        )
        for root, directories, files in walker:
            root_path = Path(root)
            safe_directories: list[str] = []
            for name in directories:
                path = root_path / name
                if _is_protected(path, workspace_root, access_policy):
                    continue
                if name.casefold() in IGNORED_DIRECTORY_NAMES:
                    continue
                if not _is_link_or_junction(path):
                    safe_directories.append(name)
            directories[:] = sorted(safe_directories)
            for name in sorted(files):
                candidate = root_path / name
                if _is_protected(candidate, workspace_root, access_policy):
                    continue
                if not _is_supported(candidate):
                    continue
                if files_considered >= MAX_SEARCH_FILES:
                    warnings.append("file_limit_reached")
                    truncated = True
                    stop = True
                    break
                files_considered += 1
                if _is_link_or_junction(candidate):
                    continue
                resolution = resolver.resolve_file(str(candidate))
                if not resolution.ok or resolution.path is None:
                    skipped = resolution.relative_path or candidate.name
                    skipped_files.append(skipped)
                    warnings.append(
                        f"Skipped {skipped}: {resolution.error_code.value if resolution.error_code else 'path_invalid'}"
                    )
                    continue
                if not has_single_regular_file_link(resolution.path):
                    warnings.append("Skipped a file denied by workspace link policy.")
                    continue
                try:
                    before = resolution.path.stat(follow_symlinks=False)
                    size = before.st_size
                    remaining = MAX_SEARCH_BYTES - scanned_bytes
                    if size > remaining:
                        skipped_files.append(resolution.relative_path or candidate.name)
                        warnings.append("Skipped a file because the byte limit was reached.")
                        truncated = True
                        continue
                    with resolution.path.open("rb") as source:
                        raw = source.read(size)
                    after = resolution.path.stat(follow_symlinks=False)
                    if len(raw) != size or (
                        before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                    ):
                        skipped_files.append(resolution.relative_path or candidate.name)
                        warnings.append("Skipped a file that changed while it was read.")
                        continue
                    scanned_bytes += len(raw)
                    files_scanned += 1
                    if b"\x00" in raw:
                        raise ValueError("binary")
                    relative = resolution.relative_path or candidate.name
                    file_hash = hashlib.sha256(raw).hexdigest()
                    hashes.append((relative, file_hash))
                    matches = _search_in_text(
                        relative, raw, search_term, context
                    )
                except UnicodeError:
                    skipped_files.append(resolution.relative_path or candidate.name)
                    warnings.append(
                        f"Skipped {resolution.relative_path or candidate.name}: invalid_utf8"
                    )
                    continue
                except ValueError as error:
                    if str(error) != "binary":
                        raise
                    skipped_files.append(resolution.relative_path or candidate.name)
                    warnings.append(
                        f"Skipped {resolution.relative_path or candidate.name}: binary_file"
                    )
                    continue
                except OSError as error:
                    skipped_files.append(resolution.relative_path or candidate.name)
                    warnings.append(
                        f"Skipped {resolution.relative_path or candidate.name}: {type(error).__name__}"
                    )
                    continue

                for metadata, display in matches:
                    if len(results) >= MAX_SEARCH_RESULTS:
                        warnings.append("result_limit_reached")
                        truncated = True
                        stop = True
                        break
                    candidate_results = results + [metadata]
                    metadata_size = len(
                        json.dumps(
                            candidate_results,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                    existing_rendered_size = sum(
                        len(item.encode("utf-8")) for item in rendered
                    ) + max(0, len(rendered) - 1)
                    separator_size = 1 if rendered else 0
                    remaining_output = (
                        MAX_SEARCH_BYTES
                        - metadata_size
                        - existing_rendered_size
                        - separator_size
                    )
                    if remaining_output <= 0:
                        warnings.append("byte_limit_reached")
                        truncated = True
                        stop = True
                        break
                    clipped, was_clipped = clip_utf8(display, remaining_output)
                    if was_clipped:
                        warnings.append("result_snippet_truncated")
                        truncated = True
                    results.append(metadata)
                    rendered.append(clipped)
                    if was_clipped:
                        stop = True
                        break
                if stop:
                    break
            if stop:
                break
    except OSError:
        return _DirectorySearchResult(
            results=[],
            content="",
            content_hash="",
            scanned_bytes=scanned_bytes,
            files_scanned=files_scanned,
            files_considered=files_considered,
            warnings=["directory_scan_failed"],
            skipped_files=skipped_files,
            truncated=True,
            evidence_bytes=0,
        )

    if scan_errors:
        for error in scan_errors:
            warnings.append(f"directory_scan_error:{type(error).__name__}")
        truncated = True
    content = "\n".join(rendered) if rendered else "No matches found."
    content, clipped = clip_utf8(content, MAX_SEARCH_BYTES)
    truncated = truncated or clipped
    evidence_bytes = len(
        json.dumps(
            results,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) + len(content.encode("utf-8"))
    return _DirectorySearchResult(
        results=results,
        content=content,
        content_hash=stable_digest(hashes),
        scanned_bytes=scanned_bytes,
        files_scanned=files_scanned,
        files_considered=files_considered,
        warnings=warnings,
        skipped_files=skipped_files,
        truncated=truncated,
        evidence_bytes=evidence_bytes,
    )


@tool(parse_docstring=True)
def search_keyword_in_directory(
    directory: str, search_term: str, context: int = 2
) -> dict[str, object]:
    """Search bounded UTF-8 source/config files for a literal keyword.

    Args:
        directory: Workspace directory, relative or absolute within the workspace.
        search_term: Literal term to find; it must contain at least 3 characters.
        context: Number of surrounding lines, between 0 and 20.
    """
    if len(search_term) < 3 or len(search_term) > MAX_SEARCH_TERM_CHARS:
        return tool_failure(
            directory,
            "invalid_request",
            f"search_term must contain between 3 and {MAX_SEARCH_TERM_CHARS} characters.",
        )
    if not 0 <= context <= 20:
        return tool_failure(
            directory,
            "invalid_request",
            "context must be between 0 and 20.",
        )
    resolver = default_workspace_resolver()
    policy = current_workspace_access_policy()
    early_denial = tool_policy_denial(resolver, directory, policy)
    if early_denial is not None:
        return early_denial
    resolution = resolver.resolve_directory(directory)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    relative = resolution.relative_path or directory
    if policy is not None:
        decision = policy.check_read(relative)
        if not decision.allowed:
            return tool_access_denied(
                decision.error_code.value, decision.message
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
        access_policy=policy,
    )
    if any(warning.startswith("directory_scan_failed") for warning in result.warnings):
        return tool_failure(
            resolution.relative_path or ".",
            "scan_failed",
            "Workspace directory scan failed.",
            warnings=result.warnings,
        )
    return {
        "ok": True,
        "path": resolution.relative_path or ".",
        "content": result.content,
        "results": result.results,
        "range": {"result_count": len(result.results)},
        "content_hash": result.content_hash,
        "scanned_bytes": result.scanned_bytes,
        "files_scanned": result.files_scanned,
        "files_considered": result.files_considered,
        "evidence_bytes": result.evidence_bytes,
        "truncated": result.truncated,
        "warnings": result.warnings,
        "skipped_files": result.skipped_files,
        "continuation": None,
        "limits": {
            "max_files": MAX_SEARCH_FILES,
            "max_results": MAX_SEARCH_RESULTS,
            "max_bytes": MAX_SEARCH_BYTES,
        },
    }


search_tools = [search_keyword_in_directory]
search_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in search_tools}
