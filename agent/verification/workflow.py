"""Deterministic baseline, post-edit, and bounded-repair graph nodes."""

import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from agent.common.entities import ImplementationPlan
from agent.developer.editing import canonical_plan_path
from agent.developer.state import DeveloperStatus
from agent.editing import EditResult
from agent.outcome import WorkflowOutcome
from agent.runtime.budget import BudgetErrorCode, BudgetSnapshot
from agent.verification.contracts import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
)
from agent.verification.evaluation import (
    AcceptanceReason,
    AcceptanceResult,
    classify_verification,
    evaluate_acceptance,
)
from agent.verification.runner import VerificationRunner
from agent.runtime.secrets import KnownSecretFilter

MAX_REPAIR_ATTEMPTS = 2


class _VerificationState(Protocol):
    budget: BudgetSnapshot | None
    baseline_verification: tuple[VerificationResult, ...]
    post_verification: tuple[VerificationResult, ...]
    verification_status: VerificationStatus
    repair_attempts: int
    developer_status: DeveloperStatus
    implementation_plan: ImplementationPlan | None
    last_edit_result: EditResult | None
    outcome: WorkflowOutcome
    acceptance: AcceptanceResult | None


class VerificationController:
    """Provide deterministic nodes for one configured verification lifecycle."""

    def __init__(
        self,
        specs: Sequence[VerificationSpec],
        runner: VerificationRunner | None,
        workspace_root: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        secret_filter: KnownSecretFilter | None = None,
    ) -> None:
        self._specs = tuple(specs)
        self._runner = runner
        if self._specs and self._runner is None:
            self._runner = VerificationRunner(workspace_root)
        self._clock = clock
        self._secret_filter = secret_filter

    def run_baseline(self, state: _VerificationState) -> dict[str, Any]:
        initial = {
            "post_verification": (),
            "verification_feedback": None,
            "repair_plan": None,
            "repair_attempts": 0,
            "acceptance": None,
            "developer_status": state.developer_status,
            "outcome": WorkflowOutcome.PENDING,
        }
        if not self._specs:
            return {
                **initial,
                "baseline_verification": (),
                "verification_status": VerificationStatus.UNVERIFIED,
                "verification_message": "No verification checks are configured.",
                "acceptance": _insufficient_acceptance(),
            }
        results, deadline_overrun = self._run_checks(state)
        if deadline_overrun:
            return {
                **initial,
                **_deadline_error_update(),
                "baseline_verification": results,
                "verification_status": VerificationStatus.VERIFICATION_ERROR,
                "verification_message": (
                    "Baseline verification exceeded the absolute run deadline."
                ),
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
            }
        if _has_execution_problem(results):
            return {
                **initial,
                "baseline_verification": results,
                "verification_status": VerificationStatus.VERIFICATION_ERROR,
                "verification_message": (
                    "Baseline verification could not execute reliably."
                ),
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
            }
        if _has_evidence_problem(results):
            return {
                **initial,
                "baseline_verification": results,
                "verification_status": VerificationStatus.EVIDENCE_INSUFFICIENT,
                "verification_message": (
                    "Baseline verification did not produce a valid structured report."
                ),
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
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
                "acceptance": _insufficient_acceptance(),
                "outcome": _workflow_outcome(
                    state.developer_status, VerificationStatus.UNVERIFIED
                ),
            }
        results, deadline_overrun = self._run_checks(state)
        if deadline_overrun:
            return {
                **_deadline_error_update(),
                "post_verification": results,
                "verification_status": VerificationStatus.VERIFICATION_ERROR,
                "verification_message": (
                    "Post-edit verification exceeded the absolute run deadline."
                ),
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
            }
        status = classify_verification(state.baseline_verification, results)
        acceptance = evaluate_acceptance(state.baseline_verification, results)
        if (
            status is VerificationStatus.REGRESSION
            and state.repair_attempts >= MAX_REPAIR_ATTEMPTS
        ):
            status = VerificationStatus.REPAIR_EXHAUSTED
        return {
            "post_verification": results,
            "verification_status": status,
            "verification_message": _verification_message(status),
            "acceptance": acceptance,
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
            "acceptance": None,
            "outcome": WorkflowOutcome.PENDING,
        }

    def route_after_baseline(self, state: _VerificationState) -> str:
        if state.verification_status in {
            VerificationStatus.VERIFICATION_ERROR,
            VerificationStatus.EVIDENCE_INSUFFICIENT,
        }:
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

    def finalize_outcome(self, state: _VerificationState) -> dict[str, Any]:
        """Seal graph-terminal state so PENDING never escapes through END."""
        if state.outcome is WorkflowOutcome.PENDING:
            return {"outcome": WorkflowOutcome.FAILED}
        return {"outcome": state.outcome}

    def _run_checks(
        self, state: _VerificationState
    ) -> tuple[tuple[VerificationResult, ...], bool]:
        assert self._runner is not None
        results: list[VerificationResult] = []
        budget = getattr(state, "budget", None)
        deadline = budget.deadline_at if budget is not None else None
        for index, spec in enumerate(self._specs):
            effective = spec
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    results.extend(
                        _deadline_result(pending)
                        for pending in self._specs[index:]
                    )
                    return tuple(results), True
                effective = replace(
                    spec,
                    timeout_seconds=min(spec.timeout_seconds, remaining),
                )
            result = self._runner.run(effective)
            if self._secret_filter is not None:
                result = self._secret_filter.sanitize(result).value
            results.append(result)
            if deadline is not None and self._clock() >= deadline:
                results.extend(
                    _deadline_result(pending)
                    for pending in self._specs[index + 1 :]
                )
                return tuple(results), True
        return tuple(results), False


