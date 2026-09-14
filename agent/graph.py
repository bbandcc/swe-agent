from typing import Annotated, Any, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph, add_messages
from pydantic import BaseModel, Field

from agent.architect.graph import swe_architect
from agent.common.entities import ImplementationPlan
from agent.developer.graph import swe_developer
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import EditResult


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


def create_workflow_graph(*, architect: Any = None, developer: Any = None):
    """Create the parent workflow with injectable compiled child graphs."""
    graph_builder = StateGraph(AgentState)

    graph_builder.add_node(
        "swe_architect", swe_architect if architect is None else architect
    )
    graph_builder.add_node(
        "swe_developer", swe_developer if developer is None else developer
    )
    graph_builder.add_edge(START, "swe_architect")
    graph_builder.add_edge("swe_architect", "swe_developer")
    graph_builder.add_edge("swe_developer", END)

    return graph_builder


swe_agent = create_workflow_graph().compile().with_config(
    {"tags": ["agent-v1"], "recursion_limit": 200}
)
