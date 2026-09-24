"""Tree-sitter TSX symbol extraction using the dedicated TSX grammar."""

from __future__ import annotations

from agent.tools.symbol_contract import (
    MAX_SYMBOL_ENTRIES,
    MAX_SYMBOL_SOURCE_BYTES,
    SymbolExtraction,
)
from agent.tools.typescript_symbols import _extract_symbols_for_grammar


def extract_tsx_symbols(
    canonical_path: str,
    source_bytes: bytes,
    *,
    max_entries: int = MAX_SYMBOL_ENTRIES,
    max_source_bytes: int = MAX_SYMBOL_SOURCE_BYTES,
) -> SymbolExtraction:
    """Extract bounded TSX symbols using the independent `tsx` grammar."""
    return _extract_symbols_for_grammar(
        canonical_path,
        source_bytes,
        grammar_name="tsx",
        language_name="TSX",
        max_entries=max_entries,
        max_source_bytes=max_source_bytes,
    )


__all__ = ["extract_tsx_symbols"]
