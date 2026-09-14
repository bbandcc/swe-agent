"""Developer graph that stages each file in memory before one final commit."""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AnyMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from agent.developer.runtime import DeveloperRuntime, default_developer_runtime
from agent.developer.state import (
    DeveloperErrorCode,
    DeveloperStatus,
    SoftwareDeveloperState,
)
from agent.developer.workflow_support import (
    convert_tools_messages_to_ai_and_human,
    duplicate_file_task_error,
    failed_edit,
    invalid_state,
    route_after_commit,
    route_after_staging,
    route_after_task_advance,
    should_continue_after_preparation,
    should_continue_implementation_research,
    should_start,
    start_implementing,
)
from agent.editing import EditResult, EditStatus, WorkspaceSnapshot
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools


def create_developer_workflow(
    runtime: DeveloperRuntime | None = None,
    *,
    research_tools: Sequence[Any] | None = None,
):
    runtime = runtime or default_developer_runtime()
    tools = list(
        search_tools + codemap_tools if research_tools is None else research_tools
    )

    def validate_and_start(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        result = start_implementing(state)
        if result["developer_status"] is not DeveloperStatus.RUNNING:
            return result
        duplicate_error = duplicate_file_task_error(
            state, runtime.edit_executor().canonical_plan_path
        )
        if duplicate_error is not None:
            return {
                **result,
                "developer_status": DeveloperStatus.FAILED,
                "developer_error_code": DeveloperErrorCode.INVALID_PLAN,
                "developer_message": duplicate_error,
            }
        return result

    def prepare_for_implementation(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        plan = state.active_implementation_plan
        if plan is None:
            return invalid_state("The Developer has no implementation plan.")
        current_task = plan.tasks[state.current_task_idx]
        started = runtime.edit_executor().begin(current_task.file_path)
        if not started.ok:
            assert started.edit_result is not None
            return {
                **failed_edit(started.edit_result),
                "current_file_snapshot": None,
                "current_file_transaction": None,
            }
        transaction = started.transaction
        assert transaction is not None
        snapshot = WorkspaceSnapshot(
            path=transaction.path,
            exists=transaction.existed,
            content=transaction.working_content,
            content_hash=transaction.base_hash,
        )
        return {
            "current_file_snapshot": snapshot,
            "current_file_transaction": transaction,
            "current_file_content": transaction.working_content,
            "codebase_structure": runtime.load_codebase_structure(),
            "atomic_implementation_research": [],
            "last_edit_result": None,
        }

    def get_clear_implementation_plan_for_atomic_task(
        state: SoftwareDeveloperState,
    ) -> dict[str, list[AnyMessage]]:
        plan = state.active_implementation_plan
        transaction = state.current_file_transaction
        if plan is None or transaction is None:
            return {"atomic_implementation_research": []}
        current_task = plan.tasks[state.current_task_idx]
        current_atomic_task = current_task.atomic_tasks[
            state.current_atomic_task_idx
        ]
        result = runtime.research_atomic_task(
            {
                "development_task": current_atomic_task.atomic_task,
                "file_content": transaction.working_content,
                "target_file": transaction.path,
                "codebase_structure": state.codebase_structure,
                "additional_context": current_atomic_task.additional_context,
                "atomic_implementation_research": (
                    state.atomic_implementation_research
                ),
            }
        )
        return {"atomic_implementation_research": [result]}

    def stage_diff_for_task(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        plan = state.active_implementation_plan
        transaction = state.current_file_transaction
        if plan is None or transaction is None:
            return invalid_state(
                "The Developer has no active workspace transaction."
            )
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
            "file_path": transaction.path,
            "file_content": transaction.working_content,
            "verification_feedback": state.verification_feedback or {},
        }
        if transaction.existed or transaction.task_ids:
            model_output = runtime.propose_existing_file_edit(values)
        else:
            model_output = runtime.propose_new_file(values)
        repair_prefix = (
            f"repair-{state.repair_attempts}." if state.repair_attempts else ""
        )
        task_id = repair_prefix + (
            f"task-{state.current_task_idx + 1}."
            f"step-{state.current_atomic_task_idx + 1}"
        )
        staged = runtime.edit_executor().stage(
            transaction, model_output, task_id=task_id
        )
        if not staged.ok:
            assert staged.edit_result is not None
            return failed_edit(staged.edit_result)
        updated = staged.transaction
        assert updated is not None
        return {
            "current_file_transaction": updated,
            "current_file_snapshot": WorkspaceSnapshot(
                path=updated.path,
                exists=True,
                content=updated.working_content,
                content_hash=updated.base_hash,
            ),
            "current_file_content": updated.working_content,
            "last_edit_result": None,
        }

    def proceed_to_next_atomic_task(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        return {
            "current_atomic_task_idx": state.current_atomic_task_idx + 1,
            "atomic_implementation_research": [],
        }

    def commit_file_transaction(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        transaction = state.current_file_transaction
        if transaction is None:
            return invalid_state("The Developer has no transaction to commit.")
        result = runtime.edit_executor().commit(transaction)
        if result.status is not EditStatus.APPLIED:
            return failed_edit(result)
        return {
            "last_edit_result": result,
            "current_file_transaction": None,
        }

    def proceed_to_next_task(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        return {
            "current_task_idx": state.current_task_idx + 1,
            "current_atomic_task_idx": 0,
            "current_file_snapshot": None,
            "current_file_content": None,
            "atomic_implementation_research": [],
        }

    def finish_implementation(_: SoftwareDeveloperState) -> dict[str, Any]:
        return {
            "developer_status": DeveloperStatus.COMPLETED,
            "developer_error_code": None,
            "developer_message": "Implementation completed.",
        }

    research_tool_node = ToolNode(
        tools, messages_key="atomic_implementation_research"
    )
    workflow = StateGraph(SoftwareDeveloperState)
    workflow.add_node("start_implementing", validate_and_start)
    workflow.add_node("prepare_for_implementation", prepare_for_implementation)
    workflow.add_node(
        "get_clear_implementation_plan_for_atomic_task",
        get_clear_implementation_plan_for_atomic_task,
    )
    workflow.add_node("research_tool_node", research_tool_node)
    workflow.add_node("stage_diff_for_task", stage_diff_for_task)
    workflow.add_node(
        "proceed_to_next_atomic_task", proceed_to_next_atomic_task
    )
    workflow.add_node("commit_file_transaction", commit_file_transaction)
    workflow.add_node("proceed_to_next_task", proceed_to_next_task)
    workflow.add_node("finish_implementation", finish_implementation)

    workflow.add_edge(START, "start_implementing")
    workflow.add_conditional_edges(
        "start_implementing",
        should_start,
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
            "implement_plan": "stage_diff_for_task",
        },
    )
    workflow.add_edge(
        "research_tool_node", "get_clear_implementation_plan_for_atomic_task"
    )
    workflow.add_conditional_edges(
        "stage_diff_for_task",
        route_after_staging,
        {
            "next_atomic": "proceed_to_next_atomic_task",
            "commit": "commit_file_transaction",
            END: END,
        },
    )
    workflow.add_edge(
        "proceed_to_next_atomic_task",
        "get_clear_implementation_plan_for_atomic_task",
    )
    workflow.add_conditional_edges(
        "commit_file_transaction",
        route_after_commit,
        {"advance": "proceed_to_next_task", END: END},
    )
    workflow.add_conditional_edges(
        "proceed_to_next_task",
        route_after_task_advance,
        {
            "continue": "prepare_for_implementation",
            "complete": "finish_implementation",
        },
    )
    workflow.add_edge("finish_implementation", END)
    return workflow.compile().with_config({"tags": ["developer-agent-v5"]})


swe_developer = create_developer_workflow()
