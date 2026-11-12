"""External runtime dependencies use the public call boundary, without a provider."""

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from agent.architect.runtime import durable_architect_runtime
from agent.developer.runtime import durable_developer_runtime
from agent.runtime import BudgetErrorCode, TokenPricing, UsageStatus
from tests.runtime._config_support import model_settings


@tool
def read_candidate() -> str:
    """Read the approved candidate only."""
    return "main.py"


class InjectedChat(RunnableLambda):
    def __init__(self, content="authored response"):
        super().__init__(lambda _: self.response(content))

    @staticmethod
    def response(content):
        return AIMessage(content=content, usage_metadata={
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    def bind_tools(self, tools):
        names = ",".join(tool.name for tool in tools)
        return RunnableLambda(lambda _: self.response(names))

    def with_structured_output(self, schema, *, include_raw):
        if not include_raw:
            raise AssertionError("Structured calls must retain the raw usage.")
        return RunnableLambda(lambda _: {
            "raw": self.response("authored structured response"),
            "parsed": schema(reasoning="authored", hypothesis="Read candidate"),
            "parsing_error": None,
        })


class RuntimeInjectionTests(unittest.TestCase):
    def test_both_runtimes_use_injected_model_tools_and_tree_with_raw_usage(self):
        settings = model_settings()

        def factory(received, *, max_output_tokens, request_timeout_seconds, max_retries):
            self.assertEqual(received, settings)
            self.assertEqual(max_output_tokens, 128)
            self.assertEqual(request_timeout_seconds, 7.0)
            self.assertEqual(max_retries, 0)
            return InjectedChat()

        with tempfile.TemporaryDirectory() as directory:
            common = dict(model_factory=factory, research_tools=[read_candidate],
                          load_codebase_structure=lambda: "main.py")
            pricing = TokenPricing(Decimal("2"), Decimal("4"), "authored")
            architect = durable_architect_runtime(settings, 128, pricing, **common)
            developer = durable_developer_runtime(settings, 128, pricing,
                                                   workspace_root=Path(directory), **common)
            values = {"codebase_structure": "main.py", "implementation_research_scratchpad": [],
                      "atomic_implementation_research": [], "file_content": "pass", "target_file": "main.py",
                      "additional_context": "", "development_task": "Read candidate",
                      "_model_request_timeout_seconds": 7.0}
            for runtime, call in ((architect, architect.conduct_research),
                                  (developer, developer.research_atomic_task)):
                with self.subTest(runtime=type(runtime).__name__):
                    self.assertEqual(runtime.load_codebase_structure(), "main.py")
                    result = call(values)
                    self.assertEqual(result.value.content, "read_candidate")
                    self.assertEqual(result.usage.status, UsageStatus.KNOWN)
                    self.assertEqual(result.usage.cost_microusd, 40)

    def test_explicit_empty_tool_list_stays_empty_after_caller_mutates_original(self):
        tools = []
        runtime = durable_architect_runtime(model_settings(), 128, None,
                                            research_tools=tools,
                                            model_factory=lambda *a, **k: InjectedChat())
        tools.append(read_candidate)
        result = runtime.conduct_research({"codebase_structure": "main.py",
                                          "implementation_research_scratchpad": []})
        self.assertEqual(result.value.content, "")
        self.assertEqual(result.usage.status, UsageStatus.PARTIAL)

    def test_injected_structured_and_invalid_plan_preserve_usage_before_parse(self):
        pricing = TokenPricing(Decimal("2"), Decimal("4"), "authored")
        runtime = durable_architect_runtime(
            model_settings(), 128, pricing, model_factory=lambda *a, **k: InjectedChat())
        values = {"codebase_structure": "main.py", "implementation_research_scratchpad": [],
                  "output_format": "authored schema"}
        valid = runtime.plan_next_step(values)
        self.assertEqual(valid.value.hypothesis, "Read candidate")
        self.assertEqual(valid.usage.cost_microusd, 40)
        invalid = runtime.extract_implementation_plan(values)
        self.assertEqual(invalid.error_code, BudgetErrorCode.MODEL_OUTPUT_INVALID)
        self.assertEqual(invalid.usage.cost_microusd, 40)

    def test_injected_transport_timeout_is_structured_and_usage_unknown(self):
        import httpx

        def timeout(_):
            raise httpx.ReadTimeout("authored")

        with tempfile.TemporaryDirectory() as directory:
            runtime = durable_developer_runtime(
                model_settings(), 128, None, workspace_root=directory,
                model_factory=lambda *a, **k: RunnableLambda(timeout))
            values = {"file_path": "main.py", "file_content": "pass", "task": "Read candidate",
                      "research": [], "additional_context": "", "verification_feedback": ""}
            result = runtime.propose_existing_file_edit(values)
            self.assertEqual(result.error_code, BudgetErrorCode.MODEL_REQUEST_TIMEOUT)
            self.assertEqual(result.usage.status, UsageStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
