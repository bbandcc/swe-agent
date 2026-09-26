"""Workspace-bound literal search with fixed file, result, and byte limits."""

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool

from agent.tools.read_contract import (
    IGNORED_DIRECTORY_NAMES,
    MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES,
    MAX_SEARCH_RESULTS,
    MAX_SEARCH_SCAN_ENTRIES,
    MAX_SEARCH_TERM_CHARS,
    clip_utf8,
    content_version,
    encode_cursor,
    stable_digest,
    tool_failure,
)
from agent.tools.results import tool_access_denied, tool_policy_denial, tool_rejection
from agent.tools.search_cursor import parse_search_cursor, search_cursor_version
from agent.tools.search_stream import search_chunked_file_page
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
    continuation: str | None


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
    *,
    start_line: int = 0,
) -> Iterator[tuple[dict[str, object], str, int]]:
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("binary")
    lines = text.splitlines()
    needle = search_term.casefold()
    digest = hashlib.sha256(raw).hexdigest()
    for index in range(start_line, len(lines)):
        line = lines[index]
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
            "snippet_truncated": False,
        }
        display = (
            f"File: {path}\nMatch found at line: {index + 1}\n"
            + "\n".join(snippet_lines)
            + "\n"
            + ("-" * 50)
        )
        yield metadata, display, index


