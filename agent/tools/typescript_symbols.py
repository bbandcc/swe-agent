"""Tree-sitter TypeScript symbol extraction from original source bytes."""

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

MAX_TYPESCRIPT_SOURCE_BYTES = MAX_SYMBOL_INPUT_BYTES
_SCOPED_DECLARATIONS = {
    "function_declaration",
    "class_declaration",
    "method_definition",
}
_TYPE_DECLARATION_KINDS = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
}


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


def _extract_symbols_for_grammar(
    canonical_path: str,
    source_bytes: bytes,
    *,
    grammar_name: str,
    language_name: str,
    max_entries: int = MAX_SYMBOL_ENTRIES,
    max_source_bytes: int = MAX_SYMBOL_SOURCE_BYTES,
) -> SymbolExtraction:
    """Extract bounded typed-JavaScript symbols from exact grammar byte ranges.

    Supported value declarations are functions, classes, methods, and arrows
    assigned to variables. Interface, type alias, and enum declarations are
    emitted as their own symbol kinds; type members are not runtime methods.
    """
    file_hash = hashlib.sha256(source_bytes).hexdigest()
    if len(source_bytes) > MAX_TYPESCRIPT_SOURCE_BYTES:
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "source_too_large",
                f"{language_name} symbol input exceeds the fixed source-size limit.",
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
        parser = get_parser(grammar_name)
        tree = parser.parse(source_bytes)
    except (KeyError, TypeError, ValueError):
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "tree_sitter_error",
                f"The installed Tree-sitter {language_name} parser could not process this file.",
            ),
        )
    if tree.root_node.has_error:
        return SymbolExtraction(
            (),
            file_hash,
            issue=SymbolIssue(
                "parse_error",
                f"Tree-sitter could not parse the {language_name} source.",
                _first_parse_issue(tree.root_node),
            ),
        )

    symbols: list[SourceSymbol] = []
    emitted_source_bytes = 0
    truncated = False

    def add_symbol(name: str, scope: tuple[str, ...], kind: str, source_node: Any) -> bool:
        nonlocal emitted_source_bytes, truncated
        source_slice = source_bytes[source_node.start_byte : source_node.end_byte]
        if len(symbols) >= max_entries or (
            emitted_source_bytes + len(source_slice) > max_source_bytes
        ):
            truncated = True
            return False
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
        return True

    pending = [(tree.root_node, (), "module", None)]
    while pending:
        node, scope, parent_scope_kind, export_span = pending.pop()

        if node.type == "export_statement":
            pending.extend(
                (child, scope, parent_scope_kind, node)
                for child in reversed(node.named_children)
            )
            continue

        if node.type in _SCOPED_DECLARATIONS:
            name = _name_text(source_bytes, node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            if name is None or body is None:
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
            if not add_symbol(name, scope, kind, source_node):
                break
            pending.extend(
                (child, (*scope, name), child_scope_kind, None)
                for child in reversed(body.named_children)
            )
            continue

        if node.type in _TYPE_DECLARATION_KINDS:
            name = _name_text(source_bytes, node.child_by_field_name("name"))
            if name is not None and not add_symbol(
                name,
                scope,
                _TYPE_DECLARATION_KINDS[node.type],
                export_span or node,
            ):
                break
            # Interface members and type-expression nodes are not value symbols.
            continue

        if node.type == "variable_declarator":
            name = _name_text(source_bytes, node.child_by_field_name("name"))
            value = node.child_by_field_name("value")
            if name is not None and value is not None and value.type == "arrow_function":
                if not add_symbol(
                    name,
                    scope,
                    "method" if parent_scope_kind == "class" else "function",
                    export_span or node,
                ):
                    break
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


def extract_typescript_symbols(
    canonical_path: str,
    source_bytes: bytes,
    *,
    max_entries: int = MAX_SYMBOL_ENTRIES,
    max_source_bytes: int = MAX_SYMBOL_SOURCE_BYTES,
) -> SymbolExtraction:
    """Extract symbols from `.ts` source with the independent TypeScript grammar."""
    return _extract_symbols_for_grammar(
        canonical_path,
        source_bytes,
        grammar_name="typescript",
        language_name="TypeScript",
        max_entries=max_entries,
        max_source_bytes=max_source_bytes,
    )


__all__ = ["MAX_TYPESCRIPT_SOURCE_BYTES", "extract_typescript_symbols"]
