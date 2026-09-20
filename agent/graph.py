import time
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph, add_messages
from pydantic import BaseModel, Field

from agent.architect.graph import swe_architect
from agent.common.entities import ImplementationPlan
from agent.developer.graph import swe_developer
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import EditResult
from agent.outcome import WorkflowOutcome
from agent.verification import (
    AcceptanceResult,
    VerificationResult,
    VerificationRunner,
    VerificationSpec,
    VerificationStatus,
)
from agent.verification.workflow import VerificationController
from agent.verification.config import configured_verification_specs
from agent.workspace import configured_workspace_root
from agent.runtime.boundary import DurableBudgetState
from agent.runtime.identity import RunIdentity
from agent.runtime.revision import AgentCodeRevision


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
    developer_status: DeveloperStatus = Field(DeveloperStatus.PENDING)
    developer_error_code: Optional[DeveloperErrorCode] = Field(None)
    developer_message: str = Field("")
    baseline_verification: tuple[VerificationResult, ...] = Field(
        default_factory=tuple
    )
    post_verification: tuple[VerificationResult, ...] = Field(
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


def create_workflow_graph(
    *,
    architect: Any = None,
    developer: Any = None,
    verification_specs: Sequence[VerificationSpec] = (),
    verification_runner: VerificationRunner | None = None,
    durable_runtime: bool = False,
    workspace_root: Any = None,
    clock: Callable[[], float] = time.time,
):
    """Create the parent workflow with injectable compiled child graphs."""
    verification = VerificationController(
        verification_specs,
        verification_runner,
        configured_workspace_root() if workspace_root is None else workspace_root,
        clock=clock,
    )

    def run_baseline(state: AgentState) -> dict[str, Any]:
        return verification.run_baseline(state)

    def run_post(state: AgentState) -> dict[str, Any]:
        return verification.run_post(state)

    def prepare_repair(state: AgentState) -> dict[str, Any]:
        return verification.prepare_repair(state)

    def route_after_baseline(state: AgentState) -> str:
        return verification.route_after_baseline(state)

    def route_after_post(state: AgentState) -> str:
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

    graph_builder = StateGraph(AgentState)

    graph_builder.add_node(
        "swe_architect", swe_architect if architect is None else architect
    )
    graph_builder.add_node(
        "swe_developer", swe_developer if developer is None else developer
    )
    graph_builder.add_node("run_baseline_verification", run_baseline)
    graph_builder.add_node("run_post_verification", run_post)
    graph_builder.add_node("prepare_repair", prepare_repair)
    graph_builder.add_node("finalize_outcome", finalize_outcome)
    if durable_runtime:
        graph_builder.add_node("finalize_runtime_failure", finalize_runtime_failure)
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
):
    """Build the production graph from trusted external verification config."""
    return create_workflow_graph(
        architect=architect,
        developer=developer,
        verification_specs=configured_verification_specs(),
        verification_runner=verification_runner,
    )


swe_agent = create_production_workflow_graph().compile().with_config(
    {"tags": ["agent-v1"], "recursion_limit": 200}
)
