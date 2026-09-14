import unittest

from agent.verification import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationStatus,
    classify_verification,
)


def result(
    status: VerificationCheckStatus,
    *,
    output: str = "",
    name: str = "tests",
) -> VerificationResult:
    return VerificationResult.create(
        name=name,
        argv=("python", "-m", "unittest"),
        cwd=".",
        status=status,
        exit_code=0 if status is VerificationCheckStatus.PASS else 1,
        stdout=output,
    )


class VerificationEvaluationTests(unittest.TestCase):
    def test_classifies_four_baseline_post_combinations(self) -> None:
        passed = result(VerificationCheckStatus.PASS)
        failed_before = result(
            VerificationCheckStatus.FAIL, output="same failure"
        )
        failed_after = result(
            VerificationCheckStatus.FAIL, output="same failure"
        )

        cases = (
            ((passed,), (passed,), VerificationStatus.VERIFIED),
            ((passed,), (failed_after,), VerificationStatus.REGRESSION),
            (
                (failed_before,),
                (failed_after,),
                VerificationStatus.PRE_EXISTING_FAILURE,
            ),
            ((failed_before,), (passed,), VerificationStatus.IMPROVED),
        )
        for baseline, post, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_verification(baseline, post), expected
                )

    def test_changed_failure_has_deterministic_regression_identity(self) -> None:
        baseline = result(VerificationCheckStatus.FAIL, output="failure A")
        post = result(VerificationCheckStatus.FAIL, output="failure B")

        self.assertNotEqual(baseline.failure_id, post.failure_id)
        self.assertEqual(
            classify_verification((baseline,), (post,)),
            VerificationStatus.REGRESSION,
        )

    def test_empty_checks_are_unverified(self) -> None:
        self.assertEqual(
            classify_verification((), ()), VerificationStatus.UNVERIFIED
        )

    def test_timeout_or_execution_error_is_verification_error(self) -> None:
        passed = result(VerificationCheckStatus.PASS)
        for status in (
            VerificationCheckStatus.TIMEOUT,
            VerificationCheckStatus.EXECUTION_ERROR,
        ):
            with self.subTest(status=status):
                problem = result(status)
                self.assertEqual(
                    classify_verification((passed,), (problem,)),
                    VerificationStatus.VERIFICATION_ERROR,
                )

    def test_partial_improvement_does_not_trigger_repair(self) -> None:
        fixed_before = result(
            VerificationCheckStatus.FAIL,
            output="fixed failure",
            name="fixed",
        )
        remaining_before = result(
            VerificationCheckStatus.FAIL,
            output="remaining failure",
            name="remaining",
        )
        fixed_after = result(VerificationCheckStatus.PASS, name="fixed")
        remaining_after = result(
            VerificationCheckStatus.FAIL,
            output="remaining failure",
            name="remaining",
        )

        self.assertEqual(
            classify_verification(
                (fixed_before, remaining_before),
                (fixed_after, remaining_after),
            ),
            VerificationStatus.IMPROVED,
        )

    def test_any_new_failure_takes_priority_over_an_improvement(self) -> None:
        fixed_before = result(
            VerificationCheckStatus.FAIL,
            output="fixed failure",
            name="fixed",
        )
        regressed_before = result(
            VerificationCheckStatus.PASS,
            name="regressed",
        )
        fixed_after = result(VerificationCheckStatus.PASS, name="fixed")
        regressed_after = result(
            VerificationCheckStatus.FAIL,
            output="new failure",
            name="regressed",
        )

        self.assertEqual(
            classify_verification(
                (fixed_before, regressed_before),
                (fixed_after, regressed_after),
            ),
            VerificationStatus.REGRESSION,
        )


if __name__ == "__main__":
    unittest.main()
