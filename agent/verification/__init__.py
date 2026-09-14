"""Deterministic verification runner and baseline comparison interface."""

from agent.verification.contracts import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
)
from agent.verification.evaluation import classify_verification
from agent.verification.runner import VerificationRunner

__all__ = [
    "VerificationCheckStatus",
    "VerificationResult",
    "VerificationRunner",
    "VerificationSpec",
    "VerificationStatus",
    "classify_verification",
]
