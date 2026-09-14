"""Deterministic baseline, post-edit, and bounded-repair graph nodes."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from agent.developer.state import DeveloperStatus
from agent.verification.contracts import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
)
from agent.verification.evaluation import classify_verification
from agent.verification.runner import VerificationRunner

MAX_REPAIR_ATTEMPTS = 2


class _VerificationState(Protocol):
    baseline_verification: tuple[VerificationResult, ...]
    post_verification: tuple[VerificationResult, ...]
    verification_status: VerificationStatus
    repair_attempts: int
    developer_status: DeveloperStatus


class VerificationController:
    """Provide deterministic nodes for one configured verification lifecycle."""

    def __init__(
        self,
        specs: Sequence[VerificationSpec],
        runner: VerificationRunner | None,
        workspace_root: str | Path,
    ) -> None:
        self._specs = tuple(specs)
        self._runner = runner
        if self._specs and self._runner is None:
            self._runner = VerificationRunner(workspace_root)

    def run_baseline(self, state: _VerificationState) -> dict[str, Any]:
        initial = {
            "post_verification": (),
            "verification_feedback": None,
            "repair_attempts": 0,
            "developer_status": state.developer_status,
        }
        if not self._specs:
            return {
                **initial,
                "baseline_verification": (),
                "verification_status": VerificationStatus.UNVERIFIED,
                "verification_message": "No verification checks are configured.",
            }
        results = self._run_checks()
        if _has_execution_problem(results):
            return {
                **initial,
                "baseline_verification": results,
                "verification_status": VerificationStatus.VERIFICATION_ERROR,
                "verification_message": (
                    "Baseline verification could not execute reliably."
                ),
            }
        return {
            **initial,
            "baseline_verification": results,
            "verification_status": VerificationStatus.PENDING,
            "verification_message": "Baseline verification completed.",
        }

    def run_post(self, state: _VerificationState) -> dict[str, Any]:
        if not self._specs:
            return {
                "post_verification": (),
                "verification_status": VerificationStatus.UNVERIFIED,
                "verification_message": "No verification checks are configured.",
            }
        results = self._run_checks()
        status = classify_verification(state.baseline_verification, results)
        if (
            status is VerificationStatus.REGRESSION
            and state.repair_attempts >= MAX_REPAIR_ATTEMPTS
        ):
            status = VerificationStatus.REPAIR_EXHAUSTED
        return {
            "post_verification": results,
            "verification_status": status,
            "verification_message": _verification_message(status),
        }

    def prepare_repair(self, state: _VerificationState) -> dict[str, Any]:
        attempt = state.repair_attempts + 1
        return {
            "repair_attempts": attempt,
            "verification_feedback": {
                "attempt": attempt,
                "status": VerificationStatus.REGRESSION.value,
                "baseline_results": [
                    _result_feedback(result)
                    for result in state.baseline_verification
                ],
                "post_results": [
                    _result_feedback(result)
                    for result in state.post_verification
                ],
            },
            "developer_status": DeveloperStatus.PENDING,
            "developer_error_code": None,
            "developer_message": "",
        }

    def route_after_baseline(self, state: _VerificationState) -> str:
        if state.verification_status is VerificationStatus.VERIFICATION_ERROR:
            return "end"
        return "develop"

    def route_after_post(self, state: _VerificationState) -> str:
        if (
            state.verification_status is VerificationStatus.REGRESSION
            and state.developer_status is DeveloperStatus.COMPLETED
            and state.repair_attempts < MAX_REPAIR_ATTEMPTS
        ):
            return "repair"
        return "end"

    def _run_checks(self) -> tuple[VerificationResult, ...]:
        assert self._runner is not None
        return tuple(self._runner.run(spec) for spec in self._specs)


def _has_execution_problem(results: Sequence[VerificationResult]) -> bool:
    return any(
        result.status
        in {
            VerificationCheckStatus.TIMEOUT,
            VerificationCheckStatus.EXECUTION_ERROR,
        }
        for result in results
    )


def _result_feedback(result: VerificationResult) -> dict[str, object]:
    return {
        "name": result.name,
        "argv": result.argv,
        "cwd": result.cwd,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "failure_id": result.failure_id,
        "message": result.message,
    }


def _verification_message(status: VerificationStatus) -> str:
    messages = {
        VerificationStatus.VERIFIED: "Post-edit verification passed.",
        VerificationStatus.IMPROVED: (
            "Previously failing verification checks now pass."
        ),
        VerificationStatus.PRE_EXISTING_FAILURE: (
            "Post-edit failures match the baseline failures."
        ),
        VerificationStatus.REGRESSION: (
            "Post-edit verification introduced a regression."
        ),
        VerificationStatus.REPAIR_EXHAUSTED: (
            "The regression remains after two repair attempts."
        ),
        VerificationStatus.VERIFICATION_ERROR: (
            "Verification could not execute reliably."
        ),
    }
    return messages.get(status, status.value)
