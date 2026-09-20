"""Synchronous SQLite start/resume entry points for durable local runs."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
from agent.runtime.revision import AgentCodeRevision
from agent.runtime.semantics import semantic_config_digest
from agent.runtime.secrets import KnownSecretFilter, SENSITIVE_DATA_MESSAGE
from agent.runtime.trajectory import (
    AuditIncompleteError,
    AuditStatus,
    EventRecorder,
    EventSink,
    EventSinkError,
    JsonlEventSink,
    RunRecord,
    RunEvent,
    write_run_record,
)
from agent.verification import AcceptanceResult
from agent.workspace import WorkspaceRootError, workspace_root_scope

GraphFactory = Callable[[RunConfig, str, SqliteSaver, Callable[[], float]], Any]


class DurableRunStatus(str, Enum):
    COMPLETED = "completed"
    PAUSED = "paused"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Small versioned boundary between a run and external callers."""

    schema_version: int
    runtime_status: DurableRunStatus
    workflow_outcome: str | None
    verification_status: str | None
    error_code: str | None
    run_id: str
    record_ref: str | None = None
    warnings: tuple[str, ...] = ()
    acceptance: AcceptanceResult | None = None
    audit_incomplete: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Unsupported run summary schema version.")
        if not isinstance(self.runtime_status, DurableRunStatus):
            raise ValueError("runtime_status must be DurableRunStatus.")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string.")
        object.__setattr__(self, "run_id", self.run_id.strip())
        if self.record_ref is not None and not isinstance(self.record_ref, str):
            raise ValueError("record_ref must be a string or None.")
        if self.acceptance is not None and not isinstance(
            self.acceptance, AcceptanceResult
        ):
            raise ValueError("acceptance must be AcceptanceResult or None.")
        if not isinstance(self.audit_incomplete, bool):
            raise ValueError("audit_incomplete must be a boolean.")
        if isinstance(self.warnings, (str, bytes)):
            raise ValueError("warnings must contain warning codes.")
        try:
            warning_codes = tuple(self.warnings)
        except TypeError as error:
            raise ValueError("warnings must contain warning codes.") from error
        normalized_warnings: list[str] = []
        for warning in warning_codes:
            code = _warning_code(warning)
            if code is None:
                raise ValueError("warnings must contain non-empty codes.")
            normalized_warnings.append(code)
        object.__setattr__(self, "warnings", tuple(normalized_warnings))

    def to_dict(self) -> dict[str, object | None]:
        return {
            "schema_version": self.schema_version,
            "runtime_status": self.runtime_status.value,
            "workflow_outcome": self.workflow_outcome,
            "verification_status": self.verification_status,
            "error_code": self.error_code,
            "run_id": self.run_id,
            "record_ref": self.record_ref,
            "warnings": list(self.warnings),
            "acceptance": (
                {
                    "accepted": self.acceptance.accepted,
                    "reason": self.acceptance.reason.value,
                }
                if self.acceptance is not None
                else None
            ),
            "audit_incomplete": self.audit_incomplete,
        }


@dataclass(frozen=True, slots=True)
class DurableRunResult:
    status: DurableRunStatus
    state: Mapping[str, Any] | None = None
    error_code: str | None = None
    message: str = ""
    preflight: PreflightResult | None = None
    run_id: str | None = None
    record_ref: str | None = None
    audit_incomplete: bool = False
    audit_error_code: str | None = None

    @property
    def summary(self) -> RunSummary:
        state = self.state or {}
        identity = state.get("run_identity")
        run_id = self.run_id or getattr(identity, "run_id", None) or "unknown"
        return RunSummary(
            schema_version=1,
            runtime_status=self.status,
            workflow_outcome=_summary_value(state.get("outcome")),
            verification_status=_summary_value(
                state.get("verification_status")
            ),
            error_code=_summary_value(self.error_code),
            run_id=run_id,
            record_ref=self.record_ref,
            warnings=_preflight_warning_codes(self.preflight),
            acceptance=_acceptance_value(state.get("acceptance")),
            audit_incomplete=self.audit_incomplete,
        )

    @property
    def accepted(self) -> bool:
        """Return lifecycle acceptance, separate from business outcome."""
        return self.status not in {
            DurableRunStatus.REJECTED,
            DurableRunStatus.FAILED,
        }


