import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    AppendResult,
    AppendStatus,
    CallKind,
    CallReservation,
    CallStatus,
    BudgetSnapshot,
    DurableBudgetBoundary,
    DurableCallResult,
    DurableBudgetState,
    DurableRunStatus,
    RunIdentity,
    RecordWriteResult,
    ResumeRequest,
    StartRequest,
    WorkspaceIdentity,
    WorkspaceRevision,
    WorkspaceRevisionReason,
    run_exit_code,
    EventRecorder,
    semantic_config_digest,
    start_run,
)
from agent.runtime import (
    AppendStatus,
    AuditStatus,
    EventSinkError,
    EventSinkErrorCode,
    JsonlEventSink,
    KnownSecretFilter,
    RunEvent,
    RunRecord,
    UsageRecord,
    UsageStatus,
    write_run_record,
)
from agent.architect.graph import (
    ArchitectRuntime,
    ResearchEvaluation,
    ResearchStep,
    create_architect_workflow,
)
from agent.common.entities import ImplementationPlan
from agent.editing import EditResult, EditStatus
from agent.verification import VerificationCheckStatus, VerificationResult
from tests.runtime._config_support import RunConfigTestCase, model_settings


def event(
    *,
    status: str = "succeeded",
    summary: dict | None = None,
    subject_id: str = "call-1",
    phase: str = "result",
) -> RunEvent:
    return RunEvent.create(
        run_id="run-1",
        thread_id="thread-1",
        task_id="task-1",
        subject_id=subject_id,
        event_type="model",
        phase=phase,
        status=status,
        timestamp=100.0,
        step_id="step-1",
        call_id="call-1",
        summary=summary or {"bounded": True},
    )


