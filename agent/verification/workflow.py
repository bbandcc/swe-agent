"""Deterministic baseline, post-edit, and bounded-repair graph nodes."""

from collections.abc import Sequence
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Protocol

from agent.common.entities import ImplementationPlan
from agent.developer.state import DeveloperStatus
from agent.editing import EditResult
from agent.outcome import WorkflowOutcome
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
    implementation_plan: ImplementationPlan | None
    last_edit_result: EditResult | None


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
            "repair_plan": None,
            "repair_attempts": 0,
            "developer_status": state.developer_status,
            "outcome": WorkflowOutcome.PENDING,
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
                "outcome": WorkflowOutcome.FAILED,
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
                "outcome": _workflow_outcome(
                    state.developer_status, VerificationStatus.UNVERIFIED
                ),
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
            "outcome": _workflow_outcome(state.developer_status, status),
        }

    def prepare_repair(self, state: _VerificationState) -> dict[str, Any]:
        attempt = state.repair_attempts + 1
        return {
            "repair_attempts": attempt,
            "repair_plan": _repair_plan(
                state.implementation_plan, state.last_edit_result
            ),
            "verification_feedback": {
                "attempt": attempt,
                "status": VerificationStatus.REGRESSION.value,
                "diagnostic_data_trust": "untrusted",
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
            "outcome": WorkflowOutcome.PENDING,
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
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
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


def _workflow_outcome(
    developer_status: DeveloperStatus,
    verification_status: VerificationStatus,
) -> WorkflowOutcome:
    if developer_status is DeveloperStatus.FAILED:
        return WorkflowOutcome.FAILED
    if developer_status is DeveloperStatus.NO_CHANGES:
        return WorkflowOutcome.NO_CHANGES
    if verification_status is VerificationStatus.UNVERIFIED:
        return WorkflowOutcome.UNVERIFIED
    if verification_status in {
        VerificationStatus.VERIFIED,
        VerificationStatus.IMPROVED,
        VerificationStatus.PRE_EXISTING_FAILURE,
    }:
        return WorkflowOutcome.COMPLETED
    if verification_status is VerificationStatus.REGRESSION:
        return WorkflowOutcome.PENDING
    return WorkflowOutcome.FAILED


def _repair_plan(
    plan: ImplementationPlan | None,
    result: EditResult | None,
) -> ImplementationPlan:
    if plan is not None and result is not None:
        committed_path = _relative_plan_path(result.path)
        for task in plan.tasks:
            if _relative_plan_path(task.file_path) == committed_path:
                return plan.model_copy(update={"tasks": [task]})
    return ImplementationPlan(tasks=[])


def _relative_plan_path(path: str) -> str:
    parts = list(PurePosixPath(path.replace("\\", "/")).parts)
    if parts and parts[0] == "workspace_repo":
        parts.pop(0)
    return "/".join(parts).casefold()
