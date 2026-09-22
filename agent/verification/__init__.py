"""Deterministic verification runner and baseline comparison interface."""

from agent.verification.contracts import (
    REPORT_SCHEMA,
    VerificationCase,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationReport,
    VerificationResult,
    VerificationSummary,
    VerificationSpec,
    VerificationStatus,
    VerificationAttempt,
    VerificationAttemptStatus,
    VerificationRecoveryPolicy,
    RepairScopePolicy,
    VERIFICATION_ATTEMPT_SCHEMA_VERSION,
    verification_spec_digest,
)
from agent.verification.evaluation import (
    AcceptanceReason,
    AcceptanceResult,
    classify_verification,
    evaluate_acceptance,
)
from agent.verification.report import VerificationReportError, parse_junit_xml
from agent.verification.runner import VerificationRunner

__all__ = [
    "VerificationCheckStatus",
    "VerificationCase",
    "VerificationCaseStatus",
    "VerificationReport",
    "REPORT_SCHEMA",
    "VerificationResult",
    "VerificationSummary",
    "VerificationRunner",
    "VerificationSpec",
    "VerificationStatus",
    "VerificationAttempt",
    "VerificationAttemptStatus",
    "VerificationRecoveryPolicy",
    "RepairScopePolicy",
    "VERIFICATION_ATTEMPT_SCHEMA_VERSION",
    "verification_spec_digest",
    "AcceptanceReason",
    "AcceptanceResult",
    "classify_verification",
    "evaluate_acceptance",
    "VerificationReportError",
    "parse_junit_xml",
]
