"""Deterministic baseline, post-edit, and bounded-repair graph nodes."""

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from agent.common.entities import ImplementationPlan
from agent.developer.editing import canonical_plan_path
from agent.developer.state import DeveloperStatus
from agent.editing import CommittedEdit, EditResult
from agent.outcome import WorkflowOutcome
from agent.runtime.budget import BudgetErrorCode, BudgetSnapshot
from agent.verification.contracts import (
    VerificationAttempt,
    VerificationAttemptStatus,
    VerificationCheckStatus,
    VerificationRecoveryPolicy,
    RepairScopePolicy,
    VerificationResult,
    VerificationSummary,
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
from agent.runtime.revision import detect_workspace_revision

MAX_REPAIR_ATTEMPTS = 2


class _VerificationState(Protocol):
    budget: BudgetSnapshot | None
    baseline_verification: tuple[VerificationSummary, ...]
    post_verification: tuple[VerificationSummary, ...]
    verification_status: VerificationStatus
    repair_attempts: int
    developer_status: DeveloperStatus
    implementation_plan: ImplementationPlan | None
    last_edit_result: EditResult | None
    outcome: WorkflowOutcome
    acceptance: AcceptanceResult | None
    committed_edits: tuple[CommittedEdit, ...]
    repair_failure_signatures: tuple[
        tuple[tuple[str, str, str], ...] | None, ...
    ]
    repair_patch_digests: tuple[str | None, ...]


class VerificationController:
    """Provide deterministic nodes for one configured verification lifecycle."""

    def __init__(
        self,
        specs: Sequence[VerificationSpec],
        runner: VerificationRunner | None,
        workspace_root: str | Path,
        *,
        runtime_root: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        secret_filter: KnownSecretFilter | None = None,
        recovery_policy: VerificationRecoveryPolicy = (
            VerificationRecoveryPolicy.STOP_ON_UNKNOWN
        ),
        repair_scope_policy: RepairScopePolicy = RepairScopePolicy.LAST_FILE,
    ) -> None:
        self._specs = tuple(specs)
        self._workspace_root = workspace_root
        self._runner = runner
        if self._specs and self._runner is None:
            self._runner = VerificationRunner(
                workspace_root,
                runtime_root=runtime_root,
                secret_filter=secret_filter,
            )
        self._clock = clock
        self._secret_filter = secret_filter
        try:
            self._recovery_policy = VerificationRecoveryPolicy(recovery_policy)
        except (TypeError, ValueError) as error:
            raise ValueError("Verification recovery policy is invalid.") from error
        if self._recovery_policy is not VerificationRecoveryPolicy.STOP_ON_UNKNOWN:
            raise ValueError(
                "Verification rerun policy requires an isolated execution seam."
            )
        try:
            self._repair_scope_policy = RepairScopePolicy(repair_scope_policy)
        except (TypeError, ValueError) as error:
            raise ValueError("Repair scope policy is invalid.") from error
        # A start node and its run node execute in one graph invocation.  This
        # process-local arm is deliberately absent after a durable restart, so
        # a checkpoint containing STARTED cannot silently dispatch again.
        self._armed_attempts: set[str] = set()

    def run_baseline(self, state: _VerificationState) -> dict[str, Any]:
        initial = {
            "post_verification": (),
            "verification_feedback": None,
            "repair_plan": None,
            "repair_attempts": 0,
            "repair_failure_signatures": (),
            "repair_patch_digests": (),
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
        if _has_evidence_problem(results):
            return {
                "post_verification": results,
                "verification_status": VerificationStatus.EVIDENCE_INSUFFICIENT,
                "verification_message": (
                    "Post-edit verification evidence is incomplete."
                ),
                "acceptance": _insufficient_acceptance(),
                "outcome": WorkflowOutcome.FAILED,
            }
        status = classify_verification(state.baseline_verification, results)
        acceptance = evaluate_acceptance(state.baseline_verification, results)
        stagnated = _repair_stagnated(
            state, results, self._repair_scope_policy
        )
        if stagnated:
            status = VerificationStatus.REPAIR_EXHAUSTED
        if (
            status is VerificationStatus.REGRESSION
            and state.developer_status is DeveloperStatus.COMPLETED
            and not _repair_plan(
                state.implementation_plan,
                state.last_edit_result,
                getattr(state, "committed_edits", ()),
                self._repair_scope_policy,
            ).tasks
        ):
            status = VerificationStatus.REPAIR_EXHAUSTED
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

    def start_attempt(
        self, state: _VerificationState, *, phase: str, spec_index: int
    ) -> dict[str, Any]:
        """Persist STARTED before a command can be dispatched."""
        attempt = self._new_attempt(state, phase=phase, spec_index=spec_index)
        attempts = list(getattr(state, "verification_attempts", ()))
        existing = next(
            (item for item in attempts if item.attempt_id == attempt.attempt_id),
            None,
        )
        if existing is not None:
            if existing.status is VerificationAttemptStatus.STARTED:
                return self._unknown_update(
                    tuple(attempts),
                    "A verification command was started but its result was not recorded; execution was not repeated.",
                )
            if not self._result_matches_spec(existing.result, self._specs[spec_index]):
                return self._unknown_update(
                    tuple(attempts),
                    "The recorded verification result does not match the trusted specification.",
                )
            return {"verification_attempts": tuple(attempts)}
        attempts.append(attempt)
        self._armed_attempts.add(attempt.attempt_id)
        return {"verification_attempts": tuple(attempts)}

    def run_attempt(
        self, state: _VerificationState, *, phase: str, spec_index: int
    ) -> dict[str, Any]:
        """Execute only an armed STARTED attempt and checkpoint its result."""
        attempt = self._attempt_for_state(state, phase=phase, spec_index=spec_index)
        if attempt is None:
            return self._unknown_update(
                getattr(state, "verification_attempts", ()),
                "The durable verification attempt is missing from checkpoint state.",
            )
        if attempt.status is VerificationAttemptStatus.RESULT_RECORDED:
            if not self._result_matches_spec(attempt.result, self._specs[spec_index]):
                return self._unknown_update(
                    getattr(state, "verification_attempts", ()),
                    "The recorded verification result does not match the trusted specification.",
                )
            return {}
        if attempt.attempt_id not in self._armed_attempts:
            return self._unknown_update(
                getattr(state, "verification_attempts", ()),
                "The verification command was started before this process resumed; its result is unknown.",
            )
        self._armed_attempts.discard(attempt.attempt_id)
        spec = self._specs[spec_index]
        budget = getattr(state, "budget", None)
        deadline = budget.deadline_at if budget is not None else None
        if deadline is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                result = _deadline_result(spec)
                return self._record_attempt_result(
                    state,
                    attempt,
                    result,
                    runtime_error_code=BudgetErrorCode.TIMEOUT_OVERRUN,
                    runtime_message=(
                        "The absolute run deadline elapsed before verification dispatch."
                    ),
                )
            spec = replace(spec, timeout_seconds=min(spec.timeout_seconds, remaining))
        result = self._runner.run(spec)
        if self._secret_filter is not None:
            result = self._secret_filter.sanitize(result).value
        if isinstance(result, VerificationResult):
            summary = VerificationSummary.from_result(result)
        elif isinstance(result, VerificationSummary):
            summary = result
        else:
            raise TypeError("Verification runner must return verification evidence.")
        runtime_error_code = None
        runtime_message = ""
        if deadline is not None and self._clock() >= deadline:
            runtime_error_code = BudgetErrorCode.TIMEOUT_OVERRUN
            runtime_message = (
                "The verification command crossed the absolute run deadline; "
                "its result was recorded and later checks were blocked."
            )
        return self._record_attempt_result(
            state,
            attempt,
            summary,
            runtime_error_code=runtime_error_code,
            runtime_message=runtime_message,
        )

    def finish_baseline_attempts(self, state: _VerificationState) -> dict[str, Any]:
        results = self._phase_results(state, "baseline")
        if state.runtime_error_code is not None:
            return {
                "baseline_verification": results,
                "verification_status": VerificationStatus.VERIFICATION_ERROR,
                "verification_message": (
                    "Baseline verification stopped before every attempt produced a result."
                ),
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
            }
        return self._baseline_result_update(state, results)

    def finish_post_attempts(self, state: _VerificationState) -> dict[str, Any]:
        results = self._phase_results(state, "post")
        if state.runtime_error_code is not None:
            return {
                "post_verification": results,
                "verification_status": VerificationStatus.VERIFICATION_ERROR,
                "verification_message": (
                    "Post-edit verification stopped before every attempt produced a result."
                ),
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
            }
        if _has_evidence_problem(results):
            return {
                "post_verification": results,
                "verification_status": VerificationStatus.EVIDENCE_INSUFFICIENT,
                "verification_message": (
                    "Post-edit verification evidence is incomplete."
                ),
                "acceptance": _insufficient_acceptance(),
                "outcome": WorkflowOutcome.FAILED,
            }
        status = classify_verification(state.baseline_verification, results)
        acceptance = evaluate_acceptance(state.baseline_verification, results)
        if _repair_stagnated(state, results, self._repair_scope_policy):
            status = VerificationStatus.REPAIR_EXHAUSTED
        if (
            status is VerificationStatus.REGRESSION
            and state.developer_status is DeveloperStatus.COMPLETED
            and not _repair_plan(
                state.implementation_plan,
                state.last_edit_result,
                getattr(state, "committed_edits", ()),
                self._repair_scope_policy,
            ).tasks
        ):
            status = VerificationStatus.REPAIR_EXHAUSTED
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

    def route_after_attempt_start(
        self, state: _VerificationState, *, phase: str, spec_index: int
    ) -> str:
        if state.runtime_error_code is not None:
            return "finish"
        attempt = self._attempt_for_state(state, phase=phase, spec_index=spec_index)
        return "finish" if attempt is not None and attempt.status is VerificationAttemptStatus.RESULT_RECORDED else "run"

    def route_after_attempt_run(
        self, state: _VerificationState, *, phase: str, spec_index: int
    ) -> str:
        return "finish" if state.runtime_error_code is not None else "next"

    def _new_attempt(
        self, state: _VerificationState, *, phase: str, spec_index: int
    ) -> VerificationAttempt:
        identity = getattr(state, "run_identity", None)
        run_id = getattr(identity, "run_id", None) or "local-run"
        task_id = getattr(identity, "task_id", None) or "local-task"
        repair_attempt = state.repair_attempts if phase == "post" else 0
        return VerificationAttempt.started(
            run_id=run_id,
            task_id=task_id,
            phase=phase,
            repair_attempt=repair_attempt,
            spec_index=spec_index,
            spec=self._specs[spec_index],
            attempt_ordinal=0,
            workspace_revision=detect_workspace_revision(self._workspace_root),
        )

    def _attempt_for_state(
        self, state: _VerificationState, *, phase: str, spec_index: int
    ) -> VerificationAttempt | None:
        expected = self._new_attempt(state, phase=phase, spec_index=spec_index)
        return next(
            (
                item
                for item in getattr(state, "verification_attempts", ())
                if item.attempt_id == expected.attempt_id
            ),
            None,
        )

    def _record_attempt_result(
        self,
        state: _VerificationState,
        attempt: VerificationAttempt,
        result: VerificationResult | VerificationSummary,
        *,
        runtime_error_code: BudgetErrorCode | None = None,
        runtime_message: str = "",
    ) -> dict[str, Any]:
        summary = (
            VerificationSummary.from_result(result)
            if isinstance(result, VerificationResult)
            else result
        )
        updated = attempt.with_result(summary)
        attempts = tuple(
            updated if item.attempt_id == attempt.attempt_id else item
            for item in getattr(state, "verification_attempts", ())
        )
        update: dict[str, Any] = {"verification_attempts": attempts}
        if runtime_error_code is not None:
            update.update(
                {
                    "runtime_error_code": runtime_error_code,
                    "runtime_message": runtime_message,
                }
            )
        return update

    @staticmethod
    def _result_matches_spec(
        result: VerificationSummary | None, spec: VerificationSpec
    ) -> bool:
        return bool(
            result is not None
            and result.name == spec.name
            and result.argv == tuple(spec.argv)
            and result.allowed_failure_case_ids
            == tuple(spec.allowed_failure_case_ids)
        )

    def _phase_results(
        self, state: _VerificationState, phase: str
    ) -> tuple[VerificationSummary, ...]:
        attempts = sorted(
            (
                item
                for item in getattr(state, "verification_attempts", ())
                if item.phase == phase
                and item.repair_attempt
                == (getattr(state, "repair_attempts", 0) if phase == "post" else 0)
                and item.result is not None
            ),
            key=lambda item: item.spec_index,
        )
        return tuple(item.result for item in attempts if item.result is not None)

    @staticmethod
    def _unknown_update(
        attempts: Sequence[VerificationAttempt], message: str
    ) -> dict[str, Any]:
        return {
            "verification_attempts": tuple(attempts),
            "runtime_error_code": BudgetErrorCode.OUTCOME_UNKNOWN,
            "runtime_message": message,
        }

    def _baseline_result_update(
        self,
        state: _VerificationState,
        results: tuple[VerificationSummary, ...],
    ) -> dict[str, Any]:
        initial = {
            "post_verification": (),
            "verification_feedback": None,
            "repair_plan": None,
            "repair_attempts": 0,
            "repair_failure_signatures": (),
            "repair_patch_digests": (),
            "acceptance": None,
            "developer_status": state.developer_status,
            "outcome": WorkflowOutcome.PENDING,
        }
        if _has_execution_problem(results):
            return {
                **initial,
                "baseline_verification": results,
                "verification_status": VerificationStatus.VERIFICATION_ERROR,
                "verification_message": "Baseline verification could not execute reliably.",
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
            }
        if _has_evidence_problem(results):
            return {
                **initial,
                "baseline_verification": results,
                "verification_status": VerificationStatus.EVIDENCE_INSUFFICIENT,
                "verification_message": "Baseline verification did not produce a valid structured report.",
                "outcome": WorkflowOutcome.FAILED,
                "acceptance": _insufficient_acceptance(),
            }
        return {
            **initial,
            "baseline_verification": results,
            "verification_status": VerificationStatus.PENDING,
            "verification_message": "Baseline verification completed.",
        }

    def prepare_repair(self, state: _VerificationState) -> dict[str, Any]:
        attempt = state.repair_attempts + 1
        failure_signature = verification_failure_signature(
            state.post_verification
        )
        repair_plan = _repair_plan(
            state.implementation_plan,
            state.last_edit_result,
            getattr(state, "committed_edits", ()),
            self._repair_scope_policy,
        )
        repair_paths = {
            canonical_plan_path(task.file_path)
            for task in repair_plan.tasks
        }
        repair_paths.discard(None)
        patch_digest = _committed_patch_digest(
            getattr(state, "committed_edits", ()),
            state.repair_attempts,
            repair_paths,
        )
        return {
            "repair_attempts": attempt,
            "repair_plan": repair_plan,
            "repair_failure_signatures": (
                *getattr(state, "repair_failure_signatures", ()),
                failure_signature,
            ),
            "repair_patch_digests": (
                *getattr(state, "repair_patch_digests", ()),
                patch_digest,
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
    ) -> tuple[tuple[VerificationSummary, ...], bool]:
        assert self._runner is not None
        results: list[VerificationSummary] = []
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
            if isinstance(result, VerificationResult):
                result = VerificationSummary.from_result(result)
            elif not isinstance(result, VerificationSummary):
                raise TypeError("Verification runner must return verification evidence.")
            results.append(result)
            if deadline is not None and self._clock() >= deadline:
                results.extend(
                    _deadline_result(pending)
                    for pending in self._specs[index + 1 :]
                )
                return tuple(results), True
        return tuple(results), False


def _deadline_result(spec: VerificationSpec) -> VerificationSummary:
    return VerificationSummary.from_result(VerificationResult.create(
        name=spec.name,
        argv=spec.argv,
        cwd=spec.cwd,
        status=VerificationCheckStatus.TIMEOUT,
        exit_code=None,
        message="Run deadline elapsed before this verification check started.",
    ))


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
    if any(result.artifact_error_code is not None for result in results):
        return True
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
        "stdout_preview": result.stdout,
        "stderr_preview": result.stderr,
        "stdout_digest": result.stdout_digest,
        "stderr_digest": result.stderr_digest,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "artifact_error_code": result.artifact_error_code,
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
    committed_edits: Sequence[CommittedEdit] = (),
    scope: RepairScopePolicy = RepairScopePolicy.LAST_FILE,
) -> ImplementationPlan:
    try:
        scope = RepairScopePolicy(scope)
    except (TypeError, ValueError):
        return ImplementationPlan(tasks=[])
    if plan is not None:
        committed = {
            canonical_plan_path(edit.path)
            for edit in committed_edits
        }
        committed.discard(None)
        if scope is RepairScopePolicy.COMMITTED_PLAN_FILES:
            tasks = [
                task
                for task in plan.tasks
                if canonical_plan_path(task.file_path) in committed
            ]
            return plan.model_copy(update={"tasks": tasks})
        if result is not None:
            committed_path = canonical_plan_path(result.path)
            if committed_path is not None and (
                committed_path in committed
                or not committed_edits
            ):
                for task in plan.tasks:
                    if canonical_plan_path(task.file_path) == committed_path:
                        return plan.model_copy(update={"tasks": [task]})
    return ImplementationPlan(tasks=[])


def verification_failure_signature(
    results: Sequence[VerificationSummary],
) -> tuple[tuple[str, str, str], ...] | None:
    """Return identity-safe failure cases, excluding diagnostic noise."""
    failures: list[tuple[str, str, str]] = []
    for result in results:
        report = result.report
        if report is None:
            return None
        for case in report.cases:
            if case.status.value != "pass":
                failures.append((result.name, case.case_id, case.status.value))
    if not failures:
        return None
    return tuple(sorted(set(failures)))


def _committed_patch_digest(
    edits: Sequence[CommittedEdit],
    repair_attempt: int,
    paths: set[str | None] | None = None,
) -> str | None:
    current = [
        edit
        for edit in edits
        if edit.repair_attempt == repair_attempt
        and (paths is None or canonical_plan_path(edit.path) in paths)
    ]
    if not current:
        return None
    payload = [
        {
            "path": edit.path,
            "patch_digest": edit.patch_digest,
            "after_hash": edit.after_hash,
        }
        for edit in sorted(current, key=lambda item: (item.task_index, item.path))
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _repair_stagnated(
    state: _VerificationState,
    results: Sequence[VerificationSummary],
    scope: RepairScopePolicy,
) -> bool:
    signature = verification_failure_signature(results)
    if signature is None:
        return False
    signatures = getattr(state, "repair_failure_signatures", ())
    patches = getattr(state, "repair_patch_digests", ())
    if not signatures or not patches or signatures[-1] is None:
        return False
    repair_plan = _repair_plan(
        state.implementation_plan,
        state.last_edit_result,
        getattr(state, "committed_edits", ()),
        scope,
    )
    repair_paths = {
        canonical_plan_path(task.file_path) for task in repair_plan.tasks
    }
    repair_paths.discard(None)
    patch = _committed_patch_digest(
        getattr(state, "committed_edits", ()),
        getattr(state, "repair_attempts", 0),
        repair_paths,
    )
    return patch is not None and signatures[-1] == signature and patches[-1] == patch
