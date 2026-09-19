import sys
import tempfile
import unittest
from pathlib import Path

from agent.verification import (
    REPORT_SCHEMA,
    AcceptanceReason,
    VerificationCase,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationReport,
    VerificationResult,
    VerificationRunner,
    VerificationSpec,
    VerificationStatus,
    classify_verification,
    evaluate_acceptance,
)


def report(
    check_id: str,
    cases: tuple[tuple[str, VerificationCaseStatus], ...],
) -> VerificationReport:
    return VerificationReport(
        check_id=check_id,
        report_schema=REPORT_SCHEMA,
        cases=tuple(
            VerificationCase(
                check_id=check_id,
                case_id=case_id,
                status=status,
                report_schema=REPORT_SCHEMA,
            )
            for case_id, status in cases
        ),
    )


def result(
    status: VerificationCheckStatus,
    evidence: VerificationReport | None,
    *,
    allowed: tuple[str, ...] = (),
    stdout: str = "",
    duration: float = 1.0,
) -> VerificationResult:
    return VerificationResult.create(
        name="unit",
        argv=("pytest", "--junitxml=report.xml"),
        cwd=".",
        status=status,
        exit_code=0 if status is VerificationCheckStatus.PASS else 1,
        stdout=stdout,
        duration_seconds=duration,
        report=evidence,
        allowed_failure_case_ids=allowed,
    )


class VerificationEvidenceTests(unittest.TestCase):
    def test_same_case_ignores_log_and_timing_noise(self) -> None:
        baseline = result(
            VerificationCheckStatus.FAIL,
            report("unit", (("test_flaky", VerificationCaseStatus.FAIL),)),
            stdout="temporary path C:/run/one",
            duration=1.0,
        )
        post = result(
            VerificationCheckStatus.FAIL,
            report("unit", (("test_flaky", VerificationCaseStatus.FAIL),)),
            stdout="temporary path C:/run/two",
            duration=9.0,
        )

        self.assertNotEqual(baseline.failure_id, post.failure_id)
        self.assertEqual(
            classify_verification((baseline,), (post,)),
            VerificationStatus.PRE_EXISTING_FAILURE,
        )

    def test_failure_set_changes_are_not_guessed_and_fixed_case_is_improvement(self) -> None:
        baseline = result(
            VerificationCheckStatus.FAIL,
            report(
                "unit",
                (
                    ("passing", VerificationCaseStatus.PASS),
                    ("legacy", VerificationCaseStatus.FAIL),
                ),
            ),
        )
        new_failure = result(
            VerificationCheckStatus.FAIL,
            report(
                "unit",
                (
                    ("passing", VerificationCaseStatus.PASS),
                    ("legacy", VerificationCaseStatus.FAIL),
                    ("new", VerificationCaseStatus.FAIL),
                ),
            ),
        )
        fixed = result(
            VerificationCheckStatus.PASS,
            report(
                "unit",
                (
                    ("passing", VerificationCaseStatus.PASS),
                    ("legacy", VerificationCaseStatus.PASS),
                ),
            ),
        )

        self.assertEqual(
            classify_verification((baseline,), (new_failure,)),
            VerificationStatus.EVIDENCE_INSUFFICIENT,
        )
        self.assertEqual(
            classify_verification((baseline,), (fixed,)),
            VerificationStatus.IMPROVED,
        )

        removed_case = result(
            VerificationCheckStatus.FAIL,
            report(
                "unit",
                (("passing", VerificationCaseStatus.PASS),),
            ),
        )
        self.assertEqual(
            classify_verification((baseline,), (removed_case,)),
            VerificationStatus.EVIDENCE_INSUFFICIENT,
        )

    def test_output_truncation_does_not_remove_structured_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xml = (
                "<testsuites><testsuite name='unit'>"
                "<testcase classname='suite' name='case'/>"
                "</testsuite></testsuites>"
            )
            code = (
                "from pathlib import Path; "
                f"Path('report.xml').write_text({xml!r}, encoding='utf-8'); "
                "print('noise' * 1000)"
            )
            observed = VerificationRunner(root).run(
                VerificationSpec(
                    name="unit",
                    argv=(sys.executable, "-c", code),
                    report_path="report.xml",
                    max_output_bytes=128,
                )
            )

        self.assertTrue(observed.stdout_truncated)
        self.assertIsNotNone(observed.report)
        assert observed.report is not None
        self.assertEqual(observed.report.cases[0].case_id, "suite::case")

    def test_missing_or_malformed_report_is_evidence_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = VerificationRunner(root).run(
                VerificationSpec(
                    name="unit",
                    argv=(sys.executable, "-c", "pass"),
                    report_path="missing.xml",
                )
            )
            malformed_code = (
                "from pathlib import Path; "
                "Path('bad.xml').write_text('<not-junit', encoding='utf-8')"
            )
            malformed = VerificationRunner(root).run(
                VerificationSpec(
                    name="unit",
                    argv=(sys.executable, "-c", malformed_code),
                    report_path="bad.xml",
                )
            )

        self.assertIsNone(missing.report)
        self.assertIsNone(malformed.report)
        self.assertEqual(
            classify_verification((missing,), (missing,)),
            VerificationStatus.EVIDENCE_INSUFFICIENT,
        )
        self.assertEqual(
            classify_verification((malformed,), (malformed,)),
            VerificationStatus.EVIDENCE_INSUFFICIENT,
        )

    def test_partial_improvement_is_not_accepted(self) -> None:
        baseline = result(
            VerificationCheckStatus.FAIL,
            report(
                "unit",
                (
                    ("fixed", VerificationCaseStatus.FAIL),
                    ("remaining", VerificationCaseStatus.FAIL),
                ),
            ),
        )
        post = result(
            VerificationCheckStatus.FAIL,
            report(
                "unit",
                (
                    ("fixed", VerificationCaseStatus.PASS),
                    ("remaining", VerificationCaseStatus.FAIL),
                ),
            ),
        )

        decision = evaluate_acceptance((baseline,), (post,))

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, AcceptanceReason.PARTIAL_IMPROVEMENT)

    def test_no_checks_and_target_failure_are_not_accepted(self) -> None:
        empty = evaluate_acceptance((), ())
        target_failure = evaluate_acceptance(
            (
                result(
                    VerificationCheckStatus.PASS,
                    report("unit", (("target", VerificationCaseStatus.PASS),)),
                ),
            ),
            (
                result(
                    VerificationCheckStatus.FAIL,
                    report("unit", (("target", VerificationCaseStatus.FAIL),)),
                ),
            ),
        )

        self.assertFalse(empty.accepted)
        self.assertEqual(empty.reason, AcceptanceReason.EVIDENCE_INSUFFICIENT)
        self.assertFalse(target_failure.accepted)
        self.assertEqual(target_failure.reason, AcceptanceReason.REGRESSION)

    def test_explicit_allowed_pre_existing_failure_can_be_accepted(self) -> None:
        baseline = result(
            VerificationCheckStatus.FAIL,
            report(
                "unit",
                (
                    ("target", VerificationCaseStatus.PASS),
                    ("legacy", VerificationCaseStatus.FAIL),
                ),
            ),
            allowed=("legacy",),
        )
        post = result(
            VerificationCheckStatus.FAIL,
            report(
                "unit",
                (
                    ("target", VerificationCaseStatus.PASS),
                    ("legacy", VerificationCaseStatus.FAIL),
                ),
            ),
            allowed=("legacy",),
        )

        decision = evaluate_acceptance((baseline,), (post,))

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, AcceptanceReason.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
