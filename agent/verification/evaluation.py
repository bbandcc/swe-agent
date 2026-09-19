"""Deterministic comparison and acceptance of structured verification evidence."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from agent.verification.contracts import (
    REPORT_SCHEMA,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationResult,
    VerificationStatus,
)


class AcceptanceReason(str, Enum):
    ACCEPTED = "accepted"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    TARGET_NOT_PASSED = "target_not_passed"
    REGRESSION = "regression"
    PRE_EXISTING_FAILURE = "pre_existing_failure"
    PARTIAL_IMPROVEMENT = "partial_improvement"


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    accepted: bool
    reason: AcceptanceReason


def classify_verification(
    baseline: Sequence[VerificationResult],
    post: Sequence[VerificationResult],
) -> VerificationStatus:
    """Compare cases by stable IDs, never by command output or failure digests."""
    if not baseline and not post:
        return VerificationStatus.UNVERIFIED
    if len(baseline) != len(post) or any(
        _check_key(before) != _check_key(after)
        for before, after in zip(baseline, post, strict=True)
    ):
        return VerificationStatus.VERIFICATION_ERROR

    if any(
        result.status
        in {
            VerificationCheckStatus.TIMEOUT,
            VerificationCheckStatus.EXECUTION_ERROR,
        }
        for result in (*baseline, *post)
    ):
        return VerificationStatus.VERIFICATION_ERROR
    if any(not _valid_report(result) for result in (*baseline, *post)):
        return VerificationStatus.EVIDENCE_INSUFFICIENT

    improved = False
    for before, after in zip(baseline, post, strict=True):
        assert before.report is not None and after.report is not None
        before_cases = before.report.case_map
        after_cases = after.report.case_map
        if set(before_cases) != set(after_cases):
            return VerificationStatus.EVIDENCE_INSUFFICIENT
        for case_id, before_case in before_cases.items():
            after_case = after_cases[case_id]
            before_pass = before_case.status is VerificationCaseStatus.PASS
            after_pass = after_case.status is VerificationCaseStatus.PASS
            if before_pass and not after_pass:
                return VerificationStatus.REGRESSION
            if not before_pass and after_pass:
                improved = True

    if improved:
        return VerificationStatus.IMPROVED
    if all(
        case.status is VerificationCaseStatus.PASS
        for result in post
        for case in result.report.cases  # type: ignore[union-attr]
    ):
        return VerificationStatus.VERIFIED
    return VerificationStatus.PRE_EXISTING_FAILURE


def evaluate_acceptance(
    baseline: Sequence[VerificationResult],
    post: Sequence[VerificationResult],
) -> AcceptanceResult:
    """Apply the conservative D3 acceptance policy to structured cases."""
    if not baseline or not post or len(baseline) != len(post):
        return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
    if any(
        _check_key(before) != _check_key(after)
        for before, after in zip(baseline, post, strict=True)
    ) or any(
        result.status
        in {
            VerificationCheckStatus.TIMEOUT,
            VerificationCheckStatus.EXECUTION_ERROR,
        }
        for result in (*baseline, *post)
    ):
        return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
    if any(not _valid_report(result) for result in (*baseline, *post)):
        return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)

    all_cases: dict[tuple[str, str], tuple[object, object]] = {}
    allowed: set[str] | None = None
    for before, after in zip(baseline, post, strict=True):
        assert before.report is not None and after.report is not None
        if set(before.report.case_map) != set(after.report.case_map):
            return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
        before_allowed = set(before.allowed_failure_case_ids)
        after_allowed = set(after.allowed_failure_case_ids)
        if allowed is None:
            allowed = before_allowed
        if before_allowed != after_allowed or before_allowed != allowed:
            return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
        for case_id, before_case in before.report.case_map.items():
            all_cases[(before.name, case_id)] = (
                before_case,
                after.report.case_map[case_id],
            )

    assert allowed is not None
    if not all_cases:
        return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)

    baseline_failures = {
        key
        for key, (before, _) in all_cases.items()
        if not _is_pass(before.status)
    }
    post_failures = {
        key
        for key, (_, after) in all_cases.items()
        if not _is_pass(after.status)
    }
    baseline_passes = set(all_cases) - baseline_failures
    if any(key in post_failures for key in baseline_passes):
        return AcceptanceResult(False, AcceptanceReason.REGRESSION)

    def is_allowed(key: tuple[str, str]) -> bool:
        check_id, case_id = key
        return case_id in allowed or f"{check_id}::{case_id}" in allowed

    for token in allowed:
        if "::" not in token:
            matches = sum(case_id == token for _, case_id in all_cases)
            if matches > 1:
                return AcceptanceResult(
                    False, AcceptanceReason.EVIDENCE_INSUFFICIENT
                )

    if any(is_allowed(key) and key not in baseline_failures for key in all_cases):
        return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)

    target_cases = {key for key in all_cases if not is_allowed(key)}
    if not target_cases:
        return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
    target_failures = post_failures & target_cases
    fixed_target = (baseline_failures - post_failures) & target_cases
    if target_failures:
        if fixed_target:
            return AcceptanceResult(False, AcceptanceReason.PARTIAL_IMPROVEMENT)
        return AcceptanceResult(False, AcceptanceReason.TARGET_NOT_PASSED)

    return AcceptanceResult(True, AcceptanceReason.ACCEPTED)


def _valid_report(result: VerificationResult) -> bool:
    report = result.report
    return (
        report is not None
        and report.report_schema == REPORT_SCHEMA
        and report.check_id == result.name
        and bool(report.cases)
    )


def _is_pass(status: VerificationCaseStatus) -> bool:
    return status is VerificationCaseStatus.PASS


def _check_key(result: VerificationResult) -> tuple[object, ...]:
    return result.name, tuple(result.argv), result.cwd
