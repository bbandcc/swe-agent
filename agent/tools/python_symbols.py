"""Tree-sitter backed Python symbol extraction over original source bytes."""

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

MAX_PYTHON_SOURCE_BYTES = MAX_SYMBOL_INPUT_BYTES

# Keep the Python-named values importable for existing callers.
PythonSymbol = SourceSymbol
PythonSymbolIssue = SymbolIssue
PythonSymbolExtraction = SymbolExtraction


def _definition_node(node: Any) -> tuple[Any, Any] | None:
    """Return (definition, source span), including decorators when present."""
    if node.type == "decorated_definition":
        definition = next(
            (
                child
                for child in node.named_children
                if child.type in {"function_definition", "class_definition"}
            ),
            None,
        )
        return (definition, node) if definition is not None else None
    if node.type in {"function_definition", "class_definition"}:
        return node, node
    return None


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


def extract_python_symbols(
    canonical_path: str,
    source_bytes: bytes,
    *,
    max_entries: int = MAX_SYMBOL_ENTRIES,
    max_source_bytes: int = MAX_SYMBOL_SOURCE_BYTES,
) -> SymbolExtraction:
    """Extract bounded Python definitions using Tree-sitter byte ranges.

    The source field is decoded only from ``source_bytes[start_byte:end_byte]``;
    no function or class text is synthesized from signatures or body fragments.
    Definitions are found by walking Python grammar nodes; no query is compiled.
    """
    file_hash = hashlib.sha256(source_bytes).hexdigest()
    if len(source_bytes) > MAX_PYTHON_SOURCE_BYTES:
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "source_too_large",
                "Python symbol input exceeds the fixed source-size limit.",
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
            issue=SymbolIssue(
                "encoding_error", "The file is not valid UTF-8."
            ),
        )

    try:
        parser = get_parser("python")
        tree = parser.parse(source_bytes)
    except (KeyError, TypeError, ValueError):
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "tree_sitter_error",
                "The installed Tree-sitter Python parser could not process this file.",
            ),
        )
    if tree.root_node.has_error:
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "parse_error",
                "Tree-sitter could not parse the Python source.",
                _first_parse_issue(tree.root_node),
            ),
        )

    symbols: list[SourceSymbol] = []
    emitted_source_bytes = 0
    truncated = False

    pending = [(tree.root_node, (), "module")]
    while pending:
        node, scope, parent_scope_kind = pending.pop()
        definition_pair = _definition_node(node)
        if definition_pair is None:
            pending.extend(
                (child, scope, parent_scope_kind)
                for child in reversed(node.named_children)
            )
            continue

        definition, source_node = definition_pair
        name_node = definition.child_by_field_name("name")
        body_node = definition.child_by_field_name("body")
        if name_node is None or body_node is None:
            continue
        name = name_node.text.decode("utf-8")
        is_class = definition.type == "class_definition"
        kind = "class" if is_class else (
            "method" if parent_scope_kind == "class" else "function"
        )
        source_slice = source_bytes[source_node.start_byte : source_node.end_byte]
        if len(symbols) >= max_entries or (
            emitted_source_bytes + len(source_slice) > max_source_bytes
        ):
            truncated = True
            break
        symbols.append(
            SourceSymbol(
                path=canonical_path,
                qualified_symbol=".".join((*scope, name)),
                kind=kind,
                start_byte=source_node.start_byte,
                end_byte=source_node.end_byte,
                start_line=source_node.start_point[0] + 1,
                end_line=(
                    source_node.end_point[0] + 1
                    if source_node.end_point[1] > 0
                    else max(1, source_node.end_point[0])
                ),
                source_hash=hashlib.sha256(source_slice).hexdigest(),
                source=source_slice.decode("utf-8"),
            )
        )
        emitted_source_bytes += len(source_slice)
        child_scope_kind = "class" if is_class else "function"
        pending.extend(
            (child, (*scope, name), child_scope_kind)
            for child in reversed(body_node.named_children)
        )
    return SymbolExtraction(
        tuple(symbols), file_hash, truncated=truncated
    )


__all__ = [
    "MAX_PYTHON_SOURCE_BYTES",
    "MAX_SYMBOL_ENTRIES",
    "MAX_SYMBOL_SOURCE_BYTES",
    "PythonSymbol",
    "PythonSymbolExtraction",
    "PythonSymbolIssue",
    "extract_python_symbols",
]
