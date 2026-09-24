"""Workspace-bound tree-sitter code inspection tools."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import tool

from agent.tools.results import (
    tool_access_denied,
    tool_error,
    tool_policy_denial,
    tool_rejection,
)
from agent.tools.read_contract import (
    MAX_RAW_PAGE_BYTES,
    content_version,
    decode_cursor,
    encode_cursor,
    tool_failure,
)
from agent.tools.symbol_contract import (
    MAX_SYMBOL_ENTRIES,
    MAX_SYMBOL_INPUT_BYTES,
    MAX_SYMBOL_SOURCE_BYTES,
    SourceSymbol,
    SymbolExtraction,
    SymbolIssue,
)
from agent.tools.python_symbols import (
    extract_python_symbols,
)
from agent.tools.javascript_symbols import extract_javascript_symbols
from agent.tools.typescript_symbols import extract_typescript_symbols
from agent.workspace import (
    current_workspace_access_policy,
    default_workspace_resolver,
    has_single_regular_file_link,
)

MAX_CODE_SYMBOL_OUTPUT_BYTES = 131_072
MAX_CODE_SYMBOL_FILES = 16
_SYMBOL_ADAPTERS: dict[str, tuple[Callable[..., SymbolExtraction], str]] = {
    ".py": (extract_python_symbols, "Python"),
    ".js": (extract_javascript_symbols, "JavaScript"),
    ".jsx": (extract_javascript_symbols, "JavaScript/JSX"),
    ".mjs": (extract_javascript_symbols, "JavaScript"),
    ".cjs": (extract_javascript_symbols, "JavaScript"),
    ".ts": (extract_typescript_symbols, "TypeScript"),
}
_UNSUPPORTED_LANGUAGE_SUFFIXES = {".tsx"}


def _language_error(path: Path, display_path: str) -> dict[str, object] | None:
    suffix = path.suffix.lower()
    if suffix in _UNSUPPORTED_LANGUAGE_SUFFIXES:
        return tool_error(
            display_path,
            "unsupported_language",
            "TSX symbol extraction is not supported; use "
            "search_keyword_in_directory for bounded text search.",
        )
    if suffix not in _SYMBOL_ADAPTERS:
        return tool_error(
            display_path,
            "unsupported_file_type",
            f"Symbol extraction does not support {suffix or '<no extension>'}.",
        )
    return None


def _read_symbol_source(path: Path) -> bytes | SymbolIssue:
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_size > MAX_SYMBOL_INPUT_BYTES:
        return SymbolIssue(
            "source_too_large",
            "Symbol input exceeds the fixed source-size limit.",
        )
    with path.open("rb") as source_file:
        source = source_file.read(MAX_SYMBOL_INPUT_BYTES + 1)
    if len(source) > MAX_SYMBOL_INPUT_BYTES:
        return SymbolIssue(
            "source_too_large",
            "Symbol input exceeds the fixed source-size limit.",
        )
    return source


def _issue_result(path: str, issue: SymbolIssue) -> dict[str, object]:
    result = tool_error(path, issue.error_code, issue.message)
    if issue.details is not None:
        result["details"] = issue.details
    return result


def _bounded_candidate_result(result: dict[str, object]) -> dict[str, object]:
    candidates = result.get("candidates")
    while len(
        json.dumps(result, ensure_ascii=False).encode("utf-8")
    ) > MAX_CODE_SYMBOL_OUTPUT_BYTES:
        if not isinstance(candidates, list) or not candidates:
            return tool_error(
                str(result.get("path") or "<workspace>"),
                "symbol_output_limit",
                "Symbol candidate metadata exceeds the fixed output-size limit.",
            )
        candidates.pop()
        result["truncated"] = True
    return result


def _bounded_symbol_response(
    path: str,
    symbols: list[SourceSymbol],
    *,
    file_hash: str | None,
    truncated: bool,
    language: str = "Code",
) -> dict[str, object]:
    summary = ", ".join(symbol.qualified_symbol for symbol in symbols)
    heading = f"{language} symbols"
    response: dict[str, object] = {
        "ok": True,
        "path": path,
        "content": (
            f"{heading}: {summary}"
            if summary
            else f"No {language.lower()} symbols found."
        ),
        "symbols": [symbol.to_dict() for symbol in symbols],
        "file_hash": file_hash,
        "truncated": truncated,
        "limits": {
            "max_entries": MAX_SYMBOL_ENTRIES,
            "max_source_bytes": MAX_SYMBOL_SOURCE_BYTES,
            "max_files": MAX_CODE_SYMBOL_FILES,
            "max_output_bytes": MAX_CODE_SYMBOL_OUTPUT_BYTES,
        },
    }
    while len(
        json.dumps(response, ensure_ascii=False).encode("utf-8")
    ) > MAX_CODE_SYMBOL_OUTPUT_BYTES:
        current = response["symbols"]
        assert isinstance(current, list)
        if not current:
            return tool_error(
                path,
                "symbol_output_limit",
                "Symbol metadata exceeds the fixed output-size limit.",
            )
        current.pop()
        names = ", ".join(
            str(item.get("qualified_symbol", ""))
            for item in current
            if isinstance(item, dict)
        )
        response["content"] = f"{heading}: {names}; output truncated."
        response["truncated"] = True
    return response


def _extract_file(
    path: Path,
    canonical_path: str,
    *,
    max_entries: int = MAX_SYMBOL_ENTRIES,
    max_source_bytes: int = MAX_SYMBOL_SOURCE_BYTES,
) -> SymbolExtraction | SymbolIssue:
    try:
        source = _read_symbol_source(path)
    except OSError as error:
        return SymbolIssue("read_failed", f"Could not read file: {error}")
    if isinstance(source, SymbolIssue):
        return source
    extractor, _ = _SYMBOL_ADAPTERS[path.suffix.lower()]
    extraction = extractor(
        canonical_path,
        source,
        max_entries=max_entries,
        max_source_bytes=max_source_bytes,
    )
    return extraction.issue or extraction


def _resolve_file(file_path: str):
    return default_workspace_resolver().resolve_file(file_path)


def _read_denial(resolution):
    if not resolution.ok:
        return None
    if resolution.path is not None and not has_single_regular_file_link(
        resolution.path
    ):
        return tool_access_denied(
            "read_denied",
            "Workspace read access is denied for this file.",
        )
    policy = current_workspace_access_policy()
    if policy is None:
        return None
    decision = policy.check_read(resolution.relative_path or resolution.requested_path)
    if decision.allowed:
        return None
    return tool_access_denied(decision.error_code.value, decision.message)


@tool(parse_docstring=True)
def get_code_definitions(file_path: str) -> dict[str, object]:
    """Extract bounded, exact symbols from one supported source file.

    Args:
        file_path: Workspace file, relative or absolute within the workspace.
    """
    resolver = default_workspace_resolver()
    policy = current_workspace_access_policy()
    early_denial = tool_policy_denial(resolver, file_path, policy)
    if early_denial is not None:
        return early_denial
    resolution = resolver.resolve_file(file_path)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    denied = _read_denial(resolution)
    if denied is not None:
        return denied
    display_path = resolution.relative_path or file_path
    unsupported = _language_error(resolution.path, display_path)
    if unsupported is not None:
        return unsupported
    extraction = _extract_file(resolution.path, display_path)
    if isinstance(extraction, SymbolIssue):
        return _issue_result(display_path, extraction)
    _, language = _SYMBOL_ADAPTERS[resolution.path.suffix.lower()]
    return _bounded_symbol_response(
        display_path,
        list(extraction.symbols),
        file_hash=extraction.file_hash,
        truncated=extraction.truncated,
        language=language,
    )


@tool(parse_docstring=True)
def get_function_implementation(
    file_path: str, function_name: str
) -> dict[str, object]:
    """Return one uniquely matched supported-language function or method slice.

    Args:
        file_path: Workspace file, relative or absolute within the workspace.
        function_name: Bare or qualified function/method name to find.
    """
    resolver = default_workspace_resolver()
    policy = current_workspace_access_policy()
    early_denial = tool_policy_denial(resolver, file_path, policy)
    if early_denial is not None:
        return early_denial
    resolution = resolver.resolve_file(file_path)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    denied = _read_denial(resolution)
    if denied is not None:
        return denied
    display_path = resolution.relative_path or file_path
    unsupported = _language_error(resolution.path, display_path)
    if unsupported is not None:
        return unsupported
    extraction = _extract_file(resolution.path, display_path)
    if isinstance(extraction, SymbolIssue):
        return _issue_result(display_path, extraction)

    function_symbols = [
        symbol
        for symbol in extraction.symbols
        if symbol.kind in {"function", "method"}
    ]
    if "." in function_name:
        matches = [
            symbol
            for symbol in function_symbols
            if symbol.qualified_symbol == function_name
        ]
    else:
        matches = [
            symbol
            for symbol in function_symbols
            if symbol.qualified_symbol.rsplit(".", 1)[-1] == function_name
        ]
    if extraction.truncated:
        result = tool_error(
            display_path,
            "symbol_index_truncated",
            "The bounded symbol index was truncated; uniqueness cannot "
            "be established.",
        )
        result["truncated"] = True
        result["candidates"] = [
            {
                "qualified_symbol": symbol.qualified_symbol,
                "kind": symbol.kind,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
            }
            for symbol in matches[:MAX_SYMBOL_ENTRIES]
        ]
        return _bounded_candidate_result(result)
    if not matches:
        return tool_error(
            display_path,
            "definition_not_found",
            "No matching function or method was found.",
        )
    if len(matches) != 1:
        return _bounded_candidate_result({
            "ok": False,
            "path": display_path,
            "error_code": "ambiguous_symbol",
            "message": "Multiple symbols match; use a qualified symbol name.",
            "candidates": [
                {
                    "qualified_symbol": symbol.qualified_symbol,
                    "kind": symbol.kind,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "start_byte": symbol.start_byte,
                    "end_byte": symbol.end_byte,
                }
                for symbol in matches
            ],
            "truncated": False,
        })
    return _bounded_symbol_response(
        display_path,
        matches,
        file_hash=extraction.file_hash,
        truncated=False,
        language=_SYMBOL_ADAPTERS[resolution.path.suffix.lower()][1],
    )


@tool(parse_docstring=True)
def get_code_definitions_multi(file_paths: list[str]) -> dict[str, object]:
    """Extract bounded exact symbols from several supported source files.

    Args:
        file_paths: Workspace files to inspect.
    """
    if len(file_paths) > MAX_CODE_SYMBOL_FILES:
        return tool_error(
            "<multiple>",
            "file_limit_exceeded",
            f"At most {MAX_CODE_SYMBOL_FILES} files can be inspected at once.",
        )
    resolver = default_workspace_resolver()
    policy = current_workspace_access_policy()
    for file_path in file_paths:
        early_denial = tool_policy_denial(resolver, file_path, policy)
        if early_denial is not None:
            return early_denial
    resolutions = [resolver.resolve_file(file_path) for file_path in file_paths]
    for resolution in resolutions:
        if not resolution.ok or resolution.path is None:
            return tool_rejection(resolution)
        denied = _read_denial(resolution)
        if denied is not None:
            return denied

    symbols: list[SourceSymbol] = []
    truncated = False
    output_source_bytes = 0
    for index, resolution in enumerate(resolutions):
        assert resolution.path is not None
        display_path = resolution.relative_path or resolution.requested_path
        unsupported = _language_error(resolution.path, display_path)
        if unsupported is not None:
            return unsupported
        remaining_entries = MAX_SYMBOL_ENTRIES - len(symbols)
        remaining_source_bytes = MAX_SYMBOL_SOURCE_BYTES - output_source_bytes
        extraction = _extract_file(
            resolution.path,
            display_path,
            max_entries=remaining_entries,
            max_source_bytes=remaining_source_bytes,
        )
        if isinstance(extraction, SymbolIssue):
            return _issue_result(display_path, extraction)
        symbols.extend(extraction.symbols)
        output_source_bytes += sum(
            len(symbol.source.encode("utf-8")) for symbol in extraction.symbols
        )
        if extraction.truncated:
            truncated = True
            break
        if len(symbols) >= MAX_SYMBOL_ENTRIES and index + 1 < len(resolutions):
            truncated = True
            break
    result = _bounded_symbol_response(
        "<multiple>",
        symbols,
        file_hash=None,
        truncated=truncated,
        language="Code",
    )
    if result.get("ok"):
        result["truncated"] = bool(result["truncated"]) or truncated
        while len(
            json.dumps(result, ensure_ascii=False).encode("utf-8")
        ) > MAX_CODE_SYMBOL_OUTPUT_BYTES:
            kept = result["symbols"]
            assert isinstance(kept, list)
            if not kept:
                return tool_error(
                    "<multiple>",
                    "symbol_output_limit",
                    "Symbol metadata exceeds the fixed output-size limit.",
                )
            kept.pop()
            result["truncated"] = True
            names = ", ".join(
                str(item.get("qualified_symbol", ""))
                for item in kept
                if isinstance(item, dict)
            )
            result["content"] = f"Python symbols: {names}; output truncated."
    return result


@tool(parse_docstring=True)
def get_raw_file_content(
    file_path: str,
    start_byte: int = 0,
    end_byte: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    """Read a bounded UTF-8 byte range with version-bound pagination.

    Args:
        file_path: Workspace file, relative or absolute within the workspace.
        start_byte: Inclusive UTF-8 byte offset for the requested range.
        end_byte: Exclusive UTF-8 byte offset, or None for end of file.
        cursor: Continuation from the preceding page; repeat the same range.
    """
    if (
        isinstance(start_byte, bool)
        or not isinstance(start_byte, int)
        or start_byte < 0
        or (
            end_byte is not None
            and (
                isinstance(end_byte, bool)
                or not isinstance(end_byte, int)
                or end_byte < start_byte
            )
        )
    ):
        return tool_failure(file_path, "invalid_range", "Byte range is invalid.")
    resolver = default_workspace_resolver()
    early_denial = tool_policy_denial(
        resolver, file_path, current_workspace_access_policy()
    )
    if early_denial is not None:
        return early_denial
    resolution = resolver.resolve_file(file_path)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    denied = _read_denial(resolution)
    if denied is not None:
        return denied
    try:
        current_version = content_version(resolution.path)
        metadata = resolution.path.stat(follow_symlinks=False)
        size = metadata.st_size
        relative = resolution.relative_path or file_path
        if cursor is None:
            if start_byte > size or (end_byte is not None and end_byte > size):
                return tool_failure(
                    relative, "invalid_range", "Byte range exceeds file size."
                )
            offset = start_byte
            requested_end = end_byte
        else:
            token = decode_cursor(cursor, "raw_file")
            if token is None:
                return tool_failure(
                    relative, "invalid_cursor", "Continuation cursor is invalid."
                )
            expected_query = {
                "path": relative,
                "start_byte": start_byte,
                "end_byte": end_byte,
            }
            actual_query = {
                key: token.get(key)
                for key in ("path", "start_byte", "end_byte")
            }
            if actual_query != expected_query:
                return tool_failure(
                    relative,
                    "stale_cursor",
                    "Continuation cursor does not match this read request.",
                    stale=True,
                )
            if token.get("file_version") != current_version:
                return tool_failure(
                    relative,
                    "stale_cursor",
                    "The file changed after the previous page was read.",
                    stale=True,
                )
            offset = token.get("next_offset")
            requested_end = end_byte
            if isinstance(offset, bool) or not isinstance(offset, int):
                return tool_failure(
                    relative, "invalid_cursor", "Continuation cursor is invalid."
                )
        range_end = size if requested_end is None else requested_end
        if not 0 <= offset <= range_end <= size:
            return tool_failure(
                relative,
                "stale_cursor",
                "Continuation cursor range is no longer valid.",
                stale=True,
            )
        page_size = min(MAX_RAW_PAGE_BYTES, range_end - offset)
        if not has_single_regular_file_link(resolution.path):
            return tool_access_denied(
                "read_denied", "Workspace read access is denied for this file."
            )
        with resolution.path.open("rb") as source:
            source.seek(offset)
            raw = source.read(page_size)
        next_offset = offset + len(raw)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            if (
                error.reason == "unexpected end of data"
                and next_offset < range_end
                and error.start >= max(0, len(raw) - 3)
            ):
                raw = raw[: error.start]
                next_offset = offset + len(raw)
                content = raw.decode("utf-8")
            else:
                return tool_failure(
                    relative,
                    "encoding_error",
                    "The selected byte range is not valid UTF-8 text.",
                )
        after_version = content_version(resolution.path)
        if after_version != current_version:
            return tool_failure(
                relative,
                "stale_cursor",
                "The file changed while the page was being read.",
                stale=True,
            )
        has_more = next_offset < range_end
        continuation = (
            encode_cursor(
                "raw_file",
                {
                    "path": relative,
                    "start_byte": start_byte,
                    "end_byte": end_byte,
                    "next_offset": next_offset,
                    "file_version": current_version,
                },
            )
            if has_more
            else None
        )
    except UnicodeDecodeError:
        return tool_error(file_path, "encoding_error", "The file is not valid UTF-8.")
    except OSError as error:
        return tool_error(file_path, "read_failed", f"Could not read file: {error}")
    return {
        "ok": True,
        "path": relative,
        "content": content,
        "range": {
            "start_byte": offset,
            "end_byte": next_offset,
            "total_bytes": size,
        },
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "content_version": current_version,
        "truncated": has_more,
        "warnings": [],
        "continuation": continuation,
        "limits": {"max_bytes": MAX_RAW_PAGE_BYTES},
    }


codemap_tools = [
    get_code_definitions,
    get_function_implementation,
    get_code_definitions_multi,
    get_raw_file_content,
]
codemap_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in codemap_tools}
