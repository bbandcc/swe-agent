import math
import unittest
from decimal import Decimal

from agent.runtime import (
    BudgetController,
    BudgetErrorCode,
    BudgetSnapshot,
    CallKind,
    CallStatus,
    UsageRecord,
    UsageStatus,
)


class BudgetControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = BudgetController()

    def snapshot(
        self,
        *,
        max_steps: int = 3,
        max_cost_usd: Decimal | None = None,
    ) -> BudgetSnapshot:
        return BudgetSnapshot.create(
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
            deadline_at=200.0,
        )

    def test_max_steps_accepts_n_and_rejects_n_plus_one(self) -> None:
        snapshot = self.snapshot(max_steps=2)
        first = self.controller.reserve_model(
            snapshot, run_id="run", request_digest="a" * 64, now=100.0
        )
        self.assertTrue(first.allowed)
        snapshot = self.controller.settle(
            first.snapshot,
            (UsageRecord.unknown(first.reservations[0].call_id),),
        )
        second = self.controller.reserve_model(
            snapshot, run_id="run", request_digest="b" * 64, now=100.0
        )
        self.assertTrue(second.allowed)
        snapshot = self.controller.settle(
            second.snapshot,
            (UsageRecord.unknown(second.reservations[0].call_id),),
        )

        denied = self.controller.reserve_model(
            snapshot, run_id="run", request_digest="c" * 64, now=100.0
        )

        self.assertFalse(denied.allowed)
        self.assertEqual(denied.error_code, BudgetErrorCode.MAX_STEPS_EXCEEDED)
        self.assertEqual(denied.snapshot.steps_used, 2)

    def test_tool_batch_capacity_is_all_or_none(self) -> None:
        snapshot = self.snapshot(max_steps=2)

        denied = self.controller.reserve_tools(
            snapshot,
            run_id="run",
            tool_calls=(("tool-1", "a" * 64), ("tool-2", "b" * 64), ("tool-3", "c" * 64)),
            now=100.0,
        )

        self.assertFalse(denied.allowed)
        self.assertEqual(denied.snapshot, snapshot)
        self.assertEqual(denied.reservations, ())

    def test_reserve_is_durable_step_and_uncertain_recovery_does_not_roll_back(self) -> None:
        reserved = self.controller.reserve_model(
            self.snapshot(max_steps=1),
            run_id="run",
            request_digest="a" * 64,
            now=100.0,
        )

        recovered = self.controller.mark_outcome_unknown(reserved.snapshot)

        self.assertEqual(recovered.steps_used, 1)
        self.assertEqual(recovered.reservations[0].status, CallStatus.OUTCOME_UNKNOWN)
        denied = self.controller.reserve_model(
            recovered, run_id="run", request_digest="b" * 64, now=100.0
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.error_code, BudgetErrorCode.OUTCOME_UNKNOWN)

    def test_known_partial_and_unknown_usage_preserve_missing_values(self) -> None:
        known = UsageRecord(
            call_id="known",
            status=UsageStatus.KNOWN,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost_microusd=7,
            cost_source="fixed-test-pricing",
        )
        partial = UsageRecord(
            call_id="partial",
            status=UsageStatus.PARTIAL,
            input_tokens=10,
            output_tokens=None,
            total_tokens=None,
            cost_microusd=None,
            cost_source=None,
        )
        unknown = UsageRecord.unknown("unknown")

        self.assertEqual(known.cost_microusd, 7)
        self.assertIsNone(partial.output_tokens)
        self.assertIsNone(partial.cost_microusd)
        self.assertIsNone(unknown.input_tokens)
        self.assertIsNone(unknown.cost_microusd)

    def test_cost_crossing_current_call_is_recorded_and_next_call_is_denied(self) -> None:
        reserved = self.controller.reserve_model(
            self.snapshot(max_cost_usd=Decimal("0.000010")),
            run_id="run",
            request_digest="a" * 64,
            now=100.0,
        )
        settled = self.controller.settle(
            reserved.snapshot,
            (
                UsageRecord(
                    call_id=reserved.reservations[0].call_id,
                    status=UsageStatus.KNOWN,
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    cost_microusd=11,
                    cost_source="provider",
                ),
            ),
        )

        denied = self.controller.reserve_model(
            settled, run_id="run", request_digest="b" * 64, now=100.0
        )

        self.assertEqual(settled.cost_microusd, 11)
        self.assertEqual(denied.error_code, BudgetErrorCode.MAX_COST_EXCEEDED)

    def test_unknown_cost_with_cost_limit_blocks_next_call(self) -> None:
        reserved = self.controller.reserve_model(
            self.snapshot(max_cost_usd=Decimal("1")),
            run_id="run",
            request_digest="a" * 64,
            now=100.0,
        )
        settled = self.controller.settle(
            reserved.snapshot,
            (UsageRecord.unknown(reserved.reservations[0].call_id),),
        )

        denied = self.controller.reserve_model(
            settled, run_id="run", request_digest="b" * 64, now=100.0
        )

        self.assertEqual(denied.error_code, BudgetErrorCode.USAGE_UNKNOWN)

    def test_deadline_is_absolute_and_finite(self) -> None:
        with self.assertRaises(ValueError):
            BudgetSnapshot.create(max_steps=1, max_cost_usd=None, deadline_at=math.inf)
        snapshot = self.snapshot()

        denied = self.controller.reserve_model(
            snapshot, run_id="run", request_digest="a" * 64, now=200.0
        )

        self.assertEqual(denied.error_code, BudgetErrorCode.DEADLINE_EXCEEDED)


if __name__ == "__main__":
    unittest.main()