def run_exit_code(summary: RunSummary) -> int:
    """Map a public summary to the conservative local CLI exit contract."""
    if summary.runtime_status is DurableRunStatus.PAUSED:
        return 3
    if summary.audit_incomplete:
        return 2
    if (
        summary.runtime_status is DurableRunStatus.REJECTED
        or summary.error_code is not None
    ):
        return 2
    if (
        summary.runtime_status is DurableRunStatus.FAILED
        and summary.workflow_outcome is None
    ):
        return 2
    if (
        summary.runtime_status is DurableRunStatus.COMPLETED
        and summary.workflow_outcome == "completed"
        and summary.acceptance is not None
        and summary.acceptance.accepted
    ):
        return 0
    return 1


def _summary_value(value: object) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else str(value)


def _acceptance_value(value: object) -> AcceptanceResult | None:
    if value is None:
        return None
    if isinstance(value, AcceptanceResult):
        return value
    if isinstance(value, Mapping):
        try:
            from agent.verification import AcceptanceReason

            accepted = value["accepted"]
            if not isinstance(accepted, bool):
                return None
            reason = value.get("reason")
            if isinstance(reason, Enum):
                reason = reason.value
            return AcceptanceResult(
                accepted=accepted,
                reason=AcceptanceReason(str(reason)),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _preflight_warning_codes(
    preflight: PreflightResult | None,
) -> tuple[str, ...]:
    if preflight is None:
        return ()
    codes: list[str] = []
    for warning in preflight.warnings:
        code = _warning_code(warning.code)
        if code is not None:
            codes.append(code)
    return tuple(codes)


def _warning_code(value: object) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


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
    event_sink: EventSink | None = None,
) -> DurableRunResult:
    """Start a new durable thread after an SQLite-backed identity preflight."""
    return _run(
        config,
        request,
        initial_state,
        resume=False,
        graph_factory=graph_factory,
        clock=clock,
        event_sink=event_sink,
    )


def resume_run(
    config: RunConfig,
    request: ResumeRequest,
    *,
    graph_factory: GraphFactory | None = None,
    clock: Callable[[], float] = time.time,
    event_sink: EventSink | None = None,
) -> DurableRunResult:
    """Resume only a matching checkpoint; never turns a miss into a new run."""
    return _run(
        config,
        request,
        None,
        resume=True,
        graph_factory=graph_factory,
        clock=clock,
        event_sink=event_sink,
    )


