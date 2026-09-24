"""Small shared value contract for exact source symbols."""

from __future__ import annotations

from dataclasses import dataclass

MAX_SYMBOL_INPUT_BYTES = 1_048_576
MAX_SYMBOL_ENTRIES = 128
MAX_SYMBOL_SOURCE_BYTES = 131_072


@dataclass(frozen=True, slots=True)
class SourceSymbol:
    """One symbol with zero-based half-open byte offsets and 1-based lines."""

    path: str
    qualified_symbol: str
    kind: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    source_hash: str
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "qualified_symbol": self.qualified_symbol,
            "kind": self.kind,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "source_hash": self.source_hash,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class SymbolIssue:
    error_code: str
    message: str
    details: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SymbolExtraction:
    symbols: tuple[SourceSymbol, ...]
    file_hash: str
    truncated: bool = False
    issue: SymbolIssue | None = None


__all__ = [
    "MAX_SYMBOL_ENTRIES",
    "MAX_SYMBOL_INPUT_BYTES",
    "MAX_SYMBOL_SOURCE_BYTES",
    "SourceSymbol",
    "SymbolExtraction",
    "SymbolIssue",
]