def _deadline_result(spec: VerificationSpec) -> VerificationResult:
    return VerificationResult.create(
        name=spec.name,
        argv=spec.argv,
        cwd=spec.cwd,
        status=VerificationCheckStatus.TIMEOUT,
        exit_code=None,
        message="Run deadline elapsed before this verification check started.",
    )


def _deadline_error_update() -> dict[str, object]:
    return {
        "runtime_error_code": BudgetErrorCode.TIMEOUT_OVERRUN,
        "runtime_message": (
            "The absolute run deadline was exhausted during verification; "
            "remaining checks and later side effects were blocked."
        ),
    }


def _insufficient_acceptance() -> AcceptanceResult:
    return AcceptanceResult(False, AcceptanceReason.EVIDENCE_INSUFFICIENT)


def _has_execution_problem(results: Sequence[VerificationResult]) -> bool:
    return any(
        result.status
        in {
            VerificationCheckStatus.TIMEOUT,
            VerificationCheckStatus.EXECUTION_ERROR,
        }
        for result in results
    )


def _has_evidence_problem(results: Sequence[VerificationResult]) -> bool:
    if any(result.report is None for result in results):
        return True
    return classify_verification(results, results) is VerificationStatus.EVIDENCE_INSUFFICIENT


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
        "report_schema": result.report.report_schema if result.report else None,
        "cases": (
            [
                {"case_id": case.case_id, "status": case.status.value}
                for case in result.report.cases
            ]
            if result.report
            else None
        ),
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
        VerificationStatus.EVIDENCE_INSUFFICIENT: (
            "Verification did not provide sufficient structured evidence."
        ),
    }
    return messages.get(status, status.value)


def _workflow_outcome(
    developer_status: DeveloperStatus,
    verification_status: VerificationStatus,
) -> WorkflowOutcome:
    if verification_status in {
        VerificationStatus.VERIFICATION_ERROR,
        VerificationStatus.EVIDENCE_INSUFFICIENT,
        VerificationStatus.REPAIR_EXHAUSTED,
    }:
        return WorkflowOutcome.FAILED
    if verification_status is VerificationStatus.REGRESSION:
        return (
            WorkflowOutcome.PENDING
            if developer_status is DeveloperStatus.COMPLETED
            else WorkflowOutcome.FAILED
        )
    if developer_status is DeveloperStatus.FAILED:
        return WorkflowOutcome.FAILED
    if developer_status in {
        DeveloperStatus.PENDING,
        DeveloperStatus.RUNNING,
    }:
        return WorkflowOutcome.PENDING
    if developer_status is DeveloperStatus.NO_CHANGES:
        return WorkflowOutcome.NO_CHANGES
    if verification_status is VerificationStatus.UNVERIFIED:
        return WorkflowOutcome.UNVERIFIED
    return WorkflowOutcome.COMPLETED


def _repair_plan(
    plan: ImplementationPlan | None,
    result: EditResult | None,
) -> ImplementationPlan:
    if plan is not None and result is not None:
        committed_path = canonical_plan_path(result.path)
        if committed_path is not None:
            for task in plan.tasks:
                if canonical_plan_path(task.file_path) == committed_path:
                    return plan.model_copy(update={"tasks": [task]})
    return ImplementationPlan(tasks=[])
