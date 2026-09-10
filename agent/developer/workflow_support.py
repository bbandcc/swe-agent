"""Deterministic validation, routing, and message adaptation for Developer."""

import json
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.constants import END

from agent.common.entities import PlanStatus
from agent.developer.state import (
    DeveloperErrorCode,
    DeveloperStatus,
    SoftwareDeveloperState,
)
from agent.editing import EditResult, EditStatus


def start_implementing(state: SoftwareDeveloperState) -> dict[str, Any]:
    plan = state.implementation_plan
    error: str | None = None
    status = DeveloperStatus.RUNNING
    if plan is None:
        error = "The Developer requires an implementation plan."
    elif plan.status is PlanStatus.NO_CHANGES:
        if plan.tasks or not plan.no_change_reason.strip():
            error = (
                "A no-change plan must contain no tasks and provide a reason."
            )
        else:
            status = DeveloperStatus.NO_CHANGES
    elif not plan.tasks:
        error = (
            "An empty task list is invalid unless status is explicitly no_changes."
        )
    elif any(not task.atomic_tasks for task in plan.tasks):
        error = "Every implementation task must contain at least one atomic task."
    return {
        "current_task_idx": 0,
        "current_atomic_task_idx": 0,
        "current_file_transaction": None,
        "last_edit_result": None,
        "developer_status": (
            DeveloperStatus.FAILED if error is not None else status
        ),
        "developer_error_code": (
            DeveloperErrorCode.INVALID_PLAN if error is not None else None
        ),
        "developer_message": (
            error or (plan.no_change_reason if plan is not None else "")
        ),
    }


def should_start(state: SoftwareDeveloperState):
    if state.developer_status is DeveloperStatus.RUNNING:
        return "continue"
    return END


def should_continue_implementation_research(state: SoftwareDeveloperState) -> str:
    last_research_step = state.atomic_implementation_research[-1]
    if last_research_step.tool_calls:
        return "should_continue_research"
    return "implement_plan"


def should_continue_after_preparation(state: SoftwareDeveloperState):
    if state.developer_status is DeveloperStatus.FAILED:
        return END
    return "continue"


def route_after_staging(state: SoftwareDeveloperState):
    if state.developer_status is DeveloperStatus.FAILED:
        return END
    plan = state.implementation_plan
    if plan is None:
        return END
    task = plan.tasks[state.current_task_idx]
    if state.current_atomic_task_idx >= len(task.atomic_tasks) - 1:
        return "commit"
    return "next_atomic"


def route_after_commit(state: SoftwareDeveloperState):
    result = state.last_edit_result
    if result is not None and result.status is EditStatus.APPLIED:
        return "advance"
    return END


def route_after_task_advance(state: SoftwareDeveloperState):
    plan = state.implementation_plan
    if plan is not None and state.current_task_idx < len(plan.tasks):
        return "continue"
    return "complete"


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


def failed_edit(result: EditResult) -> dict[str, Any]:
    return {
        "last_edit_result": result,
        "developer_status": DeveloperStatus.FAILED,
        "developer_error_code": None,
        "developer_message": result.message,
    }


def invalid_state(message: str) -> dict[str, Any]:
    return {
        "developer_status": DeveloperStatus.FAILED,
        "developer_error_code": DeveloperErrorCode.INVALID_STATE,
        "developer_message": message,
    }
