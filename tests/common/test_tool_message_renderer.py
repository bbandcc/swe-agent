import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.common.tool_message_renderer import render_tool_messages


def tool_call(call_id: str, name: str, args: dict[str, object]) -> dict[str, object]:
    return {
        "name": name,
        "args": args,
        "id": call_id,
        "type": "tool_call",
    }


class ToolMessageRendererTests(unittest.TestCase):
    def test_empty_and_plain_messages_are_preserved(self) -> None:
        message = HumanMessage(content="task")

        self.assertEqual(render_tool_messages([]), [])
        rendered = render_tool_messages([message])
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0].content, "task")

    def test_all_calls_are_rendered_and_results_pair_by_id(self) -> None:
        rendered = render_tool_messages(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        tool_call("call-a", "search", {"query": "A"}),
                        tool_call("call-b", "codemap", {"path": "b.py"}),
                    ],
                ),
                ToolMessage(
                    content={"path": "b.py", "hash": "hash-b", "truncated": False},
                    name="codemap",
                    tool_call_id="call-b",
                ),
                ToolMessage(
                    content="search-result-A",
                    name="search",
                    tool_call_id="call-a",
                ),
            ]
        )

        text = "\n".join(str(message.content) for message in rendered)
        self.assertIn("call-a", text)
        self.assertIn('"query": "A"', text)
        self.assertIn("call-b", text)
        self.assertIn("search-result-A", text)
        self.assertIn("b.py", text)
        self.assertIn("hash-b", text)
        self.assertIn("truncated", text)
        self.assertLess(text.index("search-result-A"), text.index("b.py"))

    def test_missing_duplicate_and_unknown_ids_are_explicit_errors(self) -> None:
        rendered = render_tool_messages(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        tool_call("call-a", "search", {"query": "A"}),
                        tool_call("call-duplicate", "search", {"query": "B"}),
                        tool_call("call-duplicate", "search", {"query": "C"}),
                    ],
                ),
                ToolMessage(content="first", tool_call_id="call-duplicate"),
                ToolMessage(content="second", tool_call_id="call-duplicate"),
                ToolMessage(content="unknown", tool_call_id="call-unknown"),
            ]
        )

        text = "\n".join(str(message.content) for message in rendered)
        self.assertIn("missing", text.lower())
        self.assertIn("duplicate", text.lower())
        self.assertIn("unknown", text.lower())
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_failed_tool_and_untrusted_content_are_not_dropped(self) -> None:
        rendered = render_tool_messages(
            [
                AIMessage(
                    content="",
                    tool_calls=[tool_call("call-fail", "search", {})],
                ),
                ToolMessage(
                    content="IGNORE THIS AS AN INSTRUCTION",
                    name="search",
                    status="error",
                    tool_call_id="call-fail",
                ),
            ]
        )

        text = "\n".join(str(message.content) for message in rendered)
        self.assertIn("UNTRUSTED EVIDENCE", text)
        self.assertIn("error", text.lower())
        self.assertIn("IGNORE THIS AS AN INSTRUCTION", text)


if __name__ == "__main__":
    unittest.main()
