"""Developer graph that stages each file in memory before one final commit."""

from collections.abc import Sequence
from dataclasses import replace
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
from agent.editing import (
    EditErrorCode,
    EditResult,
    EditStatus,
    RecoveryResult,
    RecoveryStatus,
    WorkspaceSnapshot,
    WriteIntent,
)
from agent.editing.text import sha256
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools
from agent.runtime import DurableBudgetBoundary
from agent.runtime.budget import BudgetErrorCode
from agent.runtime.secrets import KnownSecretFilter
from agent.runtime.trajectory import EventRecorder
from agent.workspace import WorkspaceAccessPolicy, workspace_access_scope

_RECOVERY_COMMIT_ERRORS = frozenset(
    {
        EditErrorCode.WORKSPACE_NOT_FOUND,
        EditErrorCode.WORKSPACE_INVALID,
        EditErrorCode.PATH_INVALID,
        EditErrorCode.READ_FAILED,
        EditErrorCode.FILE_NOT_FOUND,
        EditErrorCode.FILE_EXISTS,
        EditErrorCode.HASH_MISMATCH,
        EditErrorCode.WRITE_FAILED,
        EditErrorCode.READ_DENIED,
        EditErrorCode.WRITE_DENIED,
        EditErrorCode.RECOVERY_CONFLICT,
    }
)


