"""Real DeepSeek SDK serialization against an in-memory HTTP provider fixture."""

from decimal import Decimal
import json
import tempfile
from pathlib import Path
import unittest

import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek
from pydantic import SecretStr

from agent.architect.runtime import durable_architect_runtime
from agent.architect.models import ResearchStep
from agent.common.tool_message_renderer import render_tool_messages
from agent.config import ModelSettings
from agent.developer.runtime import durable_developer_runtime
from agent.runtime import BudgetErrorCode, TokenPricing, UsageStatus
from agent.evaluation.pilot_spend import CappedDeepSeekTransport


@tool
def read_source(path: str) -> str:
    """Read an allowed source file from the configured workspace."""
    return "print('fixture')" if path == "main.py" else "REFUSED"


class PilotModelProtocolTests(unittest.TestCase):
    def test_real_sdk_structured_request_passes_the_final_transport_gate(self):
        requests = []

        def provider(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion",
                "created": 1, "model": "deepseek-flash", "choices": [{"index": 0,
                "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "step", "type": "function", "function": {
                    "name": "ResearchStep", "arguments": '{"reasoning":"Read file","hypothesis":"Check sum"}'}}]}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})

        with tempfile.TemporaryDirectory() as directory:
            cap = CappedDeepSeekTransport(httpx.MockTransport(provider), Path(directory) / "spend.jsonl")
            with httpx.Client(transport=cap) as client:
                model = ChatDeepSeek(model="deepseek-flash", base_url="https://api.deepseek.com",
                    api_key="local-fixture-key", max_tokens=2048, max_retries=0,
                    extra_body={"thinking": {"type": "disabled"}}, http_client=client)
                result = model.with_structured_output(ResearchStep, include_raw=True).invoke("Read main.py")
            self.assertEqual(result["parsed"].hypothesis, "Check sum")
            self.assertEqual(requests[0]["tool_choice"], "required")
            self.assertEqual(cap.summary()["reserved_microcny"], 114688)

    def fixture(self, messages, *, status=200):
        requests = []
        replies = iter(messages)

        def provider(request):
            requests.append(json.loads(request.content))
            message = next(replies)
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "fixture failure"}})
            return httpx.Response(200, json={
                "id": "fixture-response", "object": "chat.completion", "created": 1,
                "model": "deepseek-flash", "choices": [{"index": 0,
                    "message": {"role": "assistant", **message},
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            })

        client = httpx.Client(transport=httpx.MockTransport(provider))
        self.addCleanup(client.close)
        settings = ModelSettings("deepseek", "deepseek-flash", "https://api.deepseek.com",
                                 SecretStr("local-fixture-key"))

        def factory(settings, *, max_output_tokens, request_timeout_seconds, max_retries):
            return ChatDeepSeek(model=settings.model, base_url=settings.base_url,
                api_key=settings.api_key, max_tokens=max_output_tokens,
                timeout=request_timeout_seconds, max_retries=max_retries,
                extra_body={"thinking": {"type": "disabled"}}, http_client=client)

        pricing = TokenPricing(Decimal("0.3"), Decimal("1.2"), "fixed-fixture-price")
        return settings, pricing, factory, requests

    def test_sdk_structured_parse_failure_keeps_the_returned_usage(self):
        message = {"content": None, "tool_calls": [{"id": "step", "type": "function",
            "function": {"name": "ResearchStep", "arguments": "{}"}}]}
        settings, pricing, factory, requests = self.fixture([message])
        runtime = durable_architect_runtime(settings, 256, pricing,
            model_factory=factory, research_tools=[read_source],
            load_codebase_structure=lambda: "main.py")
        result = runtime.plan_next_step({"codebase_structure": "main.py",
                                        "implementation_research_scratchpad": [HumanMessage("Fix sum")]})
        self.assertEqual(result.error_code, BudgetErrorCode.MODEL_OUTPUT_INVALID)
        self.assertEqual(result.usage.status, UsageStatus.KNOWN)
        self.assertEqual(result.usage.total_tokens, 20)
        self.assertEqual(requests[0]["max_tokens"], 256)
        self.assertEqual(requests[0]["thinking"], {"type": "disabled"})

    def test_sdk_research_tool_exchange_uses_the_injected_schema_and_paired_evidence(self):
        call = {"content": None, "tool_calls": [{"id": "read-1", "type": "function",
            "function": {"name": "read_source", "arguments": '{"path":"main.py"}'}}]}
        settings, pricing, factory, requests = self.fixture([call, {"content": "Read complete"}])
        runtime = durable_architect_runtime(settings, 256, pricing,
            model_factory=factory, research_tools=[read_source],
            load_codebase_structure=lambda: "main.py")
        initial = HumanMessage("Read main.py")
        first = runtime.conduct_research({"codebase_structure": "main.py",
                                         "implementation_research_scratchpad": [initial]})
        actual_call = first.value.tool_calls[0]
        reply = ToolMessage(read_source.invoke(actual_call["args"]), tool_call_id=actual_call["id"])
        second = runtime.conduct_research({"codebase_structure": "main.py",
            "implementation_research_scratchpad": render_tool_messages([initial, first.value, reply])})
        self.assertEqual(second.value.content, "Read complete")
        self.assertEqual([t["function"]["name"] for t in requests[0]["tools"]], ["read_source"])
        evidence = json.dumps(requests[1]["messages"])
        self.assertIn("read-1", evidence)
        self.assertIn("UNTRUSTED EVIDENCE", evidence)
        self.assertIn("print('fixture')", evidence)

    def test_sdk_rate_limit_has_no_hidden_retry_and_usage_stays_unknown(self):
        settings, pricing, factory, requests = self.fixture([{}], status=429)
        with tempfile.TemporaryDirectory() as directory:
            runtime = durable_developer_runtime(settings, 256, pricing,
                workspace_root=Path(directory), model_factory=factory, research_tools=[])
            result = runtime.propose_new_file({"task": "Create sum", "file_path": "main.py",
                                              "additional_context": "", "verification_feedback": ""})
        self.assertEqual(len(requests), 1)
        self.assertEqual(result.error_code, BudgetErrorCode.MODEL_TRANSPORT_ERROR)
        self.assertEqual(result.usage.status, UsageStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
