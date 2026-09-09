from agent.architect.graph import swe_architect
from agent.common.entities import ImplementationPlan
from agent.developer.graph import swe_developer
from agent.editing import EditResult
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages, StateGraph, START, END
from typing import Annotated, Optional

class AgentState(BaseModel):
    implementation_research_scratchpad: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    implementation_plan: Optional[ImplementationPlan] = Field(None, description="The implementation plan to be executed")
    last_edit_result: Optional[EditResult] = Field(None, description="The final Developer edit outcome")


def create_workflow_graph():
    """Create and return the workflow graph with conditional routing"""
    # Initialize graph
    graph_builder = StateGraph(AgentState)

    # Add nodes
    graph_builder.add_node("swe_architect", swe_architect)
    graph_builder.add_node("swe_developer", swe_developer)
    # Add edges for the workflow
    graph_builder.add_edge(START, "swe_architect")
    graph_builder.add_edge("swe_architect", "swe_developer")
    graph_builder.add_edge("swe_developer", END)

    return graph_builder

swe_agent = create_workflow_graph().compile().with_config({"tags":["agent-v1"], "recursion_limit": 200})
