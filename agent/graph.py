from collections.abc import Sequence
from typing import Annotated, Any, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph, add_messages
from pydantic import BaseModel, Field

from agent.architect.graph import swe_architect
from agent.common.entities import ImplementationPlan
from agent.developer.graph import swe_developer
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import EditResult
from agent.verification import (
    VerificationResult,
    VerificationRunner,
    VerificationSpec,
    VerificationStatus,
)
from agent.verification.workflow import VerificationController
from agent.workspace import configured_workspace_root


class AgentState(BaseModel):
    implementation_research_scratchpad: Annotated[
        list[AnyMessage], add_messages
    ] = Field(default_factory=list)
    implementation_plan: Optional[ImplementationPlan] = Field(
        None, description="The implementation plan to be executed"
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
    verification_feedback: dict[str, Any] | None = Field(None)
    repair_attempts: int = Field(0, ge=0)


def create_workflow_graph(
    *,
    architect: Any = None,
    developer: Any = None,
    verification_specs: Sequence[VerificationSpec] = (),
    verification_runner: VerificationRunner | None = None,
):
    """Create the parent workflow with injectable compiled child graphs."""
    verification = VerificationController(
        verification_specs,
        verification_runner,
        configured_workspace_root(),
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
    graph_builder.add_edge(START, "swe_architect")
    graph_builder.add_edge("swe_architect", "run_baseline_verification")
    graph_builder.add_conditional_edges(
        "run_baseline_verification",
        route_after_baseline,
        {"develop": "swe_developer", "end": END},
    )
    graph_builder.add_edge("swe_developer", "run_post_verification")
    graph_builder.add_conditional_edges(
        "run_post_verification",
        route_after_post,
        {"repair": "prepare_repair", "end": END},
    )
    graph_builder.add_edge("prepare_repair", "swe_developer")

    return graph_builder


swe_agent = create_workflow_graph().compile().with_config(
    {"tags": ["agent-v1"], "recursion_limit": 200}
)
