"""Tree-sitter JavaScript/JSX symbol extraction from original source bytes."""

from __future__ import annotations

import hashlib
from typing import Any

from tree_sitter_languages import get_parser

from agent.tools.symbol_contract import (
    MAX_SYMBOL_ENTRIES,
    MAX_SYMBOL_INPUT_BYTES,
    MAX_SYMBOL_SOURCE_BYTES,
    SourceSymbol,
    SymbolExtraction,
    SymbolIssue,
)

MAX_JAVASCRIPT_SOURCE_BYTES = MAX_SYMBOL_INPUT_BYTES


def _first_parse_issue(root: Any) -> dict[str, object] | None:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            return {
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "node_type": node.type,
            }
        stack.extend(reversed(node.children))
    return {
        "start_byte": root.start_byte,
        "end_byte": root.end_byte,
        "start_line": root.start_point[0] + 1,
        "end_line": root.end_point[0] + 1,
        "node_type": root.type,
    }


def _name_text(source_bytes: bytes, node: Any) -> str | None:
    if node is None:
        return None
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8")


def _line_range(node: Any) -> tuple[int, int]:
    start_line = node.start_point[0] + 1
    end_line = (
        node.end_point[0] + 1
        if node.end_point[1] > 0
        else max(1, node.end_point[0])
    )
    return start_line, end_line


def extract_javascript_symbols(
    canonical_path: str,
    source_bytes: bytes,
    *,
    max_entries: int = MAX_SYMBOL_ENTRIES,
    max_source_bytes: int = MAX_SYMBOL_SOURCE_BYTES,
) -> SymbolExtraction:
    """Extract bounded JavaScript/JSX definitions from Tree-sitter node ranges.

    Supported definitions are function/class declarations, methods, and arrow
    functions assigned to a variable declarator. Each source field is decoded
    only from the original byte slice selected by a Tree-sitter node.
    """
    file_hash = hashlib.sha256(source_bytes).hexdigest()
    if len(source_bytes) > MAX_JAVASCRIPT_SOURCE_BYTES:
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "source_too_large",
                "JavaScript symbol input exceeds the fixed source-size limit.",
            ),
        )
    if max_entries < 0 or max_source_bytes < 0:
        raise ValueError("symbol extraction limits must be non-negative")
    try:
        source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue("encoding_error", "The file is not valid UTF-8."),
        )

    try:
        parser = get_parser("javascript")
        tree = parser.parse(source_bytes)
    except (KeyError, TypeError, ValueError):
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "tree_sitter_error",
                "The installed Tree-sitter JavaScript parser could not process this file.",
            ),
        )
    if tree.root_node.has_error:
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "parse_error",
                "Tree-sitter could not parse the JavaScript/JSX source.",
                _first_parse_issue(tree.root_node),
            ),
        )

    symbols: list[SourceSymbol] = []
    emitted_source_bytes = 0
    truncated = False
    pending = [(tree.root_node, (), "module", None)]
    while pending:
        node, scope, parent_scope_kind, export_span = pending.pop()

        if node.type == "export_statement":
            pending.extend(
                (child, scope, parent_scope_kind, node)
                for child in reversed(node.named_children)
            )
            continue

        if node.type in {"function_declaration", "class_declaration", "method_definition"}:
            name = _name_text(source_bytes, node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            if name is None or body is None:
                if body is not None:
                    pending.extend(
                        (child, scope, "function", None)
                        for child in reversed(body.named_children)
                    )
                continue
            if node.type == "class_declaration":
                kind = "class"
                child_scope_kind = "class"
            elif node.type == "method_definition" or parent_scope_kind == "class":
                kind = "method"
                child_scope_kind = "function"
            else:
                kind = "function"
                child_scope_kind = "function"
            source_node = export_span or node
            source_slice = source_bytes[source_node.start_byte : source_node.end_byte]
            if len(symbols) >= max_entries or (
                emitted_source_bytes + len(source_slice) > max_source_bytes
            ):
                truncated = True
                break
            start_line, end_line = _line_range(source_node)
            symbols.append(
                SourceSymbol(
                    path=canonical_path,
                    qualified_symbol=".".join((*scope, name)),
                    kind=kind,
                    start_byte=source_node.start_byte,
                    end_byte=source_node.end_byte,
                    start_line=start_line,
                    end_line=end_line,
                    source_hash=hashlib.sha256(source_slice).hexdigest(),
                    source=source_slice.decode("utf-8"),
                )
            )
            emitted_source_bytes += len(source_slice)
            pending.extend(
                (child, (*scope, name), child_scope_kind, None)
                for child in reversed(body.named_children)
            )
            continue

        if node.type == "variable_declarator":
            name = _name_text(source_bytes, node.child_by_field_name("name"))
            value = node.child_by_field_name("value")
            if name is not None and value is not None and value.type == "arrow_function":
                source_node = export_span or node
                source_slice = source_bytes[source_node.start_byte : source_node.end_byte]
                if len(symbols) >= max_entries or (
                    emitted_source_bytes + len(source_slice) > max_source_bytes
                ):
                    truncated = True
                    break
                start_line, end_line = _line_range(source_node)
                symbols.append(
                    SourceSymbol(
                        path=canonical_path,
                        qualified_symbol=".".join((*scope, name)),
                        kind="method" if parent_scope_kind == "class" else "function",
                        start_byte=source_node.start_byte,
                        end_byte=source_node.end_byte,
                        start_line=start_line,
                        end_line=end_line,
                        source_hash=hashlib.sha256(source_slice).hexdigest(),
                        source=source_slice.decode("utf-8"),
                    )
                )
                emitted_source_bytes += len(source_slice)
                body = value.child_by_field_name("body")
                if body is not None:
                    pending.extend(
                        (child, (*scope, name), "function", None)
                        for child in reversed(body.named_children)
                    )
                continue

        pending.extend(
            (child, scope, parent_scope_kind, export_span)
            for child in reversed(node.named_children)
        )

    return SymbolExtraction(tuple(symbols), file_hash, truncated=truncated)


__all__ = ["MAX_JAVASCRIPT_SOURCE_BYTES", "extract_javascript_symbols"]
