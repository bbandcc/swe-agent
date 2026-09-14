"""Deterministically compare baseline and post-edit verification evidence."""

from collections.abc import Sequence

from agent.verification.contracts import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationStatus,
)


def classify_verification(
    baseline: Sequence[VerificationResult],
    post: Sequence[VerificationResult],
) -> VerificationStatus:
    if not baseline and not post:
        return VerificationStatus.UNVERIFIED
    if len(baseline) != len(post) or any(
        _check_key(before) != _check_key(after)
        for before, after in zip(baseline, post, strict=True)
    ):
        return VerificationStatus.VERIFICATION_ERROR

    infrastructure_statuses = {
        VerificationCheckStatus.TIMEOUT,
        VerificationCheckStatus.EXECUTION_ERROR,
    }
    if any(
        result.status in infrastructure_statuses
        for result in (*baseline, *post)
    ):
        return VerificationStatus.VERIFICATION_ERROR

    improved = False
    for before, after in zip(baseline, post, strict=True):
        if before.status is VerificationCheckStatus.PASS:
            if after.status is VerificationCheckStatus.FAIL:
                return VerificationStatus.REGRESSION
            continue
        if after.status is VerificationCheckStatus.PASS:
            improved = True
        elif before.failure_id != after.failure_id:
            return VerificationStatus.REGRESSION

    if improved:
        return VerificationStatus.IMPROVED
    if all(result.status is VerificationCheckStatus.PASS for result in post):
        return VerificationStatus.VERIFIED
    return VerificationStatus.PRE_EXISTING_FAILURE


def _check_key(result: VerificationResult) -> tuple[object, ...]:
    return result.name, result.argv, result.cwd
