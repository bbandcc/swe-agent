"""Developer graph that stages each file in memory before one final commit."""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig
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
from agent.runtime import DurableBudgetBoundary


def create_developer_workflow(
    runtime: DeveloperRuntime | None = None,
    *,
    research_tools: Sequence[Any] | None = None,
    budget_boundary: DurableBudgetBoundary | None = None,
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
    ) -> dict[str, Any]:
        plan = state.active_implementation_plan
        transaction = state.current_file_transaction
        if plan is None or transaction is None:
            return {"atomic_implementation_research": []}
        current_task = plan.tasks[state.current_task_idx]
        current_atomic_task = current_task.atomic_tasks[
            state.current_atomic_task_idx
        ]
        values = {
            "development_task": current_atomic_task.atomic_task,
            "file_content": transaction.working_content,
            "target_file": transaction.path,
            "codebase_structure": state.codebase_structure,
            "additional_context": current_atomic_task.additional_context,
            "atomic_implementation_research": (
                state.atomic_implementation_research
            ),
        }
        if budget_boundary is not None:
            deadline_update = budget_boundary.guard_dispatch(state)
            if deadline_update:
                return deadline_update
        result = runtime.research_atomic_task(values)
        budget_update: dict[str, Any] = {}
        if budget_boundary is not None:
            result, budget_update = budget_boundary.capture_model(state, result)
            if result is None:
                return budget_update
        return {
            **budget_update,
            "atomic_implementation_research": [result],
        }

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
        if budget_boundary is not None:
            deadline_update = budget_boundary.guard_dispatch(state)
            if deadline_update:
                return deadline_update
        if transaction.existed or transaction.task_ids:
            model_output = runtime.propose_existing_file_edit(values)
        else:
            model_output = runtime.propose_new_file(values)
        budget_update: dict[str, Any] = {}
        if budget_boundary is not None:
            model_output, budget_update = budget_boundary.capture_model(
                state, model_output
            )
            if model_output is None:
                return budget_update
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
            return {**budget_update, **failed_edit(staged.edit_result)}
        updated = staged.transaction
        assert updated is not None
        return {
            **budget_update,
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
        deadline_update = (
            budget_boundary.check_deadline(
                state,
                message=(
                    "The file commit completed after the absolute run deadline; "
                    "the applied result was preserved and later side effects "
                    "were blocked."
                ),
            )
            if budget_boundary is not None
            else {}
        )
        if result.status is not EditStatus.APPLIED:
            return {**failed_edit(result), **deadline_update}
        return {
            "last_edit_result": result,
            "current_file_transaction": None,
            **deadline_update,
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

    def reserve_model(name: str):
        def reserve(state: SoftwareDeveloperState) -> dict[str, Any]:
            assert budget_boundary is not None
            transaction = state.current_file_transaction
            request = {
                "node": name,
                "task_index": state.current_task_idx,
                "atomic_task_index": state.current_atomic_task_idx,
                "file_path": transaction.path if transaction else None,
                "file_hash": transaction.base_hash if transaction else None,
            }
            return budget_boundary.reserve_model(state, name, request)
        return reserve

    def settle_call(state: SoftwareDeveloperState) -> dict[str, Any]:
        assert budget_boundary is not None
        return budget_boundary.settle(state)

    def reserve_tools(state: SoftwareDeveloperState) -> dict[str, Any]:
        assert budget_boundary is not None
        calls = state.atomic_implementation_research[-1].tool_calls
        return budget_boundary.reserve_tools(state, calls)

    def record_tool_results(state: SoftwareDeveloperState) -> dict[str, Any]:
        assert budget_boundary is not None
        return budget_boundary.record_tool_results(state)

    def check_deadline_before_commit(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        assert budget_boundary is not None
        return budget_boundary.check_deadline(
            state,
            message=(
                "The absolute run deadline expired before file commit; "
                "no write was attempted."
            ),
        )

    research_tool_node = ToolNode(
        tools, messages_key="atomic_implementation_research"
    )

    def dispatch_research_tools(
        state: SoftwareDeveloperState, config: RunnableConfig
    ) -> dict[str, Any]:
        assert budget_boundary is not None
        deadline_update = budget_boundary.guard_dispatch(state)
        if deadline_update:
            return deadline_update
        return research_tool_node.invoke(state, config)

    def route_after_tool_dispatch(state: SoftwareDeveloperState) -> str:
        return "settle" if state.durable_call_result is not None else "record"
    workflow = StateGraph(SoftwareDeveloperState)
    workflow.add_node("start_implementing", validate_and_start)
    workflow.add_node("prepare_for_implementation", prepare_for_implementation)
    workflow.add_node(
        "get_clear_implementation_plan_for_atomic_task",
        get_clear_implementation_plan_for_atomic_task,
    )
    workflow.add_node(
        "research_tool_node",
        (
            dispatch_research_tools
            if budget_boundary is not None
            else research_tool_node
        ),
    )
    workflow.add_node("stage_diff_for_task", stage_diff_for_task)
    workflow.add_node(
        "proceed_to_next_atomic_task", proceed_to_next_atomic_task
    )
    workflow.add_node("commit_file_transaction", commit_file_transaction)
    workflow.add_node("proceed_to_next_task", proceed_to_next_task)
    workflow.add_node("finish_implementation", finish_implementation)

    if budget_boundary is not None:
        for name in (
            "get_clear_implementation_plan_for_atomic_task",
            "stage_diff_for_task",
        ):
            workflow.add_node(f"reserve_{name}", reserve_model(name))
            workflow.add_node(f"settle_{name}", settle_call)
            workflow.add_conditional_edges(
                f"reserve_{name}",
                budget_boundary.may_dispatch,
                {"dispatch": name, "end": END},
            )
            workflow.add_edge(name, f"settle_{name}")
        workflow.add_node("reserve_research_tools", reserve_tools)
        workflow.add_node("record_research_tool_results", record_tool_results)
        workflow.add_node("settle_research_tools", settle_call)
        workflow.add_node(
            "check_deadline_before_commit", check_deadline_before_commit
        )
        workflow.add_conditional_edges(
            "reserve_research_tools",
            budget_boundary.may_dispatch,
            {"dispatch": "research_tool_node", "end": END},
        )
        workflow.add_conditional_edges(
            "research_tool_node",
            route_after_tool_dispatch,
            {
                "record": "record_research_tool_results",
                "settle": "settle_research_tools",
            },
        )
        workflow.add_edge("record_research_tool_results", "settle_research_tools")

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
                "continue": "reserve_get_clear_implementation_plan_for_atomic_task",
                END: END,
            },
        )
        workflow.add_conditional_edges(
            "settle_get_clear_implementation_plan_for_atomic_task",
            lambda state: (
                "end"
                if state.runtime_error_code is not None
                else should_continue_implementation_research(state)
            ),
            {
                "should_continue_research": "reserve_research_tools",
                "implement_plan": "reserve_stage_diff_for_task",
                "end": END,
            },
        )
        workflow.add_conditional_edges(
            "settle_research_tools",
            lambda state: (
                "end" if state.runtime_error_code is not None else "continue"
            ),
            {
                "continue": "reserve_get_clear_implementation_plan_for_atomic_task",
                "end": END,
            },
        )
        workflow.add_conditional_edges(
            "settle_stage_diff_for_task",
            lambda state: (
                "end"
                if state.runtime_error_code is not None
                else route_after_staging(state)
            ),
            {
                "next_atomic": "proceed_to_next_atomic_task",
                "commit": "check_deadline_before_commit",
                END: END,
                "end": END,
            },
        )
        workflow.add_edge(
            "proceed_to_next_atomic_task",
            "reserve_get_clear_implementation_plan_for_atomic_task",
        )
        workflow.add_conditional_edges(
            "check_deadline_before_commit",
            lambda state: (
                "end" if state.runtime_error_code is not None else "commit"
            ),
            {"commit": "commit_file_transaction", "end": END},
        )
        workflow.add_conditional_edges(
            "commit_file_transaction",
            lambda state: (
                "end"
                if state.runtime_error_code is not None
                else route_after_commit(state)
            ),
            {"advance": "proceed_to_next_task", END: END, "end": END},
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
