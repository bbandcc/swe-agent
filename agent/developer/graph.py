import json
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from agent.developer.runtime import DeveloperRuntime, default_developer_runtime
from agent.developer.state import SoftwareDeveloperState
from agent.editing import EditErrorCode, EditResult, EditStatus
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools


def start_implementing(state: SoftwareDeveloperState) -> dict[str, Any]:
    plan = state.implementation_plan
    error = None
    if plan is None or not plan.tasks:
        error = "The implementation plan contains no tasks."
    elif any(not task.atomic_tasks for task in plan.tasks):
        error = "Every implementation task must contain at least one atomic task."

    result = None
    if error is not None:
        result = EditResult(
            status=EditStatus.REJECTED,
            path="",
            error_code=EditErrorCode.INVALID_PLAN,
            message=error,
        )
    return {
        "current_task_idx": 0,
        "current_atomic_task_idx": 0,
        "last_edit_result": result,
    }


def proceed_to_next_atomic_task(
    state: SoftwareDeveloperState,
) -> dict[str, int]:
    plan = state.implementation_plan
    if plan is None:
        return {"current_task_idx": 0, "current_atomic_task_idx": 0}

    current_task = plan.tasks[state.current_task_idx]
    if state.current_atomic_task_idx >= len(current_task.atomic_tasks) - 1:
        return {
            "current_task_idx": state.current_task_idx + 1,
            "current_atomic_task_idx": 0,
        }
    return {
        "current_task_idx": state.current_task_idx,
        "current_atomic_task_idx": state.current_atomic_task_idx + 1,
    }


def is_implementation_complete(state: SoftwareDeveloperState):
    if state.last_edit_result is not None and (
        state.last_edit_result.error_code is EditErrorCode.INVALID_PLAN
    ):
        return END
    plan = state.implementation_plan
    if plan is None or state.current_task_idx >= len(plan.tasks):
        return END
    return "continue"


def should_continue_implementation_research(state: SoftwareDeveloperState) -> str:
    last_research_step = state.atomic_implementation_research[-1]
    if last_research_step.tool_calls:
        return "should_continue_research"
    return "implement_plan"


def should_continue_after_preparation(state: SoftwareDeveloperState):
    snapshot = state.current_file_snapshot
    if snapshot is None or snapshot.error_code is not None:
        return END
    return "continue"


def should_advance_after_edit(state: SoftwareDeveloperState):
    result = state.last_edit_result
    if result is not None and result.status is EditStatus.APPLIED:
        return "advance"
    return END


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


def create_developer_workflow(
    runtime: DeveloperRuntime | None = None,
    *,
    research_tools: Sequence[Any] | None = None,
):
    runtime = runtime or default_developer_runtime()
    tools = list(
        search_tools + codemap_tools if research_tools is None else research_tools
    )

    def prepare_for_implementation(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        plan = state.implementation_plan
        if plan is None:
            return {}
        current_task = plan.tasks[state.current_task_idx]
        executor = runtime.edit_executor()
        snapshot = executor.prepare(current_task.file_path)
        result = executor.apply(snapshot, "") if snapshot.error_code else None
        structure = (
            state.codebase_structure
            if snapshot.error_code
            else runtime.load_codebase_structure()
        )
        return {
            "current_file_snapshot": snapshot,
            "current_file_content": snapshot.content or "",
            "codebase_structure": structure,
            "atomic_implementation_research": [],
            "last_edit_result": result,
        }

    def get_clear_implementation_plan_for_atomic_task(
        state: SoftwareDeveloperState,
    ) -> dict[str, list[AnyMessage]]:
        plan = state.implementation_plan
        if plan is None:
            return {"atomic_implementation_research": []}
        current_task = plan.tasks[state.current_task_idx]
        current_atomic_task = current_task.atomic_tasks[
            state.current_atomic_task_idx
        ]
        result = runtime.research_atomic_task(
            {
                "development_task": current_atomic_task.atomic_task,
                "file_content": state.current_file_content,
                "target_file": state.current_file_snapshot.path,
                "codebase_structure": state.codebase_structure,
                "additional_context": current_atomic_task.additional_context,
                "atomic_implementation_research": (
                    state.atomic_implementation_research
                ),
            }
        )
        return {"atomic_implementation_research": [result]}

    def creating_diffs_for_task(
        state: SoftwareDeveloperState,
    ) -> dict[str, EditResult]:
        plan = state.implementation_plan
        snapshot = state.current_file_snapshot
        if plan is None or snapshot is None:
            return {
                "last_edit_result": EditResult(
                    status=EditStatus.REJECTED,
                    path="",
                    error_code=EditErrorCode.INVALID_STATE,
                    message="The Developer has no prepared workspace snapshot.",
                )
            }

        current_task = plan.tasks[state.current_task_idx]
        current_atomic_task = current_task.atomic_tasks[
            state.current_atomic_task_idx
        ]
        values = {
            "task": current_atomic_task.atomic_task,
            "additional_context": current_atomic_task.additional_context,
            "research": convert_tools_messages_to_ai_and_human(
                state.atomic_implementation_research
            ),
            "file_path": snapshot.path,
        }
        if snapshot.exists:
            values["file_content"] = snapshot.content
            model_output = runtime.propose_existing_file_edit(values)
        else:
            model_output = runtime.propose_new_file(values)
        result = runtime.edit_executor().apply(snapshot, model_output)
        return {"last_edit_result": result}

    research_tool_node = ToolNode(
        tools, messages_key="atomic_implementation_research"
    )
    workflow = StateGraph(SoftwareDeveloperState)
    workflow.add_node("start_implementing", start_implementing)
    workflow.add_node("prepare_for_implementation", prepare_for_implementation)
    workflow.add_node(
        "get_clear_implementation_plan_for_atomic_task",
        get_clear_implementation_plan_for_atomic_task,
    )
    workflow.add_node("research_tool_node", research_tool_node)
    workflow.add_node("creating_diffs_for_task", creating_diffs_for_task)
    workflow.add_node(
        "proceed_to_next_atomic_task", proceed_to_next_atomic_task
    )

    workflow.add_edge(START, "start_implementing")
    workflow.add_conditional_edges(
        "start_implementing",
        is_implementation_complete,
        {"continue": "prepare_for_implementation", END: END},
    )
    workflow.add_conditional_edges(
        "prepare_for_implementation",
        should_continue_after_preparation,
        {
            "continue": "get_clear_implementation_plan_for_atomic_task",
            END: END,
        },
    )
    workflow.add_conditional_edges(
        "get_clear_implementation_plan_for_atomic_task",
        should_continue_implementation_research,
        {
            "should_continue_research": "research_tool_node",
            "implement_plan": "creating_diffs_for_task",
        },
    )
    workflow.add_edge(
        "research_tool_node", "get_clear_implementation_plan_for_atomic_task"
    )
    workflow.add_conditional_edges(
        "creating_diffs_for_task",
        should_advance_after_edit,
        {"advance": "proceed_to_next_atomic_task", END: END},
    )
    workflow.add_conditional_edges(
        "proceed_to_next_atomic_task",
        is_implementation_complete,
        {"continue": "prepare_for_implementation", END: END},
    )
    return workflow.compile().with_config({"tags": ["developer-agent-v4"]})


swe_developer = create_developer_workflow()
