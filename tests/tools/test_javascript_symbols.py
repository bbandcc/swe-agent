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
    get_code_definitions_multi,
    get_code_definitions,
    get_function_implementation,
)
from agent.tools.symbol_contract import MAX_SYMBOL_ENTRIES, MAX_SYMBOL_SOURCE_BYTES


FIXTURES = Path(__file__).parent / "fixtures"


class _ToolState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class JavaScriptSymbolToolTests(unittest.TestCase):
    def _invoke(self, suffix: str, fixture: str) -> tuple[dict[str, object], bytes]:
        source = (FIXTURES / fixture).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / f"sample{suffix}").write_bytes(source)
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke(
                    {"file_path": f"sample{suffix}"}
                )
        return result, source

    def test_javascript_symbols_match_golden_byte_ranges_and_source_hashes(self) -> None:
        result, source = self._invoke(".js", "javascript_symbols.js")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["file_hash"], hashlib.sha256(source).hexdigest())
        symbols = {item["qualified_symbol"]: item for item in result["symbols"]}
        expected = {
            "outer": (
                "function",
                2,
                17,
                b"export async function outer(value) {\n"
                b"  function nested() {\n"
                b"    return value;\n"
                b"  }\n"
                b"  const arrow = async (item) => {\n"
                b"    return item;\n"
                b"  };\n"
                b"  class Inner {\n"
                b"    method(\n"
                b"      item,\n"
                b"    ) {\n"
                b"      return item;\n"
                b"    }\n"
                b"  }\n"
                b"  return arrow(value);\n"
                b"}",
            ),
            "outer.nested": (
                "function",
                3,
                5,
                b"function nested() {\n    return value;\n  }",
            ),
            "outer.arrow": (
                "function",
                6,
                8,
                b"arrow = async (item) => {\n    return item;\n  }",
            ),
            "outer.Inner": (
                "class",
                9,
                15,
                b"class Inner {\n    method(\n      item,\n    ) {\n      return item;\n    }\n  }",
            ),
            "outer.Inner.method": (
                "method",
                10,
                14,
                b"method(\n      item,\n    ) {\n      return item;\n    }",
            ),
            "Worker": (
                "class",
                19,
                23,
                b"export class Worker {\n"
                b"  async run(value) {\n"
                b"    return value;\n"
                b"  }\n"
                b"}",
            ),
            "Worker.run": (
                "method",
                20,
                22,
                b"async run(value) {\n    return value;\n  }",
            ),
            "exportedArrow": (
                "function",
                25,
                25,
                b"export const exportedArrow = (value) => value;",
            ),
        }
        self.assertEqual(set(symbols), set(expected))
        for qualified_name, (kind, start_line, end_line, expected_source) in expected.items():
            with self.subTest(symbol=qualified_name):
                symbol = symbols[qualified_name]
                expected_start = source.index(expected_source)
                self.assertEqual(symbol["path"], "sample.js")
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

    def test_jsx_grammar_symbols_preserve_exact_original_slices(self) -> None:
        result, source = self._invoke(".jsx", "javascript_symbols.jsx")
        self.assertTrue(result["ok"], result)
        symbols = {item["qualified_symbol"]: item for item in result["symbols"]}
        self.assertEqual(set(symbols), {"Panel", "Item", "FragmentView"})
        self.assertEqual(result["file_hash"], hashlib.sha256(source).hexdigest())
        expected = {
            "Panel": (
                b"export function Panel({ label }) {\n"
                b"  return <section title={label}><strong>{label}</strong></section>;\n"
                b"}",
                2,
                4,
            ),
            "Item": (
                b"export const Item = (props) => <Thing value={props.value} />;",
                5,
                5,
            ),
            "FragmentView": (
                b"FragmentView = () => <>text <Widget /></>",
                6,
                6,
            ),
        }
        for name, (expected_source, start_line, end_line) in expected.items():
            symbol = symbols[name]
            expected_start = source.index(expected_source)
            self.assertEqual(symbol["path"], "sample.jsx")
            self.assertEqual(symbol["start_byte"], expected_start)
            self.assertEqual(symbol["end_byte"], expected_start + len(expected_source))
            self.assertEqual(symbol["start_line"], start_line)
            self.assertEqual(symbol["end_line"], end_line)
            raw_slice = source[symbol["start_byte"] : symbol["end_byte"]]
            self.assertEqual(raw_slice, expected_source)
            self.assertEqual(symbol["source"].encode("utf-8"), raw_slice)
            self.assertEqual(
                symbol["source_hash"], hashlib.sha256(raw_slice).hexdigest()
            )
        self.assertIn("<section title={label}>", symbols["Panel"]["source"])
        self.assertIn("<Widget />", symbols["FragmentView"]["source"])

    def test_javascript_adapter_keeps_entry_source_output_and_file_caps(self) -> None:
        many_definitions = "\n".join(
            f"function item_{index}() {{ return {index}; }}"
            for index in range(MAX_SYMBOL_ENTRIES + 20)
        )
        output_sized_definitions = "\n".join(
            f"function output_{index}() {{ return '{'x' * 850}'; }}"
            for index in range(MAX_SYMBOL_ENTRIES)
        )
        too_large_symbol = (
            "function huge() { return '"
            + "x" * (MAX_SYMBOL_SOURCE_BYTES + 1)
            + "'; }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "many.js").write_text(many_definitions, encoding="utf-8")
            (root / "output.js").write_text(
                output_sized_definitions, encoding="utf-8"
            )
            (root / "large.jsx").write_text(too_large_symbol, encoding="utf-8")
            (root / "one.js").write_text("function one() {}\n", encoding="utf-8")
            (root / "two.jsx").write_text("const two = () => 2;\n", encoding="utf-8")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                bounded = get_code_definitions.invoke({"file_path": "many.js"})
                output_bounded = get_code_definitions.invoke(
                    {"file_path": "output.js"}
                )
                source_bounded = get_code_definitions.invoke(
                    {"file_path": "large.jsx"}
                )
                mixed = get_code_definitions_multi.invoke(
                    {"file_paths": ["one.js", "two.jsx"]}
                )
                file_limit = get_code_definitions_multi.invoke(
                    {
                        "file_paths": [
                            f"unused_{index}.js"
                            for index in range(MAX_CODE_SYMBOL_FILES + 1)
                        ]
                    }
                )

        self.assertTrue(bounded["ok"], bounded)
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(len(bounded["symbols"]), MAX_SYMBOL_ENTRIES)
        self.assertLessEqual(
            len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")),
            MAX_CODE_SYMBOL_OUTPUT_BYTES,
        )
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
        self.assertEqual(
            {symbol["path"] for symbol in mixed["symbols"]},
            {"one.js", "two.jsx"},
        )
        self.assertEqual(file_limit["error_code"], "file_limit_exceeded")

    def test_javascript_same_name_candidates_are_ambiguous(self) -> None:
        source = (
            "function run() { return 'module'; }\n"
            "class Worker { run() { return 'method'; } }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.js").write_bytes(source.encode("utf-8"))
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                ambiguous = get_function_implementation.invoke(
                    {"file_path": "app.js", "function_name": "run"}
                )
                qualified = get_function_implementation.invoke(
                    {"file_path": "app.js", "function_name": "Worker.run"}
                )

        self.assertEqual(ambiguous["error_code"], "ambiguous_symbol")
        self.assertEqual(
            {item["qualified_symbol"] for item in ambiguous["candidates"]},
            {"run", "Worker.run"},
        )
        self.assertTrue(qualified["ok"], qualified)
        self.assertEqual(qualified["symbols"][0]["qualified_symbol"], "Worker.run")

    def test_tsx_remains_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.tsx").write_text("const View = () => <div />;\n")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke({"file_path": "sample.tsx"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "unsupported_language")
        self.assertIn("search_keyword_in_directory", result["message"])

    def test_javascript_syntax_errors_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.js").write_text("function broken( {\n")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke({"file_path": "broken.js"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "parse_error")
        self.assertIn("start_byte", result["details"])

    def test_known_parser_failure_is_structured_and_unexpected_error_propagates(self) -> None:
        class BrokenParser:
            def __init__(self, error: Exception):
                self.error = error

            def parse(self, _source: bytes):
                raise self.error

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.js").write_text("function answer() {}\n")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                with patch(
                    "agent.tools.javascript_symbols.get_parser",
                    side_effect=KeyError("javascript grammar unavailable"),
                ):
                    missing_grammar = get_code_definitions.invoke(
                        {"file_path": "valid.js"}
                    )
                with patch(
                    "agent.tools.javascript_symbols.get_parser",
                    return_value=BrokenParser(ValueError("known parser issue")),
                ):
                    known = get_code_definitions.invoke({"file_path": "valid.js"})
                with patch(
                    "agent.tools.javascript_symbols.get_parser",
                    return_value=BrokenParser(RuntimeError("unexpected parser bug")),
                ):
                    with self.assertRaisesRegex(RuntimeError, "unexpected parser bug"):
                        get_code_definitions.invoke({"file_path": "valid.js"})
        self.assertEqual(missing_grammar["error_code"], "tree_sitter_error")
        self.assertEqual(known["error_code"], "tree_sitter_error")
        self.assertNotIn("known parser issue", known["message"])

    def test_compiled_toolnode_returns_javascript_symbol_evidence(self) -> None:
        source = (FIXTURES / "javascript_symbols.jsx").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "view.jsx").write_bytes(source)
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
                                        "args": {"file_path": "view.jsx"},
                                        "id": "javascript-symbols",
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
        self.assertLessEqual(
            len(tool_messages[0].content.encode("utf-8")),
            MAX_CODE_SYMBOL_OUTPUT_BYTES,
        )
        payload = json.loads(tool_messages[0].content)
        self.assertTrue(payload["ok"], payload)
        self.assertIn("Panel", {item["qualified_symbol"] for item in payload["symbols"]})


if __name__ == "__main__":
    unittest.main()