class TrajectoryContractTests(unittest.TestCase):
    def test_idempotency_key_excludes_sequence_and_reopen_replay_is_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = JsonlEventSink(directory)
            original = first.append_once(event())
            self.assertEqual(original.status, AppendStatus.APPENDED)
            self.assertEqual(original.sequence, 1)

            reopened = JsonlEventSink(directory)
            duplicate = reopened.append_once(event())

            self.assertEqual(duplicate.status, AppendStatus.DUPLICATE)
            self.assertEqual(duplicate.sequence, 1)
            self.assertEqual(len(reopened.events), 1)

    def test_conflicting_semantic_payload_uses_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlEventSink(directory)
            self.assertEqual(sink.append_once(event()).status, AppendStatus.APPENDED)

            conflict = sink.append_once(event(summary={"bounded": False}))

            self.assertEqual(conflict.status, AppendStatus.CONFLICT)
            self.assertEqual(
                conflict.error_code,
                EventSinkErrorCode.CONFLICTING_KEY.value,
            )
            self.assertEqual(len(sink.events), 1)

    def test_stale_sink_instances_refresh_sequence_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = JsonlEventSink(directory)
            second = JsonlEventSink(directory)

            first_result = first.append_once(event(subject_id="call-1"))
            second_result = second.append_once(
                event(subject_id="call-2")
            )
            reopened = JsonlEventSink(directory)

            self.assertEqual(first_result.sequence, 1)
            self.assertEqual(second_result.sequence, 2)
            self.assertEqual(reopened.last_sequence, 2)
            self.assertEqual(len(reopened.events), 2)

    def test_truncated_tail_and_corrupt_json_are_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b'{"schema_version": 1')
            with self.assertRaises(EventSinkError) as truncated:
                JsonlEventSink(directory)
            self.assertEqual(
                truncated.exception.code,
                EventSinkErrorCode.TRUNCATED_TAIL,
            )

            path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaises(EventSinkError) as corrupt:
                JsonlEventSink(directory)
            self.assertEqual(
                corrupt.exception.code,
                EventSinkErrorCode.CORRUPT_LOG,
            )

    def test_existing_event_secret_fails_closed_on_reopen(self) -> None:
        canary = "S3_EVENT_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            JsonlEventSink(directory).append_once(
                event(summary={"diagnostic": canary})
            )

            with self.assertRaises(EventSinkError) as failure:
                JsonlEventSink(
                    directory,
                    secret_filter=KnownSecretFilter((canary,)),
                )

            self.assertEqual(
                failure.exception.code,
                EventSinkErrorCode.SENSITIVE_DATA_DETECTED,
            )

    def test_append_failure_is_structured_and_does_not_update_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlEventSink(directory)
            with patch.object(Path, "open", side_effect=OSError("disk full")):
                result = sink.append_once(event())

            self.assertEqual(result.status, AppendStatus.ERROR)
            self.assertEqual(result.error_code, EventSinkErrorCode.IO_ERROR.value)
            self.assertEqual(sink.last_sequence, 0)
            self.assertEqual(sink.events, ())

    def test_unexpected_sink_runtime_error_still_propagates(self) -> None:
        class Sink:
            def append_once(self, _event):
                raise RuntimeError("unexpected sink programming failure")

        recorder = EventRecorder(
            Sink(),
            run_id="run-1",
            thread_id="thread-1",
            task_id="task-1",
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected sink programming failure"):
            recorder.append(event())

    def test_secret_filter_redacts_event_summary_and_record_uses_safe_filename(self) -> None:
        canary = "S3_TRAJECTORY_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlEventSink(
                directory,
                secret_filter=KnownSecretFilter((canary,)),
            )
            self.assertTrue(
                sink.append_once(event(summary={"diagnostic": canary})).ok
            )

            record = RunRecord(
                schema_version=1,
                run_id="run/with/user-id",
                thread_id="thread/with/user-id",
                task_id="task/with/user-id",
                workspace_root="C:/workspace",
                workspace_root_digest="a" * 64,
                run_config_digest="b" * 64,
                runtime_status="completed",
                workflow_outcome="completed",
                verification_status="verified",
                error_code=None,
                budget={"usage": "bounded"},
                started_at=100.0,
                finished_at=101.0,
                last_event_sequence=sink.last_sequence,
                audit_status=AuditStatus.COMPLETE,
                agent_revision={"status": "unknown"},
            )
            written = write_run_record(
                directory,
                record,
                secret_filter=KnownSecretFilter((canary,)),
            )

            self.assertTrue(written.success)
            self.assertIsNotNone(written.path)
            self.assertNotIn("run", written.path.name)
            for path in Path(directory).rglob("*"):
                if path.is_file():
                    self.assertNotIn(canary.encode(), path.read_bytes())
            payload = json.loads(written.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["audit_status"], "complete")

    def test_record_write_failure_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = RunRecord(
                schema_version=1,
                run_id="run-1",
                thread_id="thread-1",
                task_id="task-1",
                workspace_root="C:/workspace",
                workspace_root_digest="a" * 64,
                run_config_digest="b" * 64,
                runtime_status="completed",
                workflow_outcome="completed",
                verification_status="verified",
                error_code=None,
                budget=None,
                started_at=100.0,
                finished_at=101.0,
                last_event_sequence=1,
                audit_status=AuditStatus.COMPLETE,
                agent_revision={"status": "unknown"},
            )
            with patch(
                "agent.runtime.trajectory_record.os.replace",
                side_effect=OSError("disk full"),
            ):
                result = write_run_record(directory, record)

            self.assertFalse(result.success)
            self.assertEqual(result.error_code, EventSinkErrorCode.IO_ERROR.value)

    def test_existing_corrupt_record_is_not_silently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = RunRecord(
                schema_version=1,
                run_id="run-1",
                thread_id="thread-1",
                task_id="task-1",
                workspace_root="C:/workspace",
                workspace_root_digest="a" * 64,
                run_config_digest="b" * 64,
                runtime_status="completed",
                workflow_outcome="completed",
                verification_status="verified",
                error_code=None,
                budget=None,
                started_at=100.0,
                finished_at=101.0,
                last_event_sequence=1,
                audit_status=AuditStatus.COMPLETE,
                agent_revision={"status": "unknown"},
            )
            written = write_run_record(directory, record)
            assert written.path is not None
            written.path.write_text("{\"schema_version\":1", encoding="utf-8")

            result = write_run_record(directory, record)

            self.assertFalse(result.success)
            self.assertEqual(
                result.error_code,
                EventSinkErrorCode.CORRUPT_RECORD.value,
            )

    def test_v2_revision_shape_is_strict_and_legacy_v1_remains_readable(self) -> None:
        unknown = WorkspaceRevision.unknown(
            WorkspaceRevisionReason.NOT_GIT
        ).to_dict()
        record = RunRecord(
            schema_version=2,
            run_id="run-v2",
            thread_id="thread-v2",
            task_id="task-v2",
            workspace_root="C:/workspace",
            workspace_root_digest="a" * 64,
            run_config_digest="b" * 64,
            runtime_status="completed",
            workflow_outcome="completed",
            verification_status="verified",
            error_code=None,
            budget=None,
            started_at=100.0,
            finished_at=101.0,
            last_event_sequence=1,
            audit_status=AuditStatus.COMPLETE,
            agent_revision={"status": "unknown"},
            workspace_revision_start=unknown,
            workspace_revision_end=unknown,
        )
        payload = record.to_dict()
        self.assertEqual(RunRecord.from_dict(payload).schema_version, 2)

        missing = dict(payload)
        missing.pop("workspace_revision_end")
        with self.assertRaises(ValueError):
            RunRecord.from_dict(missing)

        extra = dict(payload)
        extra["workspace_revision_start"] = {
            **unknown,
            "unexpected": "field",
        }
        with self.assertRaises(ValueError):
            RunRecord.from_dict(extra)

        incomplete = dict(payload)
        incomplete["workspace_revision_start"] = {
            "status": "known",
            "head": "a" * 40,
            "dirty": False,
            "status_digest": "b" * 64,
            "diff_digest": None,
            "reason": None,
        }
        with self.assertRaises(ValueError):
            RunRecord.from_dict(incomplete)

        legacy = dict(payload)
        legacy["schema_version"] = 1
        legacy.pop("workspace_revision_start")
        legacy.pop("workspace_revision_end")
        self.assertEqual(RunRecord.from_dict(legacy).schema_version, 1)

    def test_corrupt_v2_revision_is_not_silently_overwritten(self) -> None:
        unknown = WorkspaceRevision.unknown(
            WorkspaceRevisionReason.NOT_GIT
        ).to_dict()
        record = RunRecord(
            schema_version=2,
            run_id="run-v2-write",
            thread_id="thread-v2-write",
            task_id="task-v2-write",
            workspace_root="C:/workspace",
            workspace_root_digest="a" * 64,
            run_config_digest="b" * 64,
            runtime_status="completed",
            workflow_outcome="completed",
            verification_status="verified",
            error_code=None,
            budget=None,
            started_at=100.0,
            finished_at=101.0,
            last_event_sequence=1,
            audit_status=AuditStatus.COMPLETE,
            agent_revision={"status": "unknown"},
            workspace_revision_start=unknown,
            workspace_revision_end=unknown,
        )
        with tempfile.TemporaryDirectory() as directory:
            written = write_run_record(directory, record)
            assert written.path is not None
            corrupt = dict(record.to_dict())
            corrupt["workspace_revision_end"] = {
                **unknown,
                "unexpected": "field",
            }
            original = json.dumps(corrupt, sort_keys=True).encode() + b"\n"
            written.path.write_bytes(original)
            result = write_run_record(directory, record)
            self.assertFalse(result.success)
            self.assertEqual(
                result.error_code,
                EventSinkErrorCode.CORRUPT_RECORD.value,
            )
            self.assertEqual(written.path.read_bytes(), original)


