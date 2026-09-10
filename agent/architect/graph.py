"""Architect research graph with explicit invalid-step routing."""

import json
from collections.abc import Sequence
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.output_parsers import JsonOutputParser
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from agent.architect.models import ResearchEvaluation, ResearchStep
from agent.architect.runtime import ArchitectRuntime, default_architect_runtime
from agent.architect.state import SoftwareArchitectState
from agent.common.entities import ImplementationPlan
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools


class SoftwareArchitectInput(TypedDict):
    implementation_research_scratchpad: list[AnyMessage]


class SoftwareArchitectOutput(TypedDict):
    implementation_plan: ImplementationPlan | None


def should_call_tool(state: SoftwareArchitectState):
    last_message = state.implementation_research_scratchpad[-1]
    if getattr(last_message, "tool_calls", None):
        return "should_call_tool"
    return "implement_plan"


def should_conduct_research(state: SoftwareArchitectState):
    if state.is_valid_research_step:
        return "plan_is_valid"
    return "plan_is_not_valid"


def convert_tools_messages_to_ai_and_human(
    implementation_research_scratchpad: list[AnyMessage],
) -> list[AnyMessage]:
    messages: list[AnyMessage] = []
    for message in implementation_research_scratchpad:
        if message.type == "ai" and message.tool_calls:
            tool_name = message.tool_calls[0]["name"]
            tool_args = json.dumps(message.tool_calls[0]["args"])
            messages.append(
                AIMessage(
                    content=(
                        f"I want to call the tool {tool_name} with the following "
                        f"arguments: {tool_args}"
                    )
                )
            )
        elif message.type == "tool":
            messages.append(
                HumanMessage(
                    content=(
                        f"When executing Tool {message.name}\n"
                        f"The result was {message.content}"
                    )
                )
            )
        else:
            messages.append(message)
    return messages


def create_architect_workflow(
    runtime: ArchitectRuntime | None = None,
    *,
    research_tools: Sequence[Any] | None = None,
):
    runtime = runtime or default_architect_runtime()
    tools = list(
        codemap_tools + search_tools if research_tools is None else research_tools
    )

    def come_up_with_research_next_step(
        state: SoftwareArchitectState,
    ) -> dict[str, Any]:
        response = runtime.plan_next_step(
            {
                "implementation_research_scratchpad": (
                    state.implementation_research_scratchpad
                ),
                "codebase_structure": runtime.load_codebase_structure(),
            }
        )
        return {
            "research_next_step": response.hypothesis,
            "implementation_research_scratchpad": [
                AIMessage(
                    content=(
                        "My next research hypothesis is "
                        f"{response.hypothesis}. Reason: {response.reasoning}"
                    )
                )
            ],
        }

    def check_research_step(
        state: SoftwareArchitectState,
    ) -> dict[str, Any]:
        response = runtime.check_research_step(
            {
                "implementation_research_scratchpad": (
                    state.implementation_research_scratchpad
                )
            }
        )
        message = (
            "The research path is valid; start the research."
            if response.is_valid
            else f"The research path is invalid: {response.reasoning}"
        )
        return {
            "is_valid_research_step": response.is_valid,
            "implementation_research_scratchpad": [
                HumanMessage(content=message)
            ],
        }

    def conduct_research(state: SoftwareArchitectState) -> dict[str, Any]:
        response = runtime.conduct_research(
            {
                "implementation_research_scratchpad": (
                    state.implementation_research_scratchpad
                ),
                "codebase_structure": runtime.load_codebase_structure(),
            }
        )
        return {"implementation_research_scratchpad": [response]}

    def extract_implementation_plan(
        state: SoftwareArchitectState,
    ) -> dict[str, ImplementationPlan]:
        response = runtime.extract_implementation_plan(
            {
                "research_findings": convert_tools_messages_to_ai_and_human(
                    state.implementation_research_scratchpad
                ),
                "codebase_structure": runtime.load_codebase_structure(),
                "output_format": JsonOutputParser(
                    pydantic_object=ImplementationPlan
                ).get_format_instructions(),
            }
        )
        return {"implementation_plan": response}

    tool_node = ToolNode(
        tools, messages_key="implementation_research_scratchpad"
    )
    workflow = StateGraph(
        SoftwareArchitectState,
        input_schema=SoftwareArchitectInput,
        output_schema=SoftwareArchitectOutput,
    )
    workflow.add_node(
        "come_up_with_research_next_step", come_up_with_research_next_step
    )
    workflow.add_node("check_research_step", check_research_step)
    workflow.add_node("conduct_research", conduct_research)
    workflow.add_node("extract_implementation_plan", extract_implementation_plan)
    workflow.add_node("tools", tool_node)

    workflow.add_edge(START, "come_up_with_research_next_step")
    workflow.add_edge("come_up_with_research_next_step", "check_research_step")
    workflow.add_conditional_edges(
        "check_research_step",
        should_conduct_research,
        {
            "plan_is_valid": "conduct_research",
            "plan_is_not_valid": "come_up_with_research_next_step",
        },
    )
    workflow.add_conditional_edges(
        "conduct_research",
        should_call_tool,
        {
            "should_call_tool": "tools",
            "implement_plan": "extract_implementation_plan",
        },
    )
    workflow.add_edge("tools", "conduct_research")
    workflow.add_edge("extract_implementation_plan", END)
    return workflow.compile().with_config({"tags": ["research-agent-v4"]})


swe_architect = create_architect_workflow()

__all__ = [
    "ArchitectRuntime",
    "ResearchEvaluation",
    "ResearchStep",
    "create_architect_workflow",
    "swe_architect",
]