def _search_directory(
    resolver: WorkspacePathResolver,
    directory: Path,
    search_term: str,
    context: int,
    *,
    workspace_root: Path,
    access_policy: WorkspaceAccessPolicy | None,
    relative_directory: str,
    cursor_state: dict[str, object] | None,
) -> _DirectorySearchResult:
    results: list[dict[str, object]] = []
    rendered: list[str] = []
    warnings: list[str] = []
    skipped_files: list[str] = []
    hashes: list[tuple[str, str]] = []
    file_evidence: dict[str, str] = {}
    directory_evidence: dict[str, str] = {}
    scanned_bytes = 0
    files_scanned = 0
    files_considered = 0
    scan_entries = 0
    scan_truncated = False
    scan_errors: list[OSError] = []
    paused_at: tuple[int, int, Path] | None = None
    paused_path: str | None = None
    paused_file_state: dict[str, object] | None = None
    start_candidate_index = (
        int(cursor_state["next_candidate_index"])
        if cursor_state is not None
        else 0
    )
    start_line = int(cursor_state["next_line"]) if cursor_state is not None else 0
    start_file_offset = (
        cursor_state.get("next_file_offset") if cursor_state is not None else None
    )
    start_line_number = (
        int(cursor_state["next_line_number"]) if cursor_state is not None else 1
    )
    start_line_open = (
        bool(cursor_state["line_open"]) if cursor_state is not None else False
    )
    start_line_reported = (
        bool(cursor_state["line_reported"]) if cursor_state is not None else False
    )
    candidate_index = 0

    def record_directory_evidence(path: Path) -> None:
        resolution = resolver.resolve_directory(str(path))
        if not resolution.ok or resolution.path is None:
            return
        relative = resolution.relative_path or "."
        try:
            directory_evidence[relative] = content_version(resolution.path)
        except OSError:
            return

    def append_result(metadata: dict[str, object], display: str) -> bool:
        if len(results) >= MAX_SEARCH_RESULTS:
            return False
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
        remaining_output = (
            MAX_SEARCH_BYTES
            - metadata_size
            - existing_rendered_size
            - (1 if rendered else 0)
        )
        if remaining_output <= 0:
            warnings.append("result_byte_limit_reached")
            return False
        clipped, was_clipped = clip_utf8(display, remaining_output)
        if was_clipped:
            metadata["snippet_truncated"] = True
            warnings.append("result_snippet_truncated")
        results.append(metadata)
        rendered.append(clipped)
        return True

    def read_directory_entries(
        path: Path, remaining: int
    ) -> tuple[list[os.DirEntry[str]], bool]:
        entries: list[os.DirEntry[str]] = []
        reached_limit = remaining <= 0
        if reached_limit:
            return entries, reached_limit
        with os.scandir(path) as scanner:
            iterator = iter(scanner)
            while len(entries) < remaining:
                try:
                    entries.append(next(iterator))
                except StopIteration:
                    break
            if len(entries) >= remaining:
                reached_limit = True
                # One extra entry only confirms that the unscanned suffix exists.
                try:
                    next(iterator)
                except StopIteration:
                    pass
        return entries, reached_limit

    try:
        pending_directories = [directory]
        while pending_directories:
            root_path = pending_directories.pop()
            try:
                entries, reached_limit = read_directory_entries(
                    root_path, MAX_SEARCH_SCAN_ENTRIES - scan_entries
                )
            except OSError as error:
                if root_path == directory:
                    raise
                scan_errors.append(error)
                continue
            scan_entries += len(entries)
            safe_directories: list[Path] = []
            files = []
            for entry in sorted(entries, key=lambda item: item.name):
                name = entry.name
                candidate = root_path / name
                if _is_protected(candidate, workspace_root, access_policy):
                    continue
                try:
                    is_directory = entry.is_dir()
                except OSError as error:
                    scan_errors.append(error)
                    continue
                if is_directory:
                    if name.casefold() in IGNORED_DIRECTORY_NAMES:
                        continue
                    if not _is_link_or_junction(candidate):
                        safe_directories.append(candidate)
                    continue
                files.append(entry)
            scan_truncated = scan_truncated or reached_limit
            for entry in files:
                name = entry.name
                candidate = root_path / name
                if not _is_supported(candidate):
                    continue
                current_index = candidate_index
                candidate_index += 1
                if current_index < start_candidate_index:
                    continue
                if (
                    files_considered >= MAX_SEARCH_FILES
                    or len(results) >= MAX_SEARCH_RESULTS
                    or scanned_bytes >= MAX_SEARCH_BYTES
                ):
                    record_directory_evidence(root_path)
                    paused_at = (current_index, 0, root_path)
                    paused_path = candidate.relative_to(workspace_root).as_posix()
                    break
                files_considered += 1
                record_directory_evidence(root_path)
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
                relative = resolution.relative_path or candidate.name
                result_start = len(results)
                rendered_start = len(rendered)
                hash_start = len(hashes)
                warning_start = len(warnings)
                try:
                    before_version = content_version(resolution.path)
                    size = resolution.path.stat(follow_symlinks=False).st_size
                    file_evidence[relative] = before_version
                    resumed_file = (
                        current_index == start_candidate_index
                        and isinstance(start_file_offset, int)
                    )
                    if resumed_file or size > MAX_SEARCH_BYTES - scanned_bytes:
                        offset = int(start_file_offset) if resumed_file else 0
                        if offset > size:
                            skipped_files.append(relative)
                            warnings.append("stale_cursor")
                            continue
                        page = search_chunked_file_page(
                            relative,
                            resolution.path,
                            search_term,
                            file_size=size,
                            file_offset=offset,
                            line_number=(start_line_number if resumed_file else 1),
                            line_open=(start_line_open if resumed_file else False),
                            line_reported=(start_line_reported if resumed_file else False),
                            byte_budget=MAX_SEARCH_BYTES - scanned_bytes,
                            on_match=append_result,
                        )
                        scanned_bytes += page.scanned_bytes
                        files_scanned += 1
                        after_version = content_version(resolution.path)
                        if before_version != after_version:
                            del results[result_start:]
                            del rendered[rendered_start:]
                            del hashes[hash_start:]
                            del warnings[warning_start:]
                            file_evidence.pop(relative, None)
                            skipped_files.append(relative)
                            warnings.append("Skipped a file that changed while it was read.")
                            continue
                        hashes.append((relative, page.content_hash))
                        file_evidence[relative] = after_version
                        if context and len(results) > result_start:
                            warnings.append("chunked_file_context_limited")
                        if not page.complete:
                            paused_at = (current_index, 0, root_path)
                            paused_path = relative
                            paused_file_state = {
                                "next_file_offset": page.next_offset,
                                "next_line_number": page.next_line_number,
                                "line_open": page.line_open,
                                "line_reported": page.line_reported,
                            }
                            break
                        continue

                    with resolution.path.open("rb") as source:
                        raw = source.read(size)
                    after_version = content_version(resolution.path)
                    scanned_bytes += len(raw)
                    if len(raw) != size or before_version != after_version:
                        file_evidence.pop(relative, None)
                        skipped_files.append(resolution.relative_path or candidate.name)
                        warnings.append("Skipped a file that changed while it was read.")
                        continue
                    files_scanned += 1
                    if b"\x00" in raw:
                        raise ValueError("binary")
                    file_hash = hashlib.sha256(raw).hexdigest()
                    hashes.append((relative, file_hash))
                    line_offset = (
                        start_line if current_index == start_candidate_index else 0
                    )
                    for metadata, display, line_index in _search_in_text(
                        relative,
                        raw,
                        search_term,
                        context,
                        start_line=line_offset,
                    ):
                        metadata["content_hash_scope"] = "file"
                        if not append_result(metadata, display):
                            paused_at = (current_index, line_index, root_path)
                            paused_path = relative
                            break
                        if metadata["snippet_truncated"]:
                            paused_at = (current_index, line_index + 1, root_path)
                            paused_path = relative
                            break
                except UnicodeError:
                    del results[result_start:]
                    del rendered[rendered_start:]
                    del hashes[hash_start:]
                    del warnings[warning_start:]
                    skipped_files.append(resolution.relative_path or candidate.name)
                    warnings.append(
                        f"Skipped {resolution.relative_path or candidate.name}: invalid_utf8"
                    )
                    continue
                except ValueError as error:
                    del results[result_start:]
                    del rendered[rendered_start:]
                    del hashes[hash_start:]
                    del warnings[warning_start:]
                    if str(error) != "binary":
                        raise
                    skipped_files.append(resolution.relative_path or candidate.name)
                    warnings.append(
                        f"Skipped {resolution.relative_path or candidate.name}: binary_file"
                    )
                    continue
                except OSError as error:
                    del results[result_start:]
                    del rendered[rendered_start:]
                    del hashes[hash_start:]
                    del warnings[warning_start:]
                    file_evidence.pop(relative, None)
                    skipped_files.append(resolution.relative_path or candidate.name)
                    warnings.append(
                        f"Skipped {resolution.relative_path or candidate.name}: {type(error).__name__}"
                    )
                    continue
                if paused_at is not None:
                    break
            if paused_at is not None:
                break
            if scan_truncated:
                break
            pending_directories.extend(reversed(safe_directories))
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
            truncated=False,
            evidence_bytes=0,
            continuation=None,
        )

    if scan_errors:
        for error in scan_errors:
            warnings.append(f"directory_scan_error:{type(error).__name__}")
    if scan_truncated:
        warnings.append("directory_scan_entry_limit_reached")
    content = "\n".join(rendered) if rendered else "No matches found."
    content, clipped = clip_utf8(content, MAX_SEARCH_BYTES)
    if clipped and paused_at is None:
        warnings.append("result_content_truncated")
    evidence_bytes = len(
        json.dumps(
            results,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) + len(content.encode("utf-8"))
    continuation = None
    # A stateless search cursor would replay the prefix on the next call. Once
    # this request hits the scan cap, fail closed instead of issuing a cursor
    # that might repeatedly rescan that prefix without reaching later entries.
    if paused_at is not None and not scan_truncated:
        next_index, next_line, parent = paused_at
        record_directory_evidence(parent)
        file_evidence_items = sorted(file_evidence.items())
        directory_evidence_items = sorted(directory_evidence.items())
        cursor_values: dict[str, object] = {
            "directory": relative_directory,
            "search_term": search_term,
            "context": context,
            "next_candidate_index": next_index,
            "next_line": next_line,
            "next_file_path": paused_path,
            "next_file_offset": (
                paused_file_state.get("next_file_offset")
                if paused_file_state is not None
                else None
            ),
            "next_line_number": (
                paused_file_state.get("next_line_number", 1)
                if paused_file_state is not None
                else 1
            ),
            "line_open": (
                paused_file_state.get("line_open", False)
                if paused_file_state is not None
                else False
            ),
            "line_reported": (
                paused_file_state.get("line_reported", False)
                if paused_file_state is not None
                else False
            ),
            "file_evidence": file_evidence_items,
            "directory_evidence": directory_evidence_items,
        }
        cursor_values["search_version"] = search_cursor_version(cursor_values)
        continuation = encode_cursor("workspace_search", cursor_values)
    return _DirectorySearchResult(
        results=results,
        content=content,
        content_hash=stable_digest(hashes),
        scanned_bytes=scanned_bytes,
        files_scanned=files_scanned,
        files_considered=files_considered,
        warnings=warnings,
        skipped_files=skipped_files,
        truncated=scan_truncated or continuation is not None,
        evidence_bytes=evidence_bytes,
        continuation=continuation,
    )


@tool(parse_docstring=True)
def search_keyword_in_directory(
    directory: str,
    search_term: str,
    context: int = 2,
    cursor: str | None = None,
) -> dict[str, object]:
    """Search bounded UTF-8 source/config files for a literal keyword.

    Args:
        directory: Workspace directory, relative or absolute within the workspace.
        search_term: Literal term to find; it must contain at least 3 characters.
        context: Number of surrounding lines, between 0 and 20.
        cursor: Continuation from the previous page for this exact query.
    """
    if (
        not isinstance(search_term, str)
        or len(search_term) < 3
        or len(search_term) > MAX_SEARCH_TERM_CHARS
    ):
        return tool_failure(
            directory,
            "invalid_request",
            f"search_term must contain between 3 and {MAX_SEARCH_TERM_CHARS} characters.",
        )
    if (
        isinstance(context, bool)
        or not isinstance(context, int)
        or not 0 <= context <= 20
    ):
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
    cursor_state = None
    if cursor is not None:
        if not isinstance(cursor, str):
            return tool_failure(
                relative, "invalid_cursor", "Continuation cursor is invalid."
            )
        cursor_state, cursor_error = parse_search_cursor(
            cursor,
            directory=relative,
            search_term=search_term,
            context=context,
            resolver=resolver,
            access_policy=policy,
        )
        if cursor_error is not None:
            return tool_failure(
                relative,
                cursor_error,
                "Continuation cursor is invalid or stale.",
                stale=cursor_error == "stale_cursor",
            )
    result = _search_directory(
        resolver,
        resolution.path,
        search_term,
        context,
        workspace_root=workspace_root.path,
        access_policy=policy,
        relative_directory=relative,
        cursor_state=cursor_state,
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
        "continuation": result.continuation,
        "limits": {
            "max_files": MAX_SEARCH_FILES,
            "max_results": MAX_SEARCH_RESULTS,
            "max_bytes": MAX_SEARCH_BYTES,
            "max_scanned_entries": MAX_SEARCH_SCAN_ENTRIES,
        },
    }


search_tools = [search_keyword_in_directory]
search_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in search_tools}