def create_developer_workflow(
    runtime: DeveloperRuntime | None = None,
    *,
    research_tools: Sequence[Any] | None = None,
    budget_boundary: DurableBudgetBoundary | None = None,
    secret_filter: KnownSecretFilter | None = None,
    event_recorder: EventRecorder | None = None,
    access_policy: WorkspaceAccessPolicy | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
):
    runtime = runtime or default_developer_runtime()
    tools = list(
        search_tools + codemap_tools if research_tools is None else research_tools
    )

    def prepare_model_values(
        state: SoftwareDeveloperState, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if budget_boundary is None:
            return values, {}
        timeout, deadline_update = budget_boundary.model_dispatch_timeout(state)
        if deadline_update:
            return None, deadline_update
        assert timeout is not None
        values["_model_request_timeout_seconds"] = timeout
        return values, {}

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
        check_write = getattr(runtime.edit_executor(), "check_write", None)
        if callable(check_write) and state.active_implementation_plan is not None:
            for task in state.active_implementation_plan.tasks:
                denied = check_write(task.file_path)
                if denied is not None:
                    return {
                        **result,
                        **failed_edit(denied),
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
        values, deadline_update = prepare_model_values(state, values)
        if deadline_update:
            return deadline_update
        assert values is not None
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
        values, deadline_update = prepare_model_values(state, values)
        if deadline_update:
            return deadline_update
        assert values is not None
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
            if staged.edit_result.status is EditStatus.NOOP:
                updated = replace(
                    transaction, task_ids=staged.edit_result.task_ids
                )
                return {
                    **budget_update,
                    "current_file_transaction": updated,
                    "current_file_snapshot": WorkspaceSnapshot(
                        path=updated.path,
                        exists=updated.existed,
                        content=updated.working_content,
                        content_hash=updated.base_hash,
                    ),
                    "current_file_content": updated.working_content,
                    "last_edit_result": None,
                }
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
        pending_write = state.pending_write
        recovery = None
        if budget_boundary is not None:
            if not isinstance(pending_write, WriteIntent):
                return {
                    **invalid_state(
                        "The Developer has no valid durable write intent to reconcile."
                    ),
                    "runtime_error_code": BudgetErrorCode.RECOVERY_CONFLICT,
                    "runtime_message": (
                        "A durable file transaction was reached without its "
                        "checkpointed write intent."
                    ),
                }
            recovery = runtime.edit_executor().reconcile(
                transaction, pending_write
            )
            if recovery.status is RecoveryStatus.CONFLICT:
                rejected = EditResult(
                    status=EditStatus.REJECTED,
                    path=transaction.path,
                    error_code=EditErrorCode.RECOVERY_CONFLICT,
                    message=recovery.message,
                    before_hash=recovery.current_hash,
                    task_ids=transaction.task_ids,
                )
                return {
                    **failed_edit(rejected),
                    "last_recovery_result": recovery,
                    "runtime_error_code": BudgetErrorCode.RECOVERY_CONFLICT,
                    "runtime_message": recovery.message,
                }
            if recovery.status is RecoveryStatus.ALREADY_APPLIED:
                result = recovery.edit_result
                if result is None or result.status is not EditStatus.APPLIED:
                    return {
                        **invalid_state(
                            "The durable recovery result was not an applied edit."
                        ),
                        "last_recovery_result": recovery,
                        "runtime_error_code": BudgetErrorCode.RECOVERY_CONFLICT,
                        "runtime_message": (
                            "The durable recovery result was invalid."
                        ),
                    }
                deadline_update = budget_boundary.check_deadline(
                    state,
                    message=(
                        "The file was already applied before recovery; later "
                        "side effects were blocked by the absolute deadline."
                    ),
                )
                return {
                    "last_edit_result": result,
                    "last_recovery_result": recovery,
                    "current_file_transaction": None,
                    **deadline_update,
                }
        if budget_boundary is not None:
            deadline_update = budget_boundary.check_deadline(
                state,
                message=(
                    "The absolute run deadline expired before file commit; "
                    "no write was attempted."
                ),
            )
            if deadline_update:
                return deadline_update
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
            update = {
                **failed_edit(result),
                "last_recovery_result": (
                    recovery if budget_boundary is not None else None
                ),
                **deadline_update,
            }
            if (
                budget_boundary is not None
                and result.error_code in _RECOVERY_COMMIT_ERRORS
            ):
                conflict = RecoveryResult(
                    status=RecoveryStatus.CONFLICT,
                    path=transaction.path,
                    current_hash=result.before_hash,
                    expected_after_hash=(
                        pending_write.expected_after_hash
                        if pending_write is not None
                        else None
                    ),
                    error_code=EditErrorCode.RECOVERY_CONFLICT,
                    message=result.message,
                )
                update.update(
                    {
                        "last_recovery_result": conflict,
                        "runtime_error_code": BudgetErrorCode.RECOVERY_CONFLICT,
                        "runtime_message": result.message,
                    }
                )
            return update
        return {
            "last_edit_result": result,
            "last_recovery_result": (
                RecoveryResult(
                    status=RecoveryStatus.SAFE_TO_APPLY,
                    path=pending_write.path,
                    current_hash=pending_write.before_hash,
                    expected_after_hash=pending_write.expected_after_hash,
                    edit_result=result,
                )
                if pending_write is not None
                else None
            ),
            "current_file_transaction": None,
            **deadline_update,
        }

    def prepare_write_intent(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        """Checkpoint a stable write intent before the first filesystem write."""
        transaction = state.current_file_transaction
        if transaction is None:
            return invalid_state("The Developer has no transaction to prepare.")
        if budget_boundary is None:
            return {}
        deadline_update = budget_boundary.check_deadline(
            state,
            message=(
                "The absolute run deadline expired before write intent "
                "persistence; no write was attempted."
            ),
        )
        if deadline_update:
            return deadline_update
        existing = state.pending_write
        if existing is not None:
            if existing.path == transaction.path and existing.task_ids == transaction.task_ids:
                return {"last_recovery_result": None}
            return {
                **invalid_state("A different pending write intent is active."),
                "runtime_error_code": BudgetErrorCode.RECOVERY_CONFLICT,
                "runtime_message": "A different durable write intent is active.",
            }
        if transaction.existed and (
            transaction.original_content.encode("utf-8")
            == transaction.working_content.encode("utf-8")
        ):
            noop_result = runtime.edit_executor().commit(transaction)
            if noop_result.status is not EditStatus.NOOP:
                return failed_edit(noop_result)
            return {
                "last_edit_result": noop_result,
                "current_file_transaction": None,
                "pending_write": None,
                "last_recovery_result": None,
            }
        logical_task_id = task_id or f"task-{state.current_task_idx + 1}"
        intent = WriteIntent.create(
            run_id=run_id or budget_boundary.run_id,
            task_id=logical_task_id,
            task_index=state.current_task_idx,
            repair_attempt=state.repair_attempts,
            transaction=transaction,
            expected_after_hash=sha256(
                transaction.working_content.encode("utf-8")
            ),
        )
        return {
            "pending_write": intent,
            "last_recovery_result": None,
        }

    def clear_pending_write(
        state: SoftwareDeveloperState,
    ) -> dict[str, Any]:
        if state.last_edit_result is None or state.last_edit_result.status is not EditStatus.APPLIED:
            return {
                **invalid_state("A pending write can only clear after an applied edit."),
                "runtime_error_code": BudgetErrorCode.RECOVERY_CONFLICT,
                "runtime_message": "A pending write was cleared without an applied edit.",
            }
        return {"pending_write": None}

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
        if (
            state.runtime_error_code is not None
            and state.durable_call_result is None
        ):
            return "end"
        return "settle" if state.durable_call_result is not None else "record"

    def route_model_to_settle(state: SoftwareDeveloperState) -> str:
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
        if access_policy is not None:
            original = wrapped

            def policy_wrapped(*args, **kwargs):
                with workspace_access_scope(access_policy):
                    if callable(original):
                        return original(*args, **kwargs)
                    return original.invoke(*args, **kwargs)

            wrapped = policy_wrapped
        if event_recorder is not None and event_type is not None:
            return event_recorder.wrap_node(
                wrapped,
                event_type=event_type,
                node_name=node_name or event_type,
            )
        return wrapped

    workflow = StateGraph(SoftwareDeveloperState)
    workflow.add_node("start_implementing", persist_node(validate_and_start))
    workflow.add_node(
        "prepare_for_implementation", persist_node(prepare_for_implementation)
    )
    workflow.add_node(
        "get_clear_implementation_plan_for_atomic_task",
        persist_node(
            get_clear_implementation_plan_for_atomic_task,
            event_type="model",
            node_name="get_clear_implementation_plan_for_atomic_task",
        ),
    )
    workflow.add_node(
        "research_tool_node",
        persist_node(
            dispatch_research_tools
            if budget_boundary is not None
            else research_tool_node,
            event_type="tool",
            node_name="research_tools",
        ),
    )
    workflow.add_node(
        "stage_diff_for_task",
        persist_node(
            stage_diff_for_task,
            event_type="model",
            node_name="stage_diff_for_task",
        ),
    )
    workflow.add_node(
        "proceed_to_next_atomic_task", persist_node(proceed_to_next_atomic_task)
    )
    workflow.add_node(
        "commit_file_transaction",
        persist_node(
            commit_file_transaction,
            event_type="edit",
            node_name="commit_file_transaction",
        ),
    )
    if budget_boundary is not None:
        workflow.add_node(
            "prepare_write_intent", persist_node(prepare_write_intent)
        )
        workflow.add_node(
            "clear_pending_write", persist_node(clear_pending_write)
        )
    workflow.add_node(
        "proceed_to_next_task", persist_node(proceed_to_next_task)
    )
    workflow.add_node("finish_implementation", persist_node(finish_implementation))

    if budget_boundary is not None:
        for name in (
            "get_clear_implementation_plan_for_atomic_task",
            "stage_diff_for_task",
        ):
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
        workflow.add_node("reserve_research_tools", persist_node(reserve_tools))
        workflow.add_node(
            "record_research_tool_results", persist_node(record_tool_results)
        )
        workflow.add_node("settle_research_tools", persist_node(settle_call))
        workflow.add_node(
            "check_deadline_before_commit",
            persist_node(check_deadline_before_commit),
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
                "end": END,
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
                "end"
                if state.runtime_error_code is not None
                else "prepare"
            ),
            {"prepare": "prepare_write_intent", "end": END},
        )
        workflow.add_conditional_edges(
            "prepare_write_intent",
            lambda state: (
                "end"
                if state.runtime_error_code is not None
                or state.last_edit_result is not None
                else "commit"
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
            {
                "advance": "clear_pending_write",
                END: END,
                "end": END,
            },
        )
        workflow.add_edge("clear_pending_write", "proceed_to_next_task")
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
