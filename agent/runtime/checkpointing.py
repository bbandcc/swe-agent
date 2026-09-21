"""Public checkpoint lookup and conservative recovery inspection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent.common.entities import (
    AtomicTask,
    ImplementationPlan,
    ImplementationTask,
    PlanStatus,
)
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import (
    EditErrorCode,
    EditOperation,
    EditResult,
    EditStatus,
    WorkspaceSnapshot,
    WorkspaceTransaction,
)
from agent.outcome import WorkflowOutcome
from agent.runtime.boundary import DurableCallResult
from agent.artifacts import ArtifactRef
from agent.runtime.budget import (
    BudgetController,
    BudgetErrorCode,
    BudgetSnapshot,
    CallKind,
    CallReservation,
    CallStatus,
    RequestIdentityScope,
    UsageRecord,
    UsageStatus,
)
from agent.runtime.identity import (
    CheckpointLookup,
    RunCheckpoint,
    RunIdentity,
    WorkspaceIdentity,
)
from agent.runtime.revision import (
    AgentCodeRevision,
    AgentRevisionReason,
    AgentRevisionStatus,
)
from agent.verification import (
    AcceptanceReason,
    AcceptanceResult,
    VerificationCase,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationReport,
    VerificationResult,
    VerificationSummary,
    VerificationStatus,
)


class GraphCheckpointLookup(CheckpointLookup):
    """Read S3.1 identity contracts through the compiled graph API."""

    def __init__(self, graph: Any, thread_config: Mapping[str, Any]) -> None:
        self.graph = graph
        self.thread_config = thread_config

    def get(self, thread_id: str) -> RunCheckpoint | None:
        if thread_id != self.thread_config["configurable"]["thread_id"]:
            return None
        snapshot = self.graph.get_state(self.thread_config)
        values = snapshot.values
        if not values or values.get("run_identity") is None:
            return None
        return RunCheckpoint(
            identity=_identity(values["run_identity"]),
            run_config_digest=str(values["run_config_digest"]),
            agent_revision=_revision(values["agent_revision"]),
        )


def mark_uncertain_dispatch(
    graph: Any, thread_config: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Stop any parent or child IN_FLIGHT call lacking a durable result."""
    root_snapshot = graph.get_state(thread_config, subgraphs=True)
    for snapshot in _state_snapshots(root_snapshot):
        values = snapshot.values
        budget = values.get("budget") if values else None
        if isinstance(budget, dict):
            budget = BudgetSnapshot(**budget)
        if budget is None or not budget.active:
            continue
        if values.get("durable_call_result") is not None:
            continue
        next_nodes = tuple(snapshot.next)
        if next_nodes and all(
            "settle_" in node or "record_" in node for node in next_nodes
        ):
            continue
        marked = BudgetController.mark_outcome_unknown(budget)
        graph.update_state(
            thread_config,
            {
                "budget": marked,
                "runtime_error_code": BudgetErrorCode.OUTCOME_UNKNOWN,
                "runtime_message": (
                    "In-flight call outcome is unknown after recovery."
                ),
            },
        )
        return graph.get_state(thread_config).values
    return None


def checkpoint_serializer() -> JsonPlusSerializer:
    """Allow only project value types that can occur in durable state."""
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            WorkspaceIdentity,
            RunIdentity,
            AgentCodeRevision,
            AgentRevisionStatus,
            AgentRevisionReason,
            BudgetSnapshot,
            CallReservation,
            CallKind,
            CallStatus,
            RequestIdentityScope,
            BudgetErrorCode,
            UsageRecord,
            UsageStatus,
            DurableCallResult,
            AtomicTask,
            ImplementationTask,
            ImplementationPlan,
            PlanStatus,
            DeveloperStatus,
            DeveloperErrorCode,
            EditOperation,
            EditStatus,
            EditErrorCode,
            EditResult,
            WorkspaceSnapshot,
            WorkspaceTransaction,
            VerificationCheckStatus,
            AcceptanceReason,
            AcceptanceResult,
            VerificationCase,
            VerificationCaseStatus,
            VerificationReport,
            VerificationStatus,
            VerificationResult,
            VerificationSummary,
            ArtifactRef,
            WorkflowOutcome,
        ]
    )


def _state_snapshots(snapshot: Any):
    yield snapshot
    for task in snapshot.tasks:
        child = task.state
        if hasattr(child, "values") and hasattr(child, "tasks"):
            yield from _state_snapshots(child)


def _identity(value: RunIdentity | Mapping[str, Any]) -> RunIdentity:
    if isinstance(value, RunIdentity):
        return value
    workspace = value["workspace"]
    if not isinstance(workspace, WorkspaceIdentity):
        workspace = WorkspaceIdentity(**workspace)
    return RunIdentity(
        run_id=value["run_id"],
        thread_id=value["thread_id"],
        task_id=value["task_id"],
        workspace=workspace,
    )


def _revision(value: AgentCodeRevision | Mapping[str, Any]) -> AgentCodeRevision:
    if isinstance(value, AgentCodeRevision):
        return value
    reason = value.get("reason")
    return AgentCodeRevision(
        commit_sha=value.get("commit_sha"),
        status=AgentRevisionStatus(value["status"]),
        reason=AgentRevisionReason(reason) if reason else None,
    )
