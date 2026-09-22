import time
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph, add_messages
from pydantic import BaseModel, Field, field_validator

from agent.architect.graph import swe_architect
from agent.common.entities import ImplementationPlan
from agent.developer.graph import swe_developer
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import EditResult, RecoveryResult, WriteIntent
from agent.outcome import WorkflowOutcome
from agent.verification import (
    AcceptanceResult,
    VerificationResult,
    VerificationRunner,
    VerificationSpec,
    VerificationSummary,
    VerificationStatus,
)
from agent.verification.workflow import VerificationController
from agent.verification.config import configured_verification_specs
from agent.workspace import (
    WorkspaceAccessPolicy,
    configured_workspace_access_policy,
    configured_workspace_root,
    workspace_access_scope,
)
from agent.runtime.boundary import DurableBudgetState
from agent.runtime.identity import RunIdentity
from agent.runtime.revision import AgentCodeRevision
from agent.runtime.secrets import KnownSecretFilter
from agent.runtime.trajectory import EventRecorder


class AgentState(DurableBudgetState):
    run_identity: RunIdentity | None = None
    run_config_digest: str | None = None
    agent_revision: AgentCodeRevision | None = None
    implementation_research_scratchpad: Annotated[
        list[AnyMessage], add_messages
    ] = Field(default_factory=list)
    implementation_plan: Optional[ImplementationPlan] = Field(
        None, description="The implementation plan to be executed"
    )
    repair_plan: Optional[ImplementationPlan] = Field(
        None, description="Current file-scoped repair plan"
    )
    last_edit_result: Optional[EditResult] = Field(
        None, description="The final Developer edit outcome"
    )
    pending_write: WriteIntent | None = Field(
        None, description="Durable file-write intent awaiting reconciliation"
    )
    last_recovery_result: RecoveryResult | None = Field(
        None, description="Latest deterministic pending-write reconciliation"
    )
    developer_status: DeveloperStatus = Field(DeveloperStatus.PENDING)
    developer_error_code: Optional[DeveloperErrorCode] = Field(None)
    developer_message: str = Field("")
    baseline_verification: tuple[VerificationSummary, ...] = Field(
        default_factory=tuple
    )
    post_verification: tuple[VerificationSummary, ...] = Field(
        default_factory=tuple
    )
    verification_status: VerificationStatus = Field(
        VerificationStatus.PENDING
    )
    verification_message: str = Field("")
    acceptance: AcceptanceResult | None = Field(None)
    verification_feedback: dict[str, Any] | None = Field(None)
    repair_attempts: int = Field(0, ge=0)
    outcome: WorkflowOutcome = Field(WorkflowOutcome.PENDING)

    @field_validator("baseline_verification", "post_verification", mode="before")
    @classmethod
    def _checkpoint_safe_verification_summaries(cls, value):
        if value is None:
            return value
        return tuple(
            VerificationSummary.from_result(item)
            if isinstance(item, VerificationResult)
            else item
            for item in value
        )