def _run(
    config: RunConfig,
    request: StartRequest | ResumeRequest,
    initial_state: Mapping[str, Any] | None,
    *,
    resume: bool,
    graph_factory: GraphFactory | None,
    clock: Callable[[], float],
    event_sink: EventSink | None,
) -> DurableRunResult:
    secret_filter = KnownSecretFilter.from_run_config(config)
    if _identity_contains_secret(request.identity, secret_filter):
        return DurableRunResult(
            DurableRunStatus.REJECTED,
            error_code=BudgetErrorCode.SENSITIVE_DATA_DETECTED.value,
            message=SENSITIVE_DATA_MESSAGE,
            run_id=secret_filter.marker,
        )
    if request.run_config_digest != semantic_config_digest(config):
        return DurableRunResult(
            DurableRunStatus.REJECTED,
            error_code="run_config_mismatch",
            message="The request digest does not match the supplied RunConfig.",
            run_id=request.identity.run_id,
        )
    try:
        live_workspace = WorkspaceIdentity.from_root(config.workspace_root)
    except WorkspaceRootError as error:
        return DurableRunResult(
            DurableRunStatus.REJECTED,
            error_code="workspace_error",
            message=secret_filter.redact_text(str(error)),
            run_id=request.identity.run_id,
        )
    if request.identity.workspace != live_workspace:
        return DurableRunResult(
            DurableRunStatus.REJECTED,
            error_code="workspace_mismatch",
            message="The request workspace does not match the supplied RunConfig.",
            run_id=request.identity.run_id,
        )
    try:
        config.runtime_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return DurableRunResult(
            DurableRunStatus.FAILED,
            error_code="runtime_root_error",
            message=secret_filter.redact_text(str(error)),
            run_id=request.identity.run_id,
        )
    try:
        audit_sink = (
            event_sink
            if event_sink is not None
            else JsonlEventSink(
                config.runtime_root,
                secret_filter=secret_filter,
            )
        )
    except EventSinkError as error:
        return DurableRunResult(
            DurableRunStatus.FAILED,
            error_code="audit_error",
            message=secret_filter.redact_text(str(error)),
            run_id=request.identity.run_id,
            audit_incomplete=True,
            audit_error_code=error.code.value,
        )
    started_at = clock()
    database = config.runtime_root / "checkpoints.sqlite"
    try:
        connection = sqlite3.connect(database, check_same_thread=False)
    except (OSError, sqlite3.Error) as error:
        return DurableRunResult(
            DurableRunStatus.FAILED,
            error_code="sqlite_error",
            message=secret_filter.redact_text(str(error)),
            run_id=request.identity.run_id,
        )
    try:
        try:
            saver = SqliteSaver(connection, serde=checkpoint_serializer())
            saver.setup()
        except (OSError, sqlite3.Error) as error:
            return DurableRunResult(
                DurableRunStatus.FAILED,
                error_code="sqlite_error",
                message=secret_filter.redact_text(str(error)),
                run_id=request.identity.run_id,
            )
        try:
            factory = graph_factory or create_durable_workflow
            if graph_factory is None:
                graph = factory(
                    config,
                    request.identity.run_id,
                    saver,
                    clock,
                    event_sink=audit_sink,
                    task_id=request.identity.task_id,
                    thread_id=request.identity.thread_id,
                )
            else:
                graph = factory(config, request.identity.run_id, saver, clock)
            thread_config = _thread_config(
                request.identity.thread_id, config.max_steps
            )
            lookup = GraphCheckpointLookup(graph, thread_config)
            preflight = (
                preflight_resume(request, lookup)
                if resume
                else preflight_start(request, lookup)
            )
            if not preflight.accepted:
                base_result = DurableRunResult(
                    DurableRunStatus.REJECTED,
                    error_code=(
                        preflight.error_code.value
                        if preflight.error_code
                        else None
                    ),
                    message=preflight.message,
                    preflight=preflight,
                    run_id=request.identity.run_id,
                )
                return _finish_audit(
                    base_result,
                    config=config,
                    request=request,
                    sink=audit_sink,
                    started_at=started_at,
                    finished_at=time.time(),
                    secret_filter=secret_filter,
                )

            recorder = EventRecorder(
                audit_sink,
                run_id=request.identity.run_id,
                thread_id=request.identity.thread_id,
                task_id=request.identity.task_id,
                secret_filter=secret_filter,
            )
            try:
                recorder.lifecycle(
                    phase="start",
                    status="started",
                )
            except AuditIncompleteError as error:
                return DurableRunResult(
                    DurableRunStatus.FAILED,
                    error_code="audit_error",
                    message=secret_filter.redact_text(str(error)),
                    preflight=preflight,
                    run_id=request.identity.run_id,
                    audit_incomplete=True,
                    audit_error_code=error.code,
                )

            if resume:
                uncertain = mark_uncertain_dispatch(graph, thread_config)
                if uncertain is not None:
                    base_result = DurableRunResult(
                        DurableRunStatus.REJECTED,
                        state=uncertain,
                        error_code=BudgetErrorCode.OUTCOME_UNKNOWN.value,
                        message=(
                            "An in-flight external call has no durable result; "
                            "it will not be replayed."
                        ),
                        preflight=preflight,
                        run_id=request.identity.run_id,
                    )
                    return _finish_audit(
                        base_result,
                        config=config,
                        request=request,
                        sink=audit_sink,
                        started_at=started_at,
                        finished_at=time.time(),
                        secret_filter=secret_filter,
                    )
                graph_input = None
            else:
                assert initial_state is not None
                safe_initial = secret_filter.sanitize_node_update(initial_state)
                if (
                    safe_initial.get("runtime_error_code")
                    is BudgetErrorCode.SENSITIVE_DATA_DETECTED
                ):
                    base_result = DurableRunResult(
                        DurableRunStatus.FAILED,
                        state=safe_initial,
                        error_code=BudgetErrorCode.SENSITIVE_DATA_DETECTED.value,
                        message=SENSITIVE_DATA_MESSAGE,
                        preflight=preflight,
                        run_id=request.identity.run_id,
                    )
                    return _finish_audit(
                        base_result,
                        config=config,
                        request=request,
                        sink=audit_sink,
                        started_at=started_at,
                        finished_at=time.time(),
                        secret_filter=secret_filter,
                    )
                graph_input = {
                    **safe_initial,
                    "run_identity": request.identity,
                    "run_config_digest": request.run_config_digest,
                    "agent_revision": request.agent_revision,
                    "budget": BudgetSnapshot.create(
                        max_steps=config.max_steps,
                        max_cost_usd=config.max_cost_usd,
                        deadline_at=started_at + config.timeout_seconds,
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
                base_result = DurableRunResult(
                    DurableRunStatus.FAILED,
                    state=result,
                    error_code=code,
                    message=secret_filter.redact_text(
                        str(result.get("runtime_message", ""))
                    ),
                    preflight=preflight,
                    run_id=request.identity.run_id,
                )
                return _finish_audit(
                    base_result,
                    config=config,
                    request=request,
                    sink=audit_sink,
                    started_at=started_at,
                    finished_at=time.time(),
                    secret_filter=secret_filter,
                )
            status = (
                DurableRunStatus.PAUSED
                if snapshot.next
                else DurableRunStatus.COMPLETED
            )
            base_result = DurableRunResult(
                status,
                state=result,
                preflight=preflight,
                run_id=request.identity.run_id,
            )
            return _finish_audit(
                base_result,
                config=config,
                request=request,
                sink=audit_sink,
                started_at=started_at,
                finished_at=time.time(),
                secret_filter=secret_filter,
            )
        except AuditIncompleteError as error:
            return DurableRunResult(
                DurableRunStatus.FAILED,
                error_code="audit_error",
                message=secret_filter.redact_text(str(error)),
                run_id=request.identity.run_id,
                audit_incomplete=True,
                audit_error_code=error.code,
            )
        except EventSinkError as error:
            return DurableRunResult(
                DurableRunStatus.FAILED,
                error_code="audit_error",
                message=secret_filter.redact_text(str(error)),
                run_id=request.identity.run_id,
                audit_incomplete=True,
                audit_error_code=error.code.value,
            )
        except sqlite3.Error as error:
            return DurableRunResult(
                DurableRunStatus.FAILED,
                error_code="sqlite_error",
                message=secret_filter.redact_text(str(error)),
                run_id=request.identity.run_id,
            )
        except OSError as error:
            return DurableRunResult(
                DurableRunStatus.FAILED,
                error_code="runtime_io_error",
                message=secret_filter.redact_text(str(error)),
                run_id=request.identity.run_id,
            )
        except WorkspaceRootError as error:
            return DurableRunResult(
                DurableRunStatus.REJECTED,
                error_code="workspace_error",
                message=secret_filter.redact_text(str(error)),
                run_id=request.identity.run_id,
            )
    finally:
        connection.close()


def create_durable_workflow(
    config: RunConfig,
    run_id: str,
    saver: SqliteSaver,
    clock: Callable[[], float] = time.time,
    *,
    event_sink: EventSink | None = None,
    task_id: str | None = None,
    thread_id: str | None = None,
):
    """Compile the parent with a saver; child graphs inherit it by default."""
    boundary = DurableBudgetBoundary(
        run_id,
        clock=clock,
        model_request_timeout_seconds=config.model_request_timeout_seconds,
    )
    secret_filter = KnownSecretFilter.from_run_config(config)
    event_recorder = (
        EventRecorder(
            event_sink,
            run_id=run_id,
            thread_id=thread_id or run_id,
            task_id=task_id or run_id,
            model={
                "provider": config.model.provider,
                "model": config.model.model,
                "max_output_tokens": config.model_max_output_tokens,
            },
            secret_filter=secret_filter,
        )
        if event_sink is not None
        else None
    )
    architect = create_architect_workflow(
        durable_architect_runtime(
            config.model,
            config.model_max_output_tokens,
            config.pricing,
            model_request_timeout_seconds=config.model_request_timeout_seconds,
            model_retry_policy=config.model_retry_policy,
        ),
        budget_boundary=boundary,
        secret_filter=secret_filter,
        event_recorder=event_recorder,
    )
    developer = create_developer_workflow(
        durable_developer_runtime(
            config.model,
            config.model_max_output_tokens,
            config.pricing,
            workspace_root=config.workspace_root,
            model_request_timeout_seconds=config.model_request_timeout_seconds,
            model_retry_policy=config.model_retry_policy,
            secret_filter=secret_filter,
        ),
        budget_boundary=boundary,
        secret_filter=secret_filter,
        event_recorder=event_recorder,
    )
    return create_workflow_graph(
        architect=architect,
        developer=developer,
        verification_specs=config.verification_specs,
        workspace_root=config.workspace_root,
        durable_runtime=True,
        clock=clock,
        secret_filter=secret_filter,
        event_recorder=event_recorder,
    ).compile(checkpointer=saver).with_config({"tags": ["agent-durable-v1"]})


def _finish_audit(
    result: DurableRunResult,
    *,
    config: RunConfig,
    request: StartRequest | ResumeRequest,
    sink: EventSink,
    started_at: float,
    finished_at: float,
    secret_filter: KnownSecretFilter,
) -> DurableRunResult:
    """Append terminal audit facts and publish the small run record."""
    try:
        terminal_code = result.error_code
        event = RunEvent.create(
            run_id=request.identity.run_id,
            thread_id=request.identity.thread_id,
            task_id=request.identity.task_id,
            subject_id=(
                f"{request.identity.run_id}:"
                f"{result.status.value}:{terminal_code or 'ok'}"
            ),
            event_type="run",
            phase="error" if terminal_code else "result",
            status="failed" if terminal_code else result.status.value,
            timestamp=finished_at,
            error=(
                {
                    "code": terminal_code,
                    "message": secret_filter.redact_text(result.message)[:512],
                }
                if terminal_code
                else None
            ),
            summary={
                "runtime_status": result.status.value,
                "workflow_outcome": _state_value(
                    result.state, "outcome"
                ),
                "verification_status": _state_value(
                    result.state, "verification_status"
                ),
            },
        )
        append = sink.append_once(event)
        if not append.ok:
            return _audit_failure(
                result,
                code=append.error_code or "audit_error",
                message=append.message,
            )
        record = RunRecord(
            schema_version=1,
            run_id=request.identity.run_id,
            thread_id=request.identity.thread_id,
            task_id=request.identity.task_id,
            workspace_root=request.identity.workspace.canonical_root,
            workspace_root_digest=request.identity.workspace.root_digest,
            run_config_digest=request.run_config_digest,
            runtime_status=result.status.value,
            workflow_outcome=_state_value(result.state, "outcome"),
            verification_status=_state_value(result.state, "verification_status"),
            error_code=result.error_code,
            budget=_budget_record(result.state),
            started_at=started_at,
            finished_at=finished_at,
            last_event_sequence=getattr(sink, "last_sequence", append.sequence),
            audit_status=AuditStatus.COMPLETE,
            agent_revision=_revision_record(request.agent_revision),
            workspace_revision_start="UNKNOWN",
            workspace_revision_end="UNKNOWN",
        )
        written = write_run_record(
            config.runtime_root,
            record,
            secret_filter=secret_filter,
        )
        if not written.success:
            return _audit_failure(
                result,
                code=written.error_code or "audit_error",
                message=written.message,
            )
        return replace(result, record_ref=written.record_ref)
    except (AuditIncompleteError, EventSinkError, OSError, ValueError) as error:
        return _audit_failure(
            result,
            code=getattr(error, "code", "audit_error"),
            message=str(error),
        )


def _audit_failure(
    result: DurableRunResult,
    *,
    code: object,
    message: str,
) -> DurableRunResult:
    normalized = code.value if isinstance(code, Enum) else str(code)
    return replace(
        result,
        error_code=result.error_code or "audit_error",
        message=result.message or message,
        audit_incomplete=True,
        audit_error_code=normalized,
    )


def _state_value(state: Mapping[str, Any] | None, key: str) -> str | None:
    if not isinstance(state, Mapping):
        return None
    value = state.get(key)
    return value.value if isinstance(value, Enum) else (str(value) if value is not None else None)


def _budget_record(state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, Mapping):
        return None
    budget = state.get("budget")
    if budget is None:
        return None
    return {
        "max_steps": getattr(budget, "max_steps", None),
        "steps_used": getattr(budget, "steps_used", None),
        "max_cost_microusd": getattr(budget, "max_cost_microusd", None),
        "cost_microusd": getattr(budget, "cost_microusd", None),
        "cost_unknown": getattr(budget, "cost_unknown", None),
        "deadline_at": getattr(budget, "deadline_at", None),
        "usage": [
            {
                "call_id": getattr(item, "call_id", None),
                "status": _enum_value(getattr(item, "status", None)),
                "input_tokens": getattr(item, "input_tokens", None),
                "output_tokens": getattr(item, "output_tokens", None),
                "total_tokens": getattr(item, "total_tokens", None),
                "cost_microusd": getattr(item, "cost_microusd", None),
                "cost_source": getattr(item, "cost_source", None),
            }
            for item in getattr(budget, "usage", ())
        ],
    }


def _revision_record(revision: AgentCodeRevision) -> dict[str, Any]:
    return {
        "commit_sha": revision.commit_sha,
        "status": revision.status.value,
        "reason": revision.reason.value if revision.reason else None,
    }


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _thread_config(thread_id: str, max_steps: int) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": durable_recursion_limit(max_steps),
    }


def _identity_contains_secret(
    identity: Any, secret_filter: KnownSecretFilter
) -> bool:
    values = (
        identity.run_id,
        identity.thread_id,
        identity.task_id,
        identity.workspace.canonical_root,
        identity.workspace.root_digest,
    )
    return any(secret_filter.contains_secret(value) for value in values)
