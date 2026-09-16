"""Synchronous SQLite start/resume entry points for durable local runs."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from agent.architect.graph import create_architect_workflow
from agent.architect.runtime import durable_architect_runtime
from agent.developer.graph import create_developer_workflow
from agent.developer.runtime import durable_developer_runtime
from agent.graph import create_workflow_graph
from agent.runtime.boundary import DurableBudgetBoundary
from agent.runtime.budget import BudgetErrorCode, BudgetSnapshot
from agent.runtime.checkpointing import (
    GraphCheckpointLookup,
    checkpoint_serializer,
    mark_uncertain_dispatch,
)
from agent.runtime.config import RunConfig
from agent.runtime.identity import (
    PreflightResult,
    ResumeRequest,
    StartRequest,
    WorkspaceIdentity,
    preflight_resume,
    preflight_start,
)
from agent.runtime.semantics import semantic_config_digest
from agent.workspace import workspace_root_scope

GraphFactory = Callable[[RunConfig, str, SqliteSaver, Callable[[], float]], Any]


class DurableRunStatus(str, Enum):
    COMPLETED = "completed"
    PAUSED = "paused"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DurableRunResult:
    status: DurableRunStatus
    state: Mapping[str, Any] | None = None
    error_code: str | None = None
    message: str = ""
    preflight: PreflightResult | None = None

    @property
    def accepted(self) -> bool:
        return self.status not in {
            DurableRunStatus.REJECTED,
            DurableRunStatus.FAILED,
        }


def durable_recursion_limit(max_steps: int) -> int:
    if (
        isinstance(max_steps, bool)
        or not isinstance(max_steps, int)
        or max_steps <= 0
    ):
        raise ValueError("max_steps must be a positive integer.")
    return max(200, 8 * max_steps + 64)


def start_run(
    config: RunConfig,
    request: StartRequest,
    initial_state: Mapping[str, Any],
    *,
    graph_factory: GraphFactory | None = None,
    clock: Callable[[], float] = time.time,
) -> DurableRunResult:
    """Start a new durable thread after an SQLite-backed identity preflight."""
    return _run(
        config,
        request,
        initial_state,
        resume=False,
        graph_factory=graph_factory,
        clock=clock,
    )


def resume_run(
    config: RunConfig,
    request: ResumeRequest,
    *,
    graph_factory: GraphFactory | None = None,
    clock: Callable[[], float] = time.time,
) -> DurableRunResult:
    """Resume only a matching checkpoint; never turns a miss into a new run."""
    return _run(
        config,
        request,
        None,
        resume=True,
        graph_factory=graph_factory,
        clock=clock,
    )


def _run(
    config: RunConfig,
    request: StartRequest | ResumeRequest,
    initial_state: Mapping[str, Any] | None,
    *,
    resume: bool,
    graph_factory: GraphFactory | None,
    clock: Callable[[], float],
) -> DurableRunResult:
    if request.run_config_digest != semantic_config_digest(config):
        return DurableRunResult(
            DurableRunStatus.REJECTED,
            error_code="run_config_mismatch",
            message="The request digest does not match the supplied RunConfig.",
        )
    if request.identity.workspace != WorkspaceIdentity.from_root(config.workspace_root):
        return DurableRunResult(
            DurableRunStatus.REJECTED,
            error_code="workspace_mismatch",
            message="The request workspace does not match the supplied RunConfig.",
        )
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    database = config.runtime_root / "checkpoints.sqlite"
    connection = sqlite3.connect(database, check_same_thread=False)
    try:
        saver = SqliteSaver(connection, serde=checkpoint_serializer())
        saver.setup()
        factory = graph_factory or create_durable_workflow
        graph = factory(config, request.identity.run_id, saver, clock)
        thread_config = _thread_config(request.identity.thread_id, config.max_steps)
        lookup = GraphCheckpointLookup(graph, thread_config)
        preflight = (
            preflight_resume(request, lookup)
            if resume
            else preflight_start(request, lookup)
        )
        if not preflight.accepted:
            return DurableRunResult(
                DurableRunStatus.REJECTED,
                error_code=(
                    preflight.error_code.value if preflight.error_code else None
                ),
                message=preflight.message,
                preflight=preflight,
            )

        if resume:
            uncertain = mark_uncertain_dispatch(graph, thread_config)
            if uncertain is not None:
                return DurableRunResult(
                    DurableRunStatus.REJECTED,
                    state=uncertain,
                    error_code=BudgetErrorCode.OUTCOME_UNKNOWN.value,
                    message=(
                        "An in-flight external call has no durable result; "
                        "it will not be replayed."
                    ),
                    preflight=preflight,
                )
            graph_input = None
        else:
            assert initial_state is not None
            graph_input = {
                **initial_state,
                "run_identity": request.identity,
                "run_config_digest": request.run_config_digest,
                "agent_revision": request.agent_revision,
                "budget": BudgetSnapshot.create(
                    max_steps=config.max_steps,
                    max_cost_usd=config.max_cost_usd,
                    deadline_at=clock() + config.timeout_seconds,
                ),
            }
        with workspace_root_scope(config.workspace_root):
            result = graph.invoke(
                graph_input,
                thread_config,
                durability="sync",
            )
        snapshot = graph.get_state(thread_config)
        runtime_error = (
            result.get("runtime_error_code")
            if isinstance(result, Mapping)
            else None
        )
        if runtime_error is not None:
            code = (
                runtime_error.value
                if hasattr(runtime_error, "value")
                else str(runtime_error)
            )
            return DurableRunResult(
                DurableRunStatus.FAILED,
                state=result,
                error_code=code,
                message=str(result.get("runtime_message", "")),
                preflight=preflight,
            )
        status = DurableRunStatus.PAUSED if snapshot.next else DurableRunStatus.COMPLETED
        return DurableRunResult(status, state=result, preflight=preflight)
    except Exception as error:
        return DurableRunResult(
            DurableRunStatus.FAILED,
            error_code="graph_execution_failed",
            message=str(error),
        )
    finally:
        connection.close()


def create_durable_workflow(
    config: RunConfig,
    run_id: str,
    saver: SqliteSaver,
    clock: Callable[[], float] = time.time,
):
    """Compile the parent with a saver; child graphs inherit it by default."""
    boundary = DurableBudgetBoundary(run_id, clock=clock)
    architect = create_architect_workflow(
        durable_architect_runtime(
            config.model, config.model_max_output_tokens, config.pricing
        ),
        budget_boundary=boundary,
    )
    developer = create_developer_workflow(
        durable_developer_runtime(
            config.model,
            config.model_max_output_tokens,
            config.pricing,
            workspace_root=config.workspace_root,
        ),
        budget_boundary=boundary,
    )
    return create_workflow_graph(
        architect=architect,
        developer=developer,
        verification_specs=config.verification_specs,
        workspace_root=config.workspace_root,
        durable_runtime=True,
    ).compile(checkpointer=saver).with_config({"tags": ["agent-durable-v1"]})


def _thread_config(thread_id: str, max_steps: int) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": durable_recursion_limit(max_steps),
    }
