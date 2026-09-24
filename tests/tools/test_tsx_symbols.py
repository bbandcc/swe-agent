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
from tree_sitter_languages import get_parser

from agent.tools.codemap import (
    MAX_CODE_SYMBOL_FILES,
    MAX_CODE_SYMBOL_OUTPUT_BYTES,
    get_code_definitions,
    get_code_definitions_multi,
    get_function_implementation,
)
from agent.tools.symbol_contract import (
    MAX_SYMBOL_ENTRIES,
    MAX_SYMBOL_INPUT_BYTES,
    MAX_SYMBOL_SOURCE_BYTES,
)


FIXTURES = Path(__file__).parent / "fixtures"


class _ToolState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class TSXSymbolToolTests(unittest.TestCase):
    def test_tsx_symbols_match_golden_byte_ranges_and_source_hashes(self) -> None:
        source = (FIXTURES / "tsx_symbols.tsx").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.tsx").write_bytes(source)
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke({"file_path": "sample.tsx"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["file_hash"], hashlib.sha256(source).hexdigest())
        symbols = {item["qualified_symbol"]: item for item in result["symbols"]}
        expected = {
            "Props": (
                "type_alias",
                2,
                2,
                b"type Props<T extends string> = { title: T; onClick?: () => void };",
            ),
            "load": (
                "function",
                3,
                5,
                b"export async function load<T extends { id: string }>(props: Props<T>): Promise<JSX.Element> {\n"
                b"  return <Widget title={props.title} onClick={(event: MouseEvent) => event.preventDefault()} />;\n"
                b"}",
            ),
            "Panel": (
                "class",
                6,
                10,
                b"export class Panel<T extends string> extends Base<T> {\n"
                b"  render(value: T): JSX.Element {\n"
                b"    return <><Widget value={value} /><span>{value}</span></>;\n"
                b"  }\n"
                b"}",
            ),
            "Panel.render": (
                "method",
                7,
                9,
                b"render(value: T): JSX.Element {\n"
                b"    return <><Widget value={value} /><span>{value}</span></>;\n"
                b"  }",
            ),
            "Generic": (
                "function",
                11,
                16,
                b"export const Generic = <T extends string,>(props: Props<T>) => (\n"
                b"  <>\n"
                b"    <Widget title={props.title} onClick={() => props.onClick?.()} />\n"
                b"    <Icon name=\"generic\" />\n"
                b"  </>\n"
                b");",
            ),
            "SelfClosing": (
                "function",
                17,
                17,
                'export const SelfClosing = () => <Icon label={"雪"} />;'.encode("utf-8"),
            ),
        }
        self.assertEqual(set(symbols), set(expected))
        for name, (kind, start_line, end_line, expected_source) in expected.items():
            with self.subTest(symbol=name):
                symbol = symbols[name]
                expected_start = source.index(expected_source)
                raw_slice = source[symbol["start_byte"] : symbol["end_byte"]]
                self.assertEqual(symbol["path"], "sample.tsx")
                self.assertEqual(symbol["kind"], kind)
                self.assertEqual(symbol["start_byte"], expected_start)
                self.assertEqual(symbol["end_byte"], expected_start + len(expected_source))
                self.assertEqual(symbol["start_line"], start_line)
                self.assertEqual(symbol["end_line"], end_line)
                self.assertEqual(raw_slice, expected_source)
                self.assertEqual(symbol["source"].encode("utf-8"), raw_slice)
                self.assertEqual(symbol["source_hash"], hashlib.sha256(raw_slice).hexdigest())

    def test_fixture_requires_the_independent_tsx_grammar(self) -> None:
        source = (FIXTURES / "tsx_symbols.tsx").read_bytes()
        self.assertTrue(get_parser("typescript").parse(source).root_node.has_error)
        self.assertFalse(get_parser("tsx").parse(source).root_node.has_error)

    def test_ambiguous_tsx_function_name_fails_closed(self) -> None:
        source = (
            "function render(value: number) { return <span>{value}</span>; }\n"
            "class Panel { render(value: string) { return <span>{value}</span>; } }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "duplicate.tsx").write_text(source, encoding="utf-8")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_function_implementation.invoke(
                    {"file_path": "duplicate.tsx", "function_name": "render"}
                )
        self.assertEqual(result["error_code"], "ambiguous_symbol")
        self.assertEqual(
            {item["qualified_symbol"] for item in result["candidates"]},
            {"render", "Panel.render"},
        )

    def test_tsx_parse_and_grammar_errors_are_structured(self) -> None:
        class BrokenParser:
            def parse(self, _source: bytes):
                raise ValueError("known parser error")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.tsx").write_text(
                "export const Broken = () => <div>;\n", encoding="utf-8"
            )
            (root / "valid.tsx").write_text(
                "export const Valid = () => <div />;\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                parse_error = get_code_definitions.invoke(
                    {"file_path": "broken.tsx"}
                )
                with patch(
                    "agent.tools.typescript_symbols.get_parser",
                    side_effect=KeyError("tsx grammar unavailable"),
                ):
                    grammar_error = get_code_definitions.invoke(
                        {"file_path": "valid.tsx"}
                    )
                with patch(
                    "agent.tools.typescript_symbols.get_parser",
                    return_value=BrokenParser(),
                ):
                    parser_error = get_code_definitions.invoke(
                        {"file_path": "valid.tsx"}
                    )
                with patch(
                    "agent.tools.typescript_symbols.get_parser",
                    side_effect=RuntimeError("unexpected parser bug"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "unexpected parser bug"):
                        get_code_definitions.invoke({"file_path": "valid.tsx"})
        self.assertEqual(parse_error["error_code"], "parse_error")
        self.assertEqual(grammar_error["error_code"], "tree_sitter_error")
        self.assertIn("TSX", grammar_error["message"])
        self.assertEqual(parser_error["error_code"], "tree_sitter_error")

    def test_tsx_source_entry_output_and_file_caps_are_enforced(self) -> None:
        many_components = "\n".join(
            f"export function Item_{index}() {{ return <span>{index}</span>; }}"
            for index in range(MAX_SYMBOL_ENTRIES + 2)
        )
        output_pressure = "\n".join(
            f"export function Output_{index}() {{\n"
            + "\n" * 800
            + "  return <span />;\n}"
            for index in range(MAX_SYMBOL_ENTRIES)
        )
        large_symbol = (
            "function Huge() {\n"
            + "\n" * (MAX_SYMBOL_SOURCE_BYTES + 1)
            + "return <span />;\n}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "many.tsx").write_text(many_components, encoding="utf-8")
            (root / "output.tsx").write_text(output_pressure, encoding="utf-8")
            (root / "large-symbol.tsx").write_text(large_symbol, encoding="utf-8")
            (root / "too-large.tsx").write_bytes(
                b" " * (MAX_SYMBOL_INPUT_BYTES + 1)
            )
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                entries = get_code_definitions.invoke({"file_path": "many.tsx"})
                output = get_code_definitions.invoke({"file_path": "output.tsx"})
                source_limit = get_code_definitions.invoke(
                    {"file_path": "large-symbol.tsx"}
                )
                input_limit = get_code_definitions.invoke(
                    {"file_path": "too-large.tsx"}
                )
                file_limit = get_code_definitions_multi.invoke(
                    {
                        "file_paths": [
                            f"missing_{index}.tsx"
                            for index in range(MAX_CODE_SYMBOL_FILES + 1)
                        ]
                    }
                )
        self.assertTrue(entries["ok"], entries)
        self.assertTrue(entries["truncated"])
        self.assertLessEqual(len(entries["symbols"]), MAX_SYMBOL_ENTRIES)
        self.assertTrue(output["ok"], output)
        self.assertTrue(output["truncated"])
        self.assertLess(len(output["symbols"]), MAX_SYMBOL_ENTRIES)
        self.assertLessEqual(
            len(json.dumps(output, ensure_ascii=False).encode("utf-8")),
            MAX_CODE_SYMBOL_OUTPUT_BYTES,
        )
        self.assertTrue(source_limit["ok"], source_limit)
        self.assertTrue(source_limit["truncated"])
        self.assertEqual(source_limit["symbols"], [])
        self.assertEqual(input_limit["error_code"], "source_too_large")
        self.assertEqual(file_limit["error_code"], "file_limit_exceeded")

    def test_compiled_toolnode_returns_tsx_symbol_evidence(self) -> None:
        source = (FIXTURES / "tsx_symbols.tsx").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.tsx").write_bytes(source)
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
                                        "args": {"file_path": "sample.tsx"},
                                        "id": "tsx-symbols",
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
            "Panel.render",
            {item["qualified_symbol"] for item in payload["symbols"]},
        )


if __name__ == "__main__":
    unittest.main()
