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
    has_pytest_junitxml_option,
    is_pytest_command,
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

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ValueError("Acceptance accepted must be a boolean.")
        if not isinstance(self.reason, AcceptanceReason):
            try:
                object.__setattr__(self, "reason", AcceptanceReason(self.reason))
            except (TypeError, ValueError) as error:
                raise ValueError("Acceptance reason is invalid.") from error


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
    allowed_by_check: dict[str, set[str]] = {}
    for before, after in zip(baseline, post, strict=True):
        assert before.report is not None and after.report is not None
        if set(before.report.case_map) != set(after.report.case_map):
            return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
        before_allowed = set(before.allowed_failure_case_ids)
        after_allowed = set(after.allowed_failure_case_ids)
        if before_allowed != after_allowed:
            return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
        allowed_by_check[before.name] = before_allowed
        valid_allowed_tokens = set(before.report.case_map)
        valid_allowed_tokens.update(
            f"{before.name}::{case_id}" for case_id in before.report.case_map
        )
        if not before_allowed.issubset(valid_allowed_tokens):
            return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)
        for case_id, before_case in before.report.case_map.items():
            all_cases[(before.name, case_id)] = (
                before_case,
                after.report.case_map[case_id],
            )

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
        allowed = allowed_by_check[check_id]
        return case_id in allowed or f"{check_id}::{case_id}" in allowed

    for key, (before, after) in all_cases.items():
        if is_allowed(key) and (
            before.status is not VerificationCaseStatus.FAIL
            or after.status
            in {VerificationCaseStatus.ERROR, VerificationCaseStatus.SKIPPED}
        ):
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
    if not (
        report is not None
        and report.report_schema == REPORT_SCHEMA
        and report.check_id == result.name
        and bool(report.cases)
        and has_pytest_junitxml_option(result.argv)
    ):
        return False
    statuses = {case.status for case in report.cases}
    if is_pytest_command(result.argv) and result.exit_code not in {0, 1}:
        return False
    if result.status is VerificationCheckStatus.PASS:
        return result.exit_code == 0 and statuses == {VerificationCaseStatus.PASS}
    if result.status is VerificationCheckStatus.FAIL:
        return result.exit_code not in {None, 0} and any(
            status is not VerificationCaseStatus.PASS for status in statuses
        )
    return False


def _is_pass(status: VerificationCaseStatus) -> bool:
    return status is VerificationCaseStatus.PASS


def _check_key(result: VerificationResult) -> tuple[object, ...]:
    return result.name, tuple(result.argv), result.cwd
