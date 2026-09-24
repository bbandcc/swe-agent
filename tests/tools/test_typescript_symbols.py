import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Annotated, TypedDict
from unittest.mock import patch

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph, add_messages
from langgraph.prebuilt import ToolNode

from agent.tools.codemap import (
    MAX_CODE_SYMBOL_FILES,
    MAX_CODE_SYMBOL_OUTPUT_BYTES,
    get_code_definitions,
    get_code_definitions_multi,
    get_function_implementation,
)
from agent.tools.symbol_contract import MAX_SYMBOL_ENTRIES, MAX_SYMBOL_SOURCE_BYTES


FIXTURES = Path(__file__).parent / "fixtures"


class _ToolState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class TypeScriptSymbolToolTests(unittest.TestCase):
    def _invoke(self, fixture: str = "typescript_symbols.ts"):
        source = (FIXTURES / fixture).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.ts").write_bytes(source)
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke({"file_path": "sample.ts"})
        return result, source

    def test_typescript_symbols_match_golden_byte_ranges_and_source_hashes(self) -> None:
        result, source = self._invoke()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["file_hash"], hashlib.sha256(source).hexdigest())
        symbols = {item["qualified_symbol"]: item for item in result["symbols"]}
        expected = {
            "Result": (
                "interface",
                2,
                2,
                b"export interface Result<T> { value: T; }",
            ),
            "Callback": (
                "type_alias",
                3,
                3,
                b"export type Callback<T> = (value: T) => T;",
            ),
            "Mode": ("enum", 4, 4, b"export enum Mode { Read, Write }"),
            "outer": (
                "function",
                5,
                12,
                b"export async function outer<T extends object>(value: T): Promise<T> {\n"
                b"  function nested<U extends T>(item: U): U { return item; }\n"
                b"  const arrow = <U extends T>(item: U): U => item;\n"
                b"  class Box<V> {\n"
                b"    async method<W extends V>(item: W): Promise<W> { return item; }\n"
                b"  }\n"
                b"  return value;\n"
                b"}",
            ),
            "outer.nested": (
                "function",
                6,
                6,
                b"function nested<U extends T>(item: U): U { return item; }",
            ),
            "outer.arrow": (
                "function",
                7,
                7,
                b"arrow = <U extends T>(item: U): U => item",
            ),
            "outer.Box": (
                "class",
                8,
                10,
                b"class Box<V> {\n"
                b"    async method<W extends V>(item: W): Promise<W> { return item; }\n"
                b"  }",
            ),
            "outer.Box.method": (
                "method",
                9,
                9,
                b"async method<W extends V>(item: W): Promise<W> { return item; }",
            ),
            "Worker": (
                "class",
                13,
                13,
                b"export class Worker<T> { method(value: T): T { return value; } }",
            ),
            "Worker.method": (
                "method",
                13,
                13,
                b"method(value: T): T { return value; }",
            ),
        }
        self.assertEqual(set(symbols), set(expected))
        for name, (kind, start_line, end_line, expected_source) in expected.items():
            with self.subTest(symbol=name):
                symbol = symbols[name]
                expected_start = source.index(expected_source)
                self.assertEqual(symbol["path"], "sample.ts")
                self.assertEqual(symbol["kind"], kind)
                self.assertEqual(symbol["start_byte"], expected_start)
                self.assertEqual(
                    symbol["end_byte"], expected_start + len(expected_source)
                )
                self.assertEqual(symbol["start_line"], start_line)
                self.assertEqual(symbol["end_line"], end_line)
                raw_slice = source[symbol["start_byte"] : symbol["end_byte"]]
                self.assertEqual(raw_slice, expected_source)
                self.assertEqual(symbol["source"].encode("utf-8"), raw_slice)
                self.assertEqual(
                    symbol["source_hash"], hashlib.sha256(raw_slice).hexdigest()
                )

    def test_same_name_typescript_functions_fail_closed_as_ambiguous(self) -> None:
        source = (
            "function map<T>(value: T): T { return value; }\n"
            "class Mapper<T> { map<U>(value: U): U { return value; } }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "map.ts").write_bytes(source.encode("utf-8"))
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_function_implementation.invoke(
                    {"file_path": "map.ts", "function_name": "map"}
                )
        self.assertEqual(result["error_code"], "ambiguous_symbol")
        self.assertEqual(
            {item["qualified_symbol"] for item in result["candidates"]},
            {"map", "Mapper.map"},
        )

    def test_typescript_parse_and_grammar_errors_are_structured(self) -> None:
        class BrokenParser:
            def parse(self, _source: bytes):
                raise ValueError("known parser issue")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.ts").write_text(
                "export function broken<T( value: T): T {}\n", encoding="utf-8"
            )
            (root / "valid.ts").write_text("export function answer(): number;\n")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                parse_error = get_code_definitions.invoke(
                    {"file_path": "broken.ts"}
                )
                with patch(
                    "agent.tools.typescript_symbols.get_parser",
                    side_effect=KeyError("typescript grammar unavailable"),
                ):
                    grammar_error = get_code_definitions.invoke(
                        {"file_path": "valid.ts"}
                    )
                with patch(
                    "agent.tools.typescript_symbols.get_parser",
                    return_value=BrokenParser(),
                ):
                    parser_error = get_code_definitions.invoke(
                        {"file_path": "valid.ts"}
                    )
                with patch(
                    "agent.tools.typescript_symbols.get_parser",
                    side_effect=RuntimeError("unexpected grammar bug"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "unexpected grammar bug"):
                        get_code_definitions.invoke({"file_path": "valid.ts"})
        self.assertEqual(parse_error["error_code"], "parse_error")
        self.assertIn("start_byte", parse_error["details"])
        self.assertEqual(grammar_error["error_code"], "tree_sitter_error")
        self.assertEqual(parser_error["error_code"], "tree_sitter_error")

    def test_typescript_adapter_respects_entry_source_output_and_file_caps(self) -> None:
        many_definitions = "\n".join(
            f"function item_{index}(): number {{ return {index}; }}"
            for index in range(MAX_SYMBOL_ENTRIES + 20)
        )
        output_sized_definitions = "\n".join(
            f"function output_{index}(): string {{ return '{'x' * 850}'; }}"
            for index in range(MAX_SYMBOL_ENTRIES)
        )
        too_large_symbol = (
            "function huge(): string { return '"
            + "x" * (MAX_SYMBOL_SOURCE_BYTES + 1)
            + "'; }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "many.ts").write_text(many_definitions, encoding="utf-8")
            (root / "output.ts").write_text(
                output_sized_definitions, encoding="utf-8"
            )
            (root / "large.ts").write_text(too_large_symbol, encoding="utf-8")
            (root / "one.ts").write_text("function one(): number { return 1; }\n")
            (root / "two.ts").write_text("const two = (): number => 2;\n")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                entry_bounded = get_code_definitions.invoke(
                    {"file_path": "many.ts"}
                )
                output_bounded = get_code_definitions.invoke(
                    {"file_path": "output.ts"}
                )
                source_bounded = get_code_definitions.invoke(
                    {"file_path": "large.ts"}
                )
                mixed = get_code_definitions_multi.invoke(
                    {"file_paths": ["one.ts", "two.ts"]}
                )
                file_limit = get_code_definitions_multi.invoke(
                    {
                        "file_paths": [
                            f"unused_{index}.ts"
                            for index in range(MAX_CODE_SYMBOL_FILES + 1)
                        ]
                    }
                )
        self.assertTrue(entry_bounded["ok"], entry_bounded)
        self.assertTrue(entry_bounded["truncated"])
        self.assertLessEqual(len(entry_bounded["symbols"]), MAX_SYMBOL_ENTRIES)
        self.assertTrue(output_bounded["ok"], output_bounded)
        self.assertTrue(output_bounded["truncated"])
        self.assertLess(len(output_bounded["symbols"]), MAX_SYMBOL_ENTRIES)
        self.assertLessEqual(
            len(json.dumps(output_bounded, ensure_ascii=False).encode("utf-8")),
            MAX_CODE_SYMBOL_OUTPUT_BYTES,
        )
        self.assertTrue(source_bounded["ok"], source_bounded)
        self.assertTrue(source_bounded["truncated"])
        self.assertEqual(source_bounded["symbols"], [])
        self.assertTrue(mixed["ok"], mixed)
        self.assertEqual(
            {symbol["qualified_symbol"] for symbol in mixed["symbols"]},
            {"one", "two"},
        )
        self.assertEqual(file_limit["error_code"], "file_limit_exceeded")

    def test_compiled_toolnode_returns_typescript_symbol_evidence(self) -> None:
        source = (FIXTURES / "typescript_symbols.ts").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.ts").write_bytes(source)
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                graph = StateGraph(_ToolState)
                graph.add_node("tools", ToolNode([get_code_definitions]))
                graph.add_edge(START, "tools")
                graph.add_edge("tools", END)
                result = graph.compile().invoke(
                    {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "get_code_definitions",
                                        "args": {"file_path": "sample.ts"},
                                        "id": "typescript-symbols",
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        ]
                    }
                )
        tool_messages = [
            item for item in result["messages"] if isinstance(item, ToolMessage)
        ]
        self.assertEqual(len(tool_messages), 1)
        payload = json.loads(tool_messages[0].content)
        self.assertTrue(payload["ok"], payload)
        self.assertIn(
            "Result",
            {item["qualified_symbol"] for item in payload["symbols"]},
        )


if __name__ == "__main__":
    unittest.main()
