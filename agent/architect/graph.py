"""Architect research graph with explicit invalid-step routing."""

import json
from collections.abc import Sequence
from typing import Any, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableConfig
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from agent.architect.models import ResearchEvaluation, ResearchStep
from agent.architect.runtime import ArchitectRuntime, default_architect_runtime
from agent.architect.state import SoftwareArchitectState
from agent.common.entities import ImplementationPlan
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools
from agent.runtime import BudgetSnapshot, DurableBudgetBoundary, DurableCallResult
from agent.runtime.budget import BudgetErrorCode
from agent.runtime.secrets import KnownSecretFilter
from agent.runtime.trajectory import EventRecorder


class SoftwareArchitectInput(TypedDict):
    implementation_research_scratchpad: list[AnyMessage]
    budget: NotRequired[BudgetSnapshot | None]
    durable_call_result: NotRequired[DurableCallResult | None]
    runtime_error_code: NotRequired[BudgetErrorCode | None]
    runtime_message: NotRequired[str]


class SoftwareArchitectOutput(TypedDict):
    implementation_plan: ImplementationPlan | None
    budget: NotRequired[BudgetSnapshot | None]
    durable_call_result: NotRequired[DurableCallResult | None]
    runtime_error_code: NotRequired[BudgetErrorCode | None]
    runtime_message: NotRequired[str]


def should_call_tool(state: SoftwareArchitectState):
    if state.runtime_error_code is not None:
        return "end"
    last_message = state.implementation_research_scratchpad[-1]
    if getattr(last_message, "tool_calls", None):
        return "should_call_tool"
    return "implement_plan"


