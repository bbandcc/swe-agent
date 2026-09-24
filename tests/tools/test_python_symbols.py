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
    MAX_CODE_SYMBOL_OUTPUT_BYTES,
    get_code_definitions,
    get_code_definitions_multi,
    get_function_implementation,
)


class _ToolState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class PythonSymbolToolTests(unittest.TestCase):
    def test_definitions_expose_exact_tree_sitter_byte_slices(self) -> None:
        source = (
            "# 前缀：雪\n"
            "@mark('装饰')\n"
            "class Outer:\n"
            "    @staticmethod\n"
            "    async def run(\n"
            "        value: str,\n"
            "    ) -> str:\n"
            "        def nested():\n"
            "            return value\n"
            "        return nested()\n"
            "\n"
            "    class Inner:\n"
            "        def run(self):\n"
            "            return 'nested class'\n"
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_bytes(source)
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke({"file_path": "sample.py"})

        self.assertTrue(result["ok"], result)
        symbols = result["symbols"]
        by_name = {symbol["qualified_symbol"]: symbol for symbol in symbols}
        self.assertIn("Outer.run", by_name)
        self.assertIn("Outer.run.nested", by_name)
        self.assertIn("Outer.Inner", by_name)
        self.assertIn("Outer.Inner.run", by_name)

        decorated = by_name["Outer.run"]
        self.assertEqual(decorated["path"], "sample.py")
        self.assertEqual(decorated["kind"], "method")
        self.assertEqual(decorated["start_line"], 4)
        self.assertEqual(decorated["end_line"], 10)
        self.assertEqual(
            decorated["start_byte"], source.index(b"@staticmethod")
        )
        self.assertNotEqual(
            decorated["start_byte"],
            source.decode("utf-8").index("@staticmethod"),
        )
        self.assertTrue(
            decorated["source"].startswith("@staticmethod\n    async def run")
        )
        self.assertIn("value: str,\n    ) -> str:", decorated["source"])
        for symbol in symbols:
            raw_slice = source[symbol["start_byte"] : symbol["end_byte"]]
            self.assertEqual(raw_slice.decode("utf-8"), symbol["source"])
            self.assertEqual(
                hashlib.sha256(raw_slice).hexdigest(), symbol["source_hash"]
            )
        self.assertEqual(by_name["Outer"]["start_line"], 2)

    def test_same_name_candidates_return_ambiguity_instead_of_first_match(self) -> None:
        source = (
            "def run():\n    return 'module'\n\n"
            "class Worker:\n    def run(self):\n        return 'method'\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(source, encoding="utf-8", newline="")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_function_implementation.invoke(
                    {"file_path": "app.py", "function_name": "run"}
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ambiguous_symbol")
        self.assertEqual(
            {candidate["qualified_symbol"] for candidate in result["candidates"]},
            {"run", "Worker.run"},
        )

    def test_function_implementation_accepts_unique_qualified_name(self) -> None:
        source = "class Worker:\n    async def run(self):\n        return '雪'\n"
        raw = source.encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_bytes(raw)
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_function_implementation.invoke(
                    {"file_path": "app.py", "function_name": "Worker.run"}
                )

        self.assertTrue(result["ok"], result)
        symbol = result["symbols"][0]
        self.assertEqual(symbol["qualified_symbol"], "Worker.run")
        self.assertEqual(
            raw[symbol["start_byte"] : symbol["end_byte"]].decode("utf-8"),
            symbol["source"],
        )

    def test_python_syntax_error_is_not_reported_as_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.py").write_text(
                "def broken(:\n    pass\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke({"file_path": "broken.py"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "parse_error")
        self.assertIn("start_byte", result["details"])

    def test_known_tree_sitter_parser_failure_is_structured(self) -> None:
        class BrokenParser:
            def parse(self, _source: bytes):
                raise ValueError("parser failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.py").write_text("answer = 42\n", encoding="utf-8")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                with patch(
                    "agent.tools.python_symbols.get_parser",
                    return_value=BrokenParser(),
                ):
                    result = get_code_definitions.invoke(
                        {"file_path": "valid.py"}
                    )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "tree_sitter_error")
        self.assertNotIn("parser failure", result["message"])

    def test_unexpected_parser_runtime_error_is_not_silently_converted(self) -> None:
        class BrokenParser:
            def parse(self, _source: bytes):
                raise RuntimeError("unexpected parser bug")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.py").write_text("answer = 42\n", encoding="utf-8")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                with patch(
                    "agent.tools.python_symbols.get_parser",
                    return_value=BrokenParser(),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "unexpected parser bug"
                    ):
                        get_code_definitions.invoke({"file_path": "valid.py"})

    def test_symbol_response_obeys_entry_and_serialized_byte_caps(self) -> None:
        source = "\n".join(
            f"def function_{index}():\n    return {index}" for index in range(300)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "many.py").write_text(source, encoding="utf-8", newline="")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions.invoke({"file_path": "many.py"})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["symbols"]), result["limits"]["max_entries"])
        encoded = json.dumps(result, ensure_ascii=False).encode()
        self.assertLessEqual(len(encoded), result["limits"]["max_output_bytes"])

    def test_multifile_symbol_output_has_a_total_byte_cap(self) -> None:
        source = "\n".join(
            f"def item_{index}():\n    return '雪{index}'" for index in range(200)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.py", "two.py", "three.py"):
                (root / name).write_text(source, encoding="utf-8", newline="")
            with patch.dict(os.environ, {"SWE_AGENT_WORKSPACE": str(root)}):
                result = get_code_definitions_multi.invoke(
                    {"file_paths": ["one.py", "two.py", "three.py"]}
                )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["symbols"]), 128)
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False).encode("utf-8")),
            MAX_CODE_SYMBOL_OUTPUT_BYTES,
        )

    def test_tool_node_preserves_structured_python_source_evidence(self) -> None:
        source = (
            "# Unicode: 雪\n"
            "def answer(\n"
            "    value: str,\n"
            "):\n"
            "    return value\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_bytes(source)
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
                                        "args": {"file_path": "app.py"},
                                        "id": "python-symbols",
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        ]
                    }
                )

        tool_messages = [
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertLessEqual(
            len(tool_messages[0].content.encode("utf-8")),
            MAX_CODE_SYMBOL_OUTPUT_BYTES,
        )
        tool_output = json.loads(tool_messages[0].content)
        symbol = tool_output["symbols"][0]
        self.assertEqual(symbol["qualified_symbol"], "answer")
        self.assertEqual(
            source[symbol["start_byte"] : symbol["end_byte"]].decode("utf-8"),
            symbol["source"],
        )
        self.assertEqual(
            hashlib.sha256(
                source[symbol["start_byte"] : symbol["end_byte"]]
            ).hexdigest(),
            symbol["source_hash"],
        )


if __name__ == "__main__":
    unittest.main()
