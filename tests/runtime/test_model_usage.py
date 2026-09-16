import unittest
from decimal import Decimal

from langchain_core.messages import AIMessage

from agent.runtime import (
    TokenPricing,
    UsageStatus,
    capture_model_result,
    measure_usage,
)


class ModelUsageTests(unittest.TestCase):
    def test_captures_raw_usage_before_parser_without_raw_content(self) -> None:
        message = AIMessage(
            content="secret raw response",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        pricing = TokenPricing(Decimal("2"), Decimal("4"), "fixed")

        result = capture_model_result({"parsed": True}, message, pricing)

        self.assertEqual(result.value, {"parsed": True})
        self.assertEqual(result.usage.status, UsageStatus.KNOWN)
        self.assertEqual(result.usage.cost_microusd, 40)
        self.assertNotIn("secret raw response", repr(result))

    def test_partial_and_missing_usage_are_never_coerced_to_zero(self) -> None:
        partial = measure_usage(
            AIMessage.model_construct(
                content="", usage_metadata={"input_tokens": 3}
            ),
            None,
        )
        unknown = measure_usage(AIMessage(content=""), None)

        self.assertEqual(partial.status, UsageStatus.PARTIAL)
        self.assertIsNone(partial.output_tokens)
        self.assertIsNone(partial.cost_microusd)
        self.assertEqual(unknown.status, UsageStatus.UNKNOWN)
        self.assertIsNone(unknown.input_tokens)
        self.assertIsNone(unknown.cost_microusd)


if __name__ == "__main__":
    unittest.main()