def should_conduct_research(state: SoftwareArchitectState):
    if state.runtime_error_code is not None:
        return "end"
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
    budget_boundary: DurableBudgetBoundary | None = None,
    secret_filter: KnownSecretFilter | None = None,
    event_recorder: EventRecorder | None = None,
):
    runtime = runtime or default_architect_runtime()
    tools = list(
        codemap_tools + search_tools if research_tools is None else research_tools
    )

    def prepare_model_values(
        state: SoftwareArchitectState, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if budget_boundary is None:
            return values, {}
        timeout, deadline_update = budget_boundary.model_dispatch_timeout(state)
        if deadline_update:
            return None, deadline_update
        assert timeout is not None
        values["_model_request_timeout_seconds"] = timeout
        return values, {}

    def come_up_with_research_next_step(
        state: SoftwareArchitectState,
    ) -> dict[str, Any]:
        values = {
                "implementation_research_scratchpad": (
                    state.implementation_research_scratchpad
                ),
                "codebase_structure": runtime.load_codebase_structure(),
            }
        values, deadline_update = prepare_model_values(state, values)
        if deadline_update:
            return deadline_update
        assert values is not None
        response = runtime.plan_next_step(values)
        budget_update: dict[str, Any] = {}
        if budget_boundary is not None:
            response, budget_update = budget_boundary.capture_model(state, response)
            if response is None:
                return budget_update
        return {
            **budget_update,
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
        values = {
                "implementation_research_scratchpad": (
                    state.implementation_research_scratchpad
                )
            }
        values, deadline_update = prepare_model_values(state, values)
        if deadline_update:
            return deadline_update
        assert values is not None
        response = runtime.check_research_step(values)
        budget_update: dict[str, Any] = {}
        if budget_boundary is not None:
            response, budget_update = budget_boundary.capture_model(state, response)
            if response is None:
                return budget_update
        message = (
            "The research path is valid; start the research."
            if response.is_valid
            else f"The research path is invalid: {response.reasoning}"
        )
        return {
            **budget_update,
            "is_valid_research_step": response.is_valid,
            "implementation_research_scratchpad": [
                HumanMessage(content=message)
            ],
        }

    def conduct_research(state: SoftwareArchitectState) -> dict[str, Any]:
        values = {
                "implementation_research_scratchpad": (
                    state.implementation_research_scratchpad
                ),
                "codebase_structure": runtime.load_codebase_structure(),
            }
        values, deadline_update = prepare_model_values(state, values)
        if deadline_update:
            return deadline_update
        assert values is not None
        response = runtime.conduct_research(values)
        budget_update: dict[str, Any] = {}
        if budget_boundary is not None:
            response, budget_update = budget_boundary.capture_model(state, response)
            if response is None:
                return budget_update
        return {
            **budget_update,
            "implementation_research_scratchpad": [response],
        }

    def extract_implementation_plan(
        state: SoftwareArchitectState,
    ) -> dict[str, Any]:
        values = {
                "research_findings": convert_tools_messages_to_ai_and_human(
                    state.implementation_research_scratchpad
                ),
                "codebase_structure": runtime.load_codebase_structure(),
                "output_format": JsonOutputParser(
                    pydantic_object=ImplementationPlan
                ).get_format_instructions(),
            }
        values, deadline_update = prepare_model_values(state, values)
        if deadline_update:
            return deadline_update
        assert values is not None
        response = runtime.extract_implementation_plan(values)
        budget_update: dict[str, Any] = {}
        if budget_boundary is not None:
            response, budget_update = budget_boundary.capture_model(state, response)
            if response is None:
                return budget_update
        return {**budget_update, "implementation_plan": response}

    def request_values(state: SoftwareArchitectState, name: str) -> dict[str, Any]:
        if name == "extract_implementation_plan":
            return {
                "research_findings": convert_tools_messages_to_ai_and_human(
                    state.implementation_research_scratchpad
                ),
            }
        return {
            "scratchpad": state.implementation_research_scratchpad,
            "research_next_step": state.research_next_step,
            "node": name,
        }

    def reserve_model(name: str):
        def reserve(state: SoftwareArchitectState) -> dict[str, Any]:
            assert budget_boundary is not None
            return budget_boundary.reserve_model(
                state, name, request_values(state, name)
            )
        return reserve

    def settle_call(state: SoftwareArchitectState) -> dict[str, Any]:
        assert budget_boundary is not None
        return budget_boundary.settle(state)

    def reserve_tools(state: SoftwareArchitectState) -> dict[str, Any]:
        assert budget_boundary is not None
        calls = state.implementation_research_scratchpad[-1].tool_calls
        return budget_boundary.reserve_tools(state, calls)

    def record_tool_results(state: SoftwareArchitectState) -> dict[str, Any]:
        assert budget_boundary is not None
        return budget_boundary.record_tool_results(state)

    def after_settle(state: SoftwareArchitectState, route: str) -> str:
        assert budget_boundary is not None
        return budget_boundary.after_settle(state, route)

    tool_node = ToolNode(
        tools, messages_key="implementation_research_scratchpad"
    )

    def dispatch_tools(
        state: SoftwareArchitectState, config: RunnableConfig
    ) -> dict[str, Any]:
        assert budget_boundary is not None
        deadline_update = budget_boundary.guard_dispatch(state)
        if deadline_update:
            return deadline_update
        return tool_node.invoke(state, config)

    def route_after_tool_dispatch(state: SoftwareArchitectState) -> str:
        if state.runtime_error_code is not None:
            if budget_boundary is None or state.durable_call_result is None:
                return "end"
            return "settle"
        return "settle" if state.durable_call_result is not None else "record"

    def route_model_to_settle(state: SoftwareArchitectState) -> str:
        if (
            state.runtime_error_code is not None
            and state.durable_call_result is None
        ):
            return "end"
        return "settle"

    def persist_node(
        node,
        *,
        event_type: str | None = None,
        node_name: str | None = None,
    ):
        wrapped = secret_filter.wrap_node(node) if secret_filter is not None else node
        if event_recorder is not None and event_type is not None:
            return event_recorder.wrap_node(
                wrapped,
                event_type=event_type,
                node_name=node_name or event_type,
            )
        return wrapped

    workflow = StateGraph(
        SoftwareArchitectState,
        input_schema=SoftwareArchitectInput,
        output_schema=SoftwareArchitectOutput,
    )
    model_nodes = {
        "come_up_with_research_next_step": come_up_with_research_next_step,
        "check_research_step": check_research_step,
        "conduct_research": conduct_research,
        "extract_implementation_plan": extract_implementation_plan,
    }
    for name, node in model_nodes.items():
        workflow.add_node(
            name,
            persist_node(node, event_type="model", node_name=name),
        )
    workflow.add_node(
        "tools",
        persist_node(
            dispatch_tools if budget_boundary is not None else tool_node,
            event_type="tool",
            node_name="research_tools",
        ),
    )

    if budget_boundary is not None:
        for name in model_nodes:
            workflow.add_node(
                f"reserve_{name}", persist_node(reserve_model(name))
            )
            workflow.add_node(f"settle_{name}", persist_node(settle_call))
            workflow.add_conditional_edges(
                f"reserve_{name}",
                budget_boundary.may_dispatch,
                {"dispatch": name, "end": END},
            )
            workflow.add_conditional_edges(
                name,
                route_model_to_settle,
                {"settle": f"settle_{name}", "end": END},
            )
        workflow.add_node("reserve_tools", persist_node(reserve_tools))
        workflow.add_node(
            "record_tool_results", persist_node(record_tool_results)
        )
        workflow.add_node("settle_tools", persist_node(settle_call))
        workflow.add_conditional_edges(
            "reserve_tools",
            budget_boundary.may_dispatch,
            {"dispatch": "tools", "end": END},
        )
        workflow.add_conditional_edges(
            "tools",
            route_after_tool_dispatch,
            {
                "record": "record_tool_results",
                "settle": "settle_tools",
                "end": END,
            },
        )
        workflow.add_edge("record_tool_results", "settle_tools")
        workflow.add_edge(START, "reserve_come_up_with_research_next_step")
        workflow.add_conditional_edges(
            "settle_come_up_with_research_next_step",
            lambda state: after_settle(state, "continue"),
            {"continue": "reserve_check_research_step", "end": END},
        )
        workflow.add_conditional_edges(
            "settle_check_research_step",
            lambda state: (
                "end"
                if state.runtime_error_code is not None
                else should_conduct_research(state)
            ),
            {
                "plan_is_valid": "reserve_conduct_research",
                "plan_is_not_valid": "reserve_come_up_with_research_next_step",
                "end": END,
            },
        )
        workflow.add_conditional_edges(
            "settle_conduct_research",
            lambda state: (
                "end"
                if state.runtime_error_code is not None
                else should_call_tool(state)
            ),
            {
                "should_call_tool": "reserve_tools",
                "implement_plan": "reserve_extract_implementation_plan",
                "end": END,
            },
        )
        workflow.add_conditional_edges(
            "settle_tools",
            lambda state: after_settle(state, "continue"),
            {"continue": "reserve_conduct_research", "end": END},
        )
        workflow.add_conditional_edges(
            "settle_extract_implementation_plan",
            lambda state: after_settle(state, "complete"),
            {"complete": END, "end": END},
        )
        return workflow.compile().with_config({"tags": ["research-agent-v4"]})

    workflow.add_edge(START, "come_up_with_research_next_step")
    workflow.add_conditional_edges(
        "come_up_with_research_next_step",
        lambda state: "end" if state.runtime_error_code is not None else "continue",
        {"continue": "check_research_step", "end": END},
    )
    workflow.add_conditional_edges(
        "check_research_step",
        should_conduct_research,
        {
            "plan_is_valid": "conduct_research",
            "plan_is_not_valid": "come_up_with_research_next_step",
            "end": END,
        },
    )
    workflow.add_conditional_edges(
        "conduct_research",
        should_call_tool,
        {
            "should_call_tool": "tools",
            "implement_plan": "extract_implementation_plan",
            "end": END,
        },
    )
    workflow.add_conditional_edges(
        "tools",
        route_after_tool_dispatch,
        {"record": "conduct_research", "settle": "conduct_research", "end": END},
    )
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