def create_workflow_graph(
    *,
    architect: Any = None,
    developer: Any = None,
    verification_specs: Sequence[VerificationSpec] = (),
    verification_runner: VerificationRunner | None = None,
    durable_runtime: bool = False,
    workspace_root: Any = None,
    runtime_root: Any = None,
    clock: Callable[[], float] = time.time,
    secret_filter: KnownSecretFilter | None = None,
    event_recorder: EventRecorder | None = None,
    access_policy: WorkspaceAccessPolicy | None = None,
):
    """Create the parent workflow with injectable compiled child graphs."""
    verification = VerificationController(
        verification_specs,
        verification_runner,
        configured_workspace_root() if workspace_root is None else workspace_root,
        runtime_root=runtime_root,
        clock=clock,
        secret_filter=secret_filter,
    )

    def run_baseline(state: AgentState) -> dict[str, Any]:
        return verification.run_baseline(state)

    def run_post(state: AgentState) -> dict[str, Any]:
        return verification.run_post(state)

    def prepare_repair(state: AgentState) -> dict[str, Any]:
        return verification.prepare_repair(state)

    def route_after_baseline(state: AgentState) -> str:
        if state.runtime_error_code is not None:
            return "end"
        return verification.route_after_baseline(state)

    def route_after_post(state: AgentState) -> str:
        if state.runtime_error_code is not None:
            return "end"
        return verification.route_after_post(state)

    def finalize_outcome(state: AgentState) -> dict[str, Any]:
        return verification.finalize_outcome(state)

    def route_after_runtime_node(state: AgentState) -> str:
        return "fail" if state.runtime_error_code is not None else "continue"

    def finalize_runtime_failure(state: AgentState) -> dict[str, Any]:
        return {
            "outcome": WorkflowOutcome.FAILED,
            "developer_status": DeveloperStatus.FAILED,
            "developer_message": state.runtime_message,
        }

    def persist_node(
        node,
        *,
        event_type: str | None = None,
        node_name: str | None = None,
    ):
        wrapped = secret_filter.wrap_node(node) if secret_filter is not None else node
        if event_recorder is not None and event_type is not None:
            wrapped = event_recorder.wrap_node(
                wrapped,
                event_type=event_type,
                node_name=node_name or event_type,
            )
        if access_policy is not None:
            original = wrapped

            def policy_wrapped(*args, **kwargs):
                with workspace_access_scope(access_policy):
                    if callable(original):
                        return original(*args, **kwargs)
                    return original.invoke(*args, **kwargs)

            wrapped = policy_wrapped
        return wrapped

    graph_builder = StateGraph(AgentState)

    graph_builder.add_node(
        "swe_architect",
        persist_node(swe_architect if architect is None else architect),
    )
    graph_builder.add_node(
        "swe_developer",
        persist_node(swe_developer if developer is None else developer),
    )
    graph_builder.add_node(
        "run_baseline_verification",
        persist_node(
            run_baseline,
            event_type="verification",
            node_name="run_baseline_verification",
        ),
    )
    graph_builder.add_node(
        "run_post_verification",
        persist_node(
            run_post,
            event_type="verification",
            node_name="run_post_verification",
        ),
    )
    graph_builder.add_node("prepare_repair", persist_node(prepare_repair))
    graph_builder.add_node("finalize_outcome", persist_node(finalize_outcome))
    if durable_runtime:
        graph_builder.add_node(
            "finalize_runtime_failure", persist_node(finalize_runtime_failure)
        )
    graph_builder.add_edge(START, "swe_architect")
    if durable_runtime:
        graph_builder.add_conditional_edges(
            "swe_architect",
            route_after_runtime_node,
            {
                "continue": "run_baseline_verification",
                "fail": "finalize_runtime_failure",
            },
        )
    else:
        graph_builder.add_edge("swe_architect", "run_baseline_verification")
    graph_builder.add_conditional_edges(
        "run_baseline_verification",
        route_after_baseline,
        {"develop": "swe_developer", "end": "finalize_outcome"},
    )
    if durable_runtime:
        graph_builder.add_conditional_edges(
            "swe_developer",
            route_after_runtime_node,
            {
                "continue": "run_post_verification",
                "fail": "finalize_runtime_failure",
            },
        )
    else:
        graph_builder.add_edge("swe_developer", "run_post_verification")
    graph_builder.add_conditional_edges(
        "run_post_verification",
        route_after_post,
        {"repair": "prepare_repair", "end": "finalize_outcome"},
    )
    graph_builder.add_edge("prepare_repair", "swe_developer")
    graph_builder.add_edge("finalize_outcome", END)
    if durable_runtime:
        graph_builder.add_edge("finalize_runtime_failure", END)

    return graph_builder


def create_production_workflow_graph(
    *,
    architect: Any = None,
    developer: Any = None,
    verification_runner: VerificationRunner | None = None,
    access_policy: WorkspaceAccessPolicy | None = None,
):
    """Build the production graph from trusted external verification config."""
    trusted_policy = (
        configured_workspace_access_policy()
        if access_policy is None
        else access_policy
    )
    return create_workflow_graph(
        architect=architect,
        developer=developer,
        verification_specs=configured_verification_specs(),
        verification_runner=verification_runner,
        access_policy=trusted_policy,
    )


swe_agent = create_production_workflow_graph().compile().with_config(
    {"tags": ["agent-v1"], "recursion_limit": 200}
)