class DurableAuditIntegrationTests(RunConfigTestCase):
    def request(self, config: object) -> StartRequest:
        workspace = WorkspaceIdentity.from_root(config.workspace_root)
        identity = RunIdentity(
            run_id="run-audit",
            thread_id="thread-audit",
            task_id="task-audit",
            workspace=workspace,
        )
        return StartRequest(
            identity,
            semantic_config_digest(config),
            AgentCodeRevision(None, AgentRevisionStatus.UNKNOWN, "not_git"),
        )

    def test_durable_result_publishes_record_and_lifecycle_events(self) -> None:
        class State(DurableBudgetState):
            value: int = 0

        def factory(config, run_id, saver, clock):
            builder = StateGraph(State)
            builder.add_node("finish", lambda state: {"value": state.value + 1})
            builder.add_edge(START, "finish")
            builder.add_edge("finish", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            request = self.request(config)
            result = start_run(config, request, {"value": 0}, graph_factory=factory)

            self.assertEqual(result.status, DurableRunStatus.COMPLETED)
            self.assertFalse(result.summary.audit_incomplete)
            self.assertIsNotNone(result.summary.record_ref)
            self.assertEqual(run_exit_code(result.summary), 1)
            sink = JsonlEventSink(config.runtime_root)
            self.assertEqual(
                [(item.event_type, item.phase) for item in sink.events],
                [("run", "start"), ("run", "result")],
            )
            record_path = config.runtime_root / result.summary.record_ref
            self.assertTrue(record_path.exists())
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["audit_status"], "complete")
            self.assertEqual(payload["identity"]["task_id"], "task-audit")

    def test_resume_adds_new_terminal_event_without_changing_checkpoint_semantics(
        self,
    ) -> None:
        class State(DurableBudgetState):
            run_identity: RunIdentity | None = None
            run_config_digest: str | None = None
            agent_revision: AgentCodeRevision | None = None
            value: int = 0

        class Factory:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, config, run_id, saver, clock):
                self.calls += 1
                builder = StateGraph(State)
                builder.add_node("start", lambda state: {"value": state.value + 1})
                builder.add_node("finish", lambda state: {"value": state.value + 1})
                builder.add_edge(START, "start")
                builder.add_edge("start", "finish")
                builder.add_edge("finish", END)
                return builder.compile(
                    checkpointer=saver,
                    interrupt_after=["start"] if self.calls == 1 else None,
                )

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            factory = Factory()
            request = self.request(config)
            started = start_run(config, request, {"value": 0}, graph_factory=factory)
            from agent.runtime import resume_run

            resumed = resume_run(
                config,
                ResumeRequest(
                    request.identity,
                    request.run_config_digest,
                    request.agent_revision,
                ),
                graph_factory=factory,
            )

            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(resumed.status, DurableRunStatus.COMPLETED)
            sink = JsonlEventSink(config.runtime_root)
            self.assertEqual(
                [(event.phase, event.status) for event in sink.events],
                [
                    ("start", "started"),
                    ("result", "paused"),
                    ("result", "completed"),
                ],
            )

    def test_failed_audit_sink_stops_before_graph_invoke_and_exit_is_nonzero(self) -> None:
        class FailingSink:
            def append_once(self, event):
                return AppendResult(
                    AppendStatus.ERROR,
                    event.idempotency_key,
                    error_code="io_error",
                    message="disk full",
                )

        calls = {"factory": 0, "invoke": 0}

        class State(DurableBudgetState):
            value: int = 0

        def factory(config, run_id, saver, clock):
            calls["factory"] += 1
            builder = StateGraph(State)

            def finish(state):
                calls["invoke"] += 1
                return {"value": state.value + 1}

            builder.add_node("finish", finish)
            builder.add_edge(START, "finish")
            builder.add_edge("finish", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            result = start_run(
                config,
                self.request(config),
                {"value": 0},
                graph_factory=factory,
                event_sink=FailingSink(),
            )

            self.assertEqual(result.status, DurableRunStatus.FAILED)
            self.assertTrue(result.summary.audit_incomplete)
            self.assertEqual(run_exit_code(result.summary), 2)
            self.assertEqual(calls["factory"], 1)
            self.assertEqual(calls["invoke"], 0)

    def test_node_audit_failure_redacts_sink_error_before_sqlite_checkpoint(self) -> None:
        canary = "S3_AUDIT_NODE_SECRET"

        class Sink:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def append_once(self, event):
                self.calls.append(f"{event.event_type}:{event.phase}")
                if len(self.calls) == 1:
                    return AppendResult(
                        AppendStatus.APPENDED,
                        event.idempotency_key,
                        sequence=1,
                    )
                return AppendResult(
                    AppendStatus.ERROR,
                    event.idempotency_key,
                    error_code=f"sink-code-{canary}",
                    message=f"sink failed while writing {canary}",
                )

        calls = {"node": 0, "later": 0}
        sink = Sink()

        class State(DurableBudgetState):
            value: int = 0

        def factory(config, run_id, saver, clock):
            recorder = EventRecorder(
                sink,
                run_id=run_id,
                thread_id="thread-audit",
                task_id="task-audit",
                secret_filter=KnownSecretFilter((canary,)),
                clock=clock,
            )
            builder = StateGraph(State)

            def finish(state):
                calls["node"] += 1
                return {"value": state.value + 1}

            def later(_state):
                calls["later"] += 1
                return {}

            builder.add_node(
                "finish",
                recorder.wrap_node(
                    finish,
                    event_type="tool",
                    node_name="finish",
                ),
            )
            builder.add_node("later", later)
            builder.add_edge(START, "finish")
            builder.add_edge("finish", "later")
            builder.add_edge("later", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(
                root,
                model=model_settings(canary),
                verification_specs=(),
                max_cost_usd=None,
            )
            result = start_run(
                config,
                self.request(config),
                {"value": 0},
                graph_factory=factory,
                event_sink=sink,
            )

            self.assertEqual(result.status, DurableRunStatus.FAILED)
            self.assertTrue(result.audit_incomplete)
            self.assertEqual(run_exit_code(result.summary), 2)
            self.assertEqual(calls, {"node": 0, "later": 0})
            self.assertEqual(sink.calls, ["run:start", "tool:start"])
            for value in (
                result.message,
                result.error_code,
                result.audit_error_code,
                repr(result.summary),
            ):
                self.assertNotIn(canary, value or "")
            database = config.runtime_root / "checkpoints.sqlite"
            self.assertTrue(database.exists())
            for path in config.runtime_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(canary.encode(), path.read_bytes(), path.name)

    def test_node_audit_exception_redacts_sink_error_before_sqlite_checkpoint(self) -> None:
        canary = "S3_AUDIT_EXCEPTION_SECRET"

        class Sink:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def append_once(self, event):
                self.calls.append(f"{event.event_type}:{event.phase}")
                if len(self.calls) == 1:
                    return AppendResult(
                        AppendStatus.APPENDED,
                        event.idempotency_key,
                        sequence=1,
                    )
                raise EventSinkError(
                    EventSinkErrorCode.IO_ERROR,
                    f"sink exception while writing {canary}",
                )

        calls = {"node": 0, "later": 0}
        sink = Sink()

        class State(DurableBudgetState):
            value: int = 0

        def factory(config, run_id, saver, clock):
            recorder = EventRecorder(
                sink,
                run_id=run_id,
                thread_id="thread-audit-exception",
                task_id="task-audit-exception",
                secret_filter=KnownSecretFilter((canary,)),
                clock=clock,
            )
            builder = StateGraph(State)

            def finish(state):
                calls["node"] += 1
                return {"value": state.value + 1}

            def later(_state):
                calls["later"] += 1
                return {}

            builder.add_node(
                "finish",
                recorder.wrap_node(
                    finish,
                    event_type="tool",
                    node_name="finish",
                ),
            )
            builder.add_node("later", later)
            builder.add_edge(START, "finish")
            builder.add_edge("finish", "later")
            builder.add_edge("later", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(
                root,
                model=model_settings(canary),
                verification_specs=(),
                max_cost_usd=None,
            )
            result = start_run(
                config,
                self.request(config),
                {"value": 0},
                graph_factory=factory,
                event_sink=sink,
            )

            self.assertEqual(result.status, DurableRunStatus.FAILED)
            self.assertTrue(result.audit_incomplete)
            self.assertEqual(run_exit_code(result.summary), 2)
            self.assertEqual(calls, {"node": 0, "later": 0})
            self.assertEqual(sink.calls, ["run:start", "tool:start"])
            for value in (
                result.message,
                result.error_code,
                result.audit_error_code,
                repr(result.summary),
            ):
                self.assertNotIn(canary, value or "")
            database = config.runtime_root / "checkpoints.sqlite"
            self.assertTrue(database.exists())
            for path in config.runtime_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(canary.encode(), path.read_bytes(), path.name)

    def test_terminal_audit_failure_redacts_sink_error_in_library_result(self) -> None:
        canary = "S3_AUDIT_FINISH_SECRET"

        class Sink:
            def __init__(self) -> None:
                self.calls = 0

            def append_once(self, event):
                self.calls += 1
                if self.calls == 1:
                    return AppendResult(
                        AppendStatus.APPENDED,
                        event.idempotency_key,
                        sequence=1,
                    )
                return AppendResult(
                    AppendStatus.ERROR,
                    event.idempotency_key,
                    error_code=f"finish-code-{canary}",
                    message=f"terminal audit failed: {canary}",
                )

        calls = {"node": 0}
        sink = Sink()

        class State(DurableBudgetState):
            value: int = 0

        def factory(config, run_id, saver, clock):
            builder = StateGraph(State)

            def finish(state):
                calls["node"] += 1
                return {"value": state.value + 1}

            builder.add_node("finish", finish)
            builder.add_edge(START, "finish")
            builder.add_edge("finish", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(
                root,
                model=model_settings(canary),
                verification_specs=(),
                max_cost_usd=None,
            )
            result = start_run(
                config,
                self.request(config),
                {"value": 0},
                graph_factory=factory,
                event_sink=sink,
            )

            self.assertEqual(result.status, DurableRunStatus.COMPLETED)
            self.assertTrue(result.audit_incomplete)
            self.assertEqual(run_exit_code(result.summary), 2)
            self.assertEqual(calls["node"], 1)
            self.assertEqual(sink.calls, 2)
            for value in (
                result.message,
                result.error_code,
                result.audit_error_code,
                repr(result.summary),
            ):
                self.assertNotIn(canary, value or "")
            for path in config.runtime_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(canary.encode(), path.read_bytes(), path.name)

    def test_record_failure_marks_summary_audit_incomplete(self) -> None:
        class State(DurableBudgetState):
            run_identity: RunIdentity | None = None
            run_config_digest: str | None = None
            agent_revision: AgentCodeRevision | None = None
            value: int = 0

        def factory(config, run_id, saver, clock):
            builder = StateGraph(State)
            builder.add_node("finish", lambda state: {"value": state.value + 1})
            builder.add_edge(START, "finish")
            builder.add_edge("finish", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            with patch(
                "agent.runtime.durable.write_run_record",
                return_value=RecordWriteResult(
                    success=False,
                    error_code="io_error",
                    message="disk full",
                ),
            ):
                result = start_run(
                    config,
                    self.request(config),
                    {"value": 0},
                    graph_factory=factory,
                )

            self.assertEqual(result.status, DurableRunStatus.COMPLETED)
            self.assertTrue(result.summary.audit_incomplete)
            self.assertIsNone(result.summary.record_ref)
            self.assertEqual(run_exit_code(result.summary), 2)

    def test_corrupt_existing_event_log_stops_before_graph(self) -> None:
        class State(DurableBudgetState):
            value: int = 0

        calls = {"invoke": 0}

        def factory(config, run_id, saver, clock):
            builder = StateGraph(State)

            def finish(state):
                calls["invoke"] += 1
                return {"value": state.value + 1}

            builder.add_node("finish", finish)
            builder.add_edge(START, "finish")
            builder.add_edge("finish", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            config.runtime_root.mkdir(parents=True)
            (config.runtime_root / "events.jsonl").write_text(
                "{\"schema_version\": 1", encoding="utf-8"
            )
            result = start_run(
                config,
                self.request(config),
                {"value": 0},
                graph_factory=factory,
            )

            self.assertEqual(result.status, DurableRunStatus.FAILED)
            self.assertTrue(result.summary.audit_incomplete)
            self.assertEqual(run_exit_code(result.summary), 2)
            self.assertEqual(calls["invoke"], 0)

    def test_architect_recorder_emits_model_events_without_state_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlEventSink(directory)
            recorder = EventRecorder(
                sink,
                run_id="run-model",
                thread_id="thread-model",
                task_id="task-model",
                model={"provider": "deepseek", "model": "fake"},
            )
            boundary = DurableBudgetBoundary(
                "run-model", clock=lambda: 100.0
            )
            runtime = ArchitectRuntime(
                plan_next_step=lambda _: ResearchStep(
                    reasoning="reason", hypothesis="hypothesis"
                ),
                check_research_step=lambda _: ResearchEvaluation(
                    reasoning="valid", is_valid=True
                ),
                conduct_research=lambda _: AIMessage(content="done"),
                extract_implementation_plan=lambda _: ImplementationPlan(tasks=[]),
                load_codebase_structure=lambda: "app.py",
            )
            graph = create_architect_workflow(
                runtime,
                research_tools=[],
                budget_boundary=boundary,
                event_recorder=recorder,
            )
            result = graph.invoke(
                {
                    "implementation_research_scratchpad": [
                        HumanMessage(content="task")
                    ],
                    "budget": BudgetSnapshot.create(
                        max_steps=8,
                        max_cost_usd=None,
                        deadline_at=500.0,
                    ),
                }
            )

            self.assertIsNone(result.get("runtime_error_code"))
            self.assertEqual(
                {item.event_type for item in sink.events}, {"model"}
            )
            self.assertTrue(all(item.call_id for item in sink.events))
            model_results = [
                item
                for item in sink.events
                if item.phase == "result"
            ]
            self.assertTrue(model_results)
            self.assertTrue(
                all(
                    item.summary.get("request_digest")
                    and item.summary.get("response_digest")
                    for item in model_results
                )
            )
            raw = Path(directory, "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn('"content":"task"', raw)

    def test_recorder_audits_tool_edit_and_verification_with_bounded_summary(self) -> None:
        class State:
            repair_attempts = 1
            current_task_idx = 0
            current_atomic_task_idx = 0
            budget = BudgetSnapshot(
                max_steps=4,
                max_cost_microusd=None,
                deadline_at=500.0,
                reservations=(
                    CallReservation(
                        step_id="s000001",
                        call_id="run-shape:call:1:tool:tc-1",
                        kind=CallKind.TOOL,
                        status=CallStatus.IN_FLIGHT,
                        request_digest="a" * 64,
                        tool_call_id="tc-1",
                    ),
                ),
            )
            implementation_research_scratchpad = [
                type(
                    "ToolMessageLike",
                    (),
                    {"tool_call_id": "tc-1", "name": "search_directory"},
                )()
            ]
            durable_call_result = DurableCallResult(
                call_ids=("run-shape:call:1:tool:tc-1",),
                usage=(UsageRecord.unknown("run-shape:call:1:tool:tc-1"),),
                response_digests=("b" * 64,),
            )

        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlEventSink(directory)
            recorder = EventRecorder(
                sink,
                run_id="run-shape",
                thread_id="thread-shape",
                task_id="task-shape",
            )
            recorder.wrap_node(
                lambda state: {"durable_call_result": state.durable_call_result},
                event_type="tool",
                node_name="research_tools",
            )(State())
            recorder.wrap_node(
                lambda state: {
                    "last_edit_result": EditResult(
                        status=EditStatus.APPLIED,
                        path="app.py",
                        before_hash="c" * 64,
                        after_hash="d" * 64,
                        diff="secret source must not be persisted",
                    )
                },
                event_type="edit",
                node_name="commit_file_transaction",
            )(State())
            recorder.wrap_node(
                lambda state: {
                    "verification_status": "pass",
                    "post_verification": (
                        VerificationResult.create(
                            name="pytest",
                            argv=("pytest",),
                            cwd=".",
                            status=VerificationCheckStatus.PASS,
                            exit_code=0,
                        ),
                    ),
                    "stdout": "ignored",
                },
                event_type="verification",
                node_name="run_post_verification",
            )(State())

            self.assertEqual(
                {(item.event_type, item.phase) for item in sink.events},
                {
                    ("tool", "start"),
                    ("tool", "result"),
                    ("edit", "start"),
                    ("edit", "result"),
                    ("verification", "start"),
                    ("verification", "result"),
                },
            )
            tool_result = next(
                item
                for item in sink.events
                if item.event_type == "tool" and item.phase == "result"
            )
            self.assertEqual(tool_result.summary["tool_name"], "search_directory")
            self.assertEqual(tool_result.call_id, "run-shape:call:1:tool:tc-1")
            self.assertEqual(tool_result.summary["request_digest"], "a" * 64)
            self.assertEqual(tool_result.summary["response_digest"], "b" * 64)
            self.assertEqual(tool_result.usage["status"], "unknown")
            edit_result = next(
                item
                for item in sink.events
                if item.event_type == "edit" and item.phase == "result"
            )
            self.assertEqual(edit_result.summary["status"], "applied")
            self.assertEqual(edit_result.summary["before_hash"], "c" * 64)
            self.assertEqual(edit_result.summary["after_hash"], "d" * 64)
            self.assertNotIn("secret source", json.dumps(edit_result.to_dict()))
            verification_result = next(
                item
                for item in sink.events
                if item.event_type == "verification" and item.phase == "result"
            )
            self.assertEqual(
                verification_result.summary["checks"][0]["check_id"], "pytest"
            )
            self.assertEqual(
                verification_result.summary["checks"][0]["status"], "pass"
            )
            raw = Path(directory, "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("ignored", raw)


if __name__ == "__main__":
    unittest.main()
