import tempfile
import unittest
import sqlite3
from dataclasses import replace
from pathlib import Path
from pydantic import SecretStr

from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    DurableBudgetBoundary,
    DurableBudgetState,
    ModelCallResult,
    ResumeRequest,
    RunIdentity,
    StartRequest,
    UsageMeasurement,
    UsageStatus,
    WorkspaceIdentity,
    semantic_config_digest,
)
from agent.runtime.durable import (
    DurableRunStatus,
    create_durable_workflow,
    durable_recursion_limit,
    resume_run,
    start_run,
)
from tests.runtime._config_support import RunConfigTestCase


class TinyState(DurableBudgetState):
    run_identity: RunIdentity | None = None
    run_config_digest: str | None = None
    agent_revision: AgentCodeRevision | None = None
    value: int = 0


class CountingFactory:
    def __init__(self, interrupt_after: str | None = None) -> None:
        self.interrupt_after = interrupt_after
        self.calls = 0

    def __call__(self, config, run_id, saver, clock):
        self.calls += 1
        boundary = DurableBudgetBoundary(run_id, clock=clock)
        builder = StateGraph(TinyState)

        def reserve(state):
            return boundary.reserve_model(state, "model", {"value": state.value})

        def dispatch(state):
            result = ModelCallResult(
                value=state.value + 1,
                usage=UsageMeasurement(
                    UsageStatus.PARTIAL, 1, 1, 2, None, None
                ),
                response_digest="e" * 64,
            )
            value, update = boundary.capture_model(state, result)
            return {**update, "value": value}

        builder.add_node("reserve_model", reserve)
        builder.add_node("dispatch_model", dispatch)
        builder.add_node("settle_model", boundary.settle)
        builder.add_edge(START, "reserve_model")
        builder.add_edge("reserve_model", "dispatch_model")
        builder.add_edge("dispatch_model", "settle_model")
        builder.add_edge("settle_model", END)
        interrupt = self.interrupt_after if self.calls == 1 else None
        return builder.compile(
            checkpointer=saver,
            interrupt_after=[interrupt] if interrupt else None,
        )


class CompletedFactory:
    def __call__(self, config, run_id, saver, clock):
        builder = StateGraph(TinyState)
        builder.add_node("finish", lambda state: {"value": state.value + 1})
        builder.add_edge(START, "finish")
        builder.add_edge("finish", END)
        return builder.compile(checkpointer=saver)


class NestedFactory:
    def __call__(self, config, run_id, saver, clock):
        boundary = DurableBudgetBoundary(run_id, clock=clock)

        def child(label):
            builder = StateGraph(TinyState)
            builder.add_node(
                f"reserve_{label}",
                lambda state: boundary.reserve_model(
                    state, label, {"value": state.value}
                ),
            )

            def dispatch(state):
                result = ModelCallResult(
                    value=state.value + 1,
                    usage=UsageMeasurement(
                        UsageStatus.PARTIAL, 1, 1, 2, None, None
                    ),
                    response_digest="f" * 64,
                )
                value, update = boundary.capture_model(state, result)
                return {**update, "value": value}

            builder.add_node(f"dispatch_{label}", dispatch)
            builder.add_node(f"settle_{label}", boundary.settle)
            builder.add_edge(START, f"reserve_{label}")
            builder.add_edge(f"reserve_{label}", f"dispatch_{label}")
            builder.add_edge(f"dispatch_{label}", f"settle_{label}")
            builder.add_edge(f"settle_{label}", END)
            return builder.compile()

        parent = StateGraph(TinyState)
        parent.add_node("architect", child("architect"))
        parent.add_node("developer", child("developer"))
        parent.add_edge(START, "architect")
        parent.add_edge("architect", "developer")
        parent.add_edge("developer", END)
        return parent.compile(checkpointer=saver)


class NestedCrashFactory:
    def __init__(self, *, interrupt_after: str) -> None:
        self.interrupt_after = interrupt_after
        self.compiles = 0
        self.dispatches = 0

    def __call__(self, config, run_id, saver, clock):
        self.compiles += 1
        boundary = DurableBudgetBoundary(run_id, clock=clock)
        child = StateGraph(TinyState)
        child.add_node(
            "reserve_model",
            lambda state: boundary.reserve_model(state, "model", {"value": state.value}),
        )

        def dispatch(state):
            self.dispatches += 1
            value, update = boundary.capture_model(
                state,
                ModelCallResult(
                    state.value + 1,
                    UsageMeasurement(UsageStatus.PARTIAL, 1, 1, 2, None, None),
                    "c" * 64,
                ),
            )
            return {**update, "value": value}

        child.add_node("dispatch_model", dispatch)
        child.add_node("settle_model", boundary.settle)
        child.add_edge(START, "reserve_model")
        child.add_edge("reserve_model", "dispatch_model")
        child.add_edge("dispatch_model", "settle_model")
        child.add_edge("settle_model", END)
        compiled_child = child.compile(
            interrupt_after=[self.interrupt_after] if self.compiles == 1 else None
        )
        parent = StateGraph(TinyState)
        parent.add_node("child", compiled_child)
        parent.add_edge(START, "child")
        parent.add_edge("child", END)
        return parent.compile(checkpointer=saver)


class CrashingFactory:
    def __init__(self) -> None:
        self.dispatches = 0

    def __call__(self, config, run_id, saver, clock):
        boundary = DurableBudgetBoundary(run_id, clock=clock)
        builder = StateGraph(TinyState)
        builder.add_node(
            "reserve_model",
            lambda state: boundary.reserve_model(state, "model", {"value": state.value}),
        )

        def crash(_):
            self.dispatches += 1
            raise RuntimeError("simulated crash after reservation")

        builder.add_node("dispatch_model", crash)
        builder.add_edge(START, "reserve_model")
        builder.add_edge("reserve_model", "dispatch_model")
        builder.add_edge("dispatch_model", END)
        return builder.compile(checkpointer=saver)


class DeadlineFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, config, run_id, saver, clock):
        self.calls += 1
        boundary = DurableBudgetBoundary(run_id, clock=clock)
        builder = StateGraph(TinyState)

        def add_call(label, successor):
            reserve = f"reserve_{label}"
            dispatch = f"dispatch_{label}"
            settle = f"settle_{label}"
            builder.add_node(
                reserve,
                lambda state, name=label: boundary.reserve_model(
                    state, name, {"value": state.value}
                ),
            )

            def run(state):
                value, update = boundary.capture_model(
                    state,
                    ModelCallResult(
                        state.value + 1,
                        UsageMeasurement(UsageStatus.PARTIAL, 1, 1, 2, None, None),
                        "a" * 64,
                    ),
                )
                return {**update, "value": value}

            builder.add_node(dispatch, run)
            builder.add_node(settle, boundary.settle)
            builder.add_conditional_edges(
                reserve,
                boundary.may_dispatch,
                {"dispatch": dispatch, "end": END},
            )
            builder.add_edge(dispatch, settle)
            builder.add_edge(settle, successor)

        add_call("second", END)
        add_call("first", "reserve_second")
        builder.add_edge(START, "reserve_first")
        return builder.compile(
            checkpointer=saver,
            interrupt_after=["settle_first"] if self.calls == 1 else None,
        )


class DurableRuntimeTests(RunConfigTestCase):
    def request(self, config, *, digest=None):
        identity = RunIdentity(
            run_id="run-1",
            thread_id="thread-1",
            task_id="task-1",
            workspace=WorkspaceIdentity.from_root(config.workspace_root),
        )
        revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
        actual_digest = digest or semantic_config_digest(config)
        return StartRequest(identity, actual_digest, revision)

    def test_sqlite_close_reopen_resume_and_deadline_budget_do_not_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            request = self.request(config)
            factory = CountingFactory(interrupt_after="dispatch_model")

            started = start_run(
                config,
                request,
                {"value": 0},
                graph_factory=factory,
                clock=lambda: 100.0,
            )
            resumed = resume_run(
                config,
                ResumeRequest(
                    request.identity,
                    request.run_config_digest,
                    request.agent_revision,
                ),
                graph_factory=factory,
                clock=lambda: 150.0,
            )

            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(resumed.status, DurableRunStatus.COMPLETED)
            self.assertEqual(resumed.state["value"], 1)
            self.assertEqual(resumed.state["budget"].steps_used, 1)
            self.assertEqual(resumed.state["budget"].deadline_at, 100.0 + config.timeout_seconds)

    def test_parent_saver_propagates_to_default_child_graphs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory),
                verification_specs=(),
                max_cost_usd=None,
                max_steps=2,
            )
            request = self.request(config)

            started = start_run(
                config,
                request,
                {"value": 0},
                graph_factory=NestedFactory(),
                clock=lambda: 100.0,
            )
            resumed = resume_run(
                config,
                ResumeRequest(request.identity, request.run_config_digest, request.agent_revision),
                graph_factory=NestedFactory(),
                clock=lambda: 150.0,
            )

            self.assertEqual(started.status, DurableRunStatus.COMPLETED)
            self.assertEqual(started.state["value"], 2)
            self.assertEqual(started.state["budget"].steps_used, 2)
            self.assertEqual(resumed.state["budget"].steps_used, 2)

    def test_nested_reservation_without_result_is_never_dispatched_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            request = self.request(config)
            factory = NestedCrashFactory(interrupt_after="reserve_model")
            started = start_run(
                config, request, {"value": 0}, graph_factory=factory, clock=lambda: 100.0
            )
            resumed = resume_run(
                config,
                ResumeRequest(request.identity, request.run_config_digest, request.agent_revision),
                graph_factory=factory,
                clock=lambda: 110.0,
            )

            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(resumed.error_code, "outcome_unknown", resumed.message)
            self.assertEqual(factory.dispatches, 0)
            self.assertEqual(resumed.state["budget"].steps_used, 1)
            self.assertTrue(resumed.state["budget"].has_unknown_outcome)

    def test_nested_durable_result_resumes_at_settle_without_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            request = self.request(config)
            factory = NestedCrashFactory(interrupt_after="dispatch_model")
            started = start_run(
                config, request, {"value": 0}, graph_factory=factory, clock=lambda: 100.0
            )
            resumed = resume_run(
                config,
                ResumeRequest(request.identity, request.run_config_digest, request.agent_revision),
                graph_factory=factory,
                clock=lambda: 110.0,
            )

            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(resumed.status, DurableRunStatus.COMPLETED, resumed.message)
            self.assertEqual(resumed.state["value"], 1)
            self.assertEqual(resumed.state["budget"].steps_used, 1)
            self.assertEqual(factory.dispatches, 1)

    def test_reserve_crash_becomes_outcome_unknown_and_is_not_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            request = self.request(config)
            factory = CountingFactory(interrupt_after="reserve_model")
            started = start_run(
                config, request, {"value": 0}, graph_factory=factory, clock=lambda: 100.0
            )

            resumed = resume_run(
                config,
                ResumeRequest(request.identity, request.run_config_digest, request.agent_revision),
                graph_factory=factory,
                clock=lambda: 110.0,
            )

            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(resumed.status, DurableRunStatus.REJECTED)
            self.assertEqual(resumed.error_code, "outcome_unknown")
            self.assertEqual(resumed.state["value"], 0)
            self.assertEqual(resumed.state["budget"].steps_used, 1)

    def test_dispatch_crash_is_not_replayed_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            request = self.request(config)
            factory = CrashingFactory()

            started = start_run(
                config, request, {"value": 0}, graph_factory=factory, clock=lambda: 100.0
            )
            resumed = resume_run(
                config,
                ResumeRequest(request.identity, request.run_config_digest, request.agent_revision),
                graph_factory=factory,
                clock=lambda: 110.0,
            )

            self.assertEqual(started.status, DurableRunStatus.FAILED)
            self.assertEqual(resumed.error_code, "outcome_unknown")
            self.assertEqual(factory.dispatches, 1)

    def test_absolute_deadline_is_enforced_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory),
                verification_specs=(),
                max_cost_usd=None,
                timeout_seconds=10.0,
            )
            request = self.request(config)
            factory = DeadlineFactory()
            started = start_run(
                config, request, {"value": 0}, graph_factory=factory, clock=lambda: 100.0
            )
            resumed = resume_run(
                config,
                ResumeRequest(request.identity, request.run_config_digest, request.agent_revision),
                graph_factory=factory,
                clock=lambda: 111.0,
            )

            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(resumed.status, DurableRunStatus.FAILED)
            self.assertEqual(resumed.error_code, "deadline_exceeded")
            self.assertEqual(resumed.state["budget"].steps_used, 1)

    def test_real_sqlite_start_and_resume_preflight_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            request = self.request(config)
            factory = CompletedFactory()
            missing = resume_run(
                config,
                ResumeRequest(request.identity, request.run_config_digest, request.agent_revision),
                graph_factory=factory,
            )
            first = start_run(config, request, {"value": 0}, graph_factory=factory)
            duplicate = start_run(config, request, {"value": 0}, graph_factory=factory)
            mismatch = resume_run(
                config,
                ResumeRequest(request.identity, "b" * 64, request.agent_revision),
                graph_factory=factory,
            )

            alternate = config.workspace_root.parent / "other-workspace"
            alternate.mkdir()
            cases = (
                (
                    replace(
                        request.identity,
                        workspace=WorkspaceIdentity.from_root(alternate),
                    ),
                    request.run_config_digest,
                    request.agent_revision,
                    "workspace_mismatch",
                ),
                (
                    replace(request.identity, run_id="other-run"),
                    request.run_config_digest,
                    request.agent_revision,
                    "thread_identity_mismatch",
                ),
                (
                    replace(request.identity, task_id="other-task"),
                    request.run_config_digest,
                    request.agent_revision,
                    "thread_identity_mismatch",
                ),
                (
                    request.identity,
                    request.run_config_digest,
                    AgentCodeRevision("b" * 40, AgentRevisionStatus.KNOWN),
                    "agent_revision_mismatch",
                ),
            )

            self.assertEqual(missing.error_code, "checkpoint_not_found")
            self.assertEqual(first.status, DurableRunStatus.COMPLETED)
            self.assertEqual(duplicate.error_code, "thread_already_exists")
            self.assertEqual(mismatch.error_code, "run_config_mismatch")
            for identity, digest, revision, expected in cases:
                with self.subTest(expected=expected):
                    rejected = resume_run(
                        config,
                        ResumeRequest(identity, digest, revision),
                        graph_factory=factory,
                    )
                    self.assertEqual(rejected.error_code, expected)

    def test_current_config_digest_cannot_be_bypassed_by_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=(), max_cost_usd=None)
            request = self.request(config)
            factory = CompletedFactory()
            bad_start = start_run(
                config,
                replace(request, run_config_digest="b" * 64),
                {"value": 0},
                graph_factory=factory,
            )
            self.assertEqual(bad_start.error_code, "run_config_mismatch")
            self.assertFalse(config.runtime_root.exists())

            started = start_run(config, request, {"value": 0}, graph_factory=factory)
            changed = replace(config, max_steps=config.max_steps + 1)
            bypass = resume_run(
                changed,
                ResumeRequest(
                    request.identity,
                    request.run_config_digest,
                    request.agent_revision,
                ),
                graph_factory=factory,
            )

            self.assertEqual(started.status, DurableRunStatus.COMPLETED)
            self.assertEqual(bypass.error_code, "run_config_mismatch")

    def test_current_workspace_cannot_be_bypassed_by_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=(), max_cost_usd=None)
            request = self.request(config)
            other = root / "other-workspace"
            other.mkdir()
            mismatched = replace(
                request,
                identity=replace(
                    request.identity,
                    workspace=WorkspaceIdentity.from_root(other),
                ),
            )

            result = start_run(
                config,
                mismatched,
                {"value": 0},
                graph_factory=CompletedFactory(),
            )

            self.assertEqual(result.error_code, "workspace_mismatch")
            self.assertFalse(config.runtime_root.exists())

    def test_recursion_limit_uses_documented_formula(self) -> None:
        self.assertEqual(durable_recursion_limit(1), 200)
        self.assertEqual(durable_recursion_limit(100), 864)

    def test_production_durable_factory_compiles_existing_parent_and_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory), verification_specs=(), max_cost_usd=None
            )
            with sqlite3.connect(":memory:", check_same_thread=False) as connection:
                from langgraph.checkpoint.sqlite import SqliteSaver

                graph = create_durable_workflow(
                    config, "run-1", SqliteSaver(connection), clock=lambda: 100.0
                )

            nodes = graph.get_graph().nodes
            self.assertIn("swe_architect", nodes)
            self.assertIn("swe_developer", nodes)

    def test_api_key_never_enters_sqlite_checkpoint_or_budget_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=(), max_cost_usd=None)
            canary = "S3_SECRET_CANARY_9f81"
            config = replace(
                config,
                model=replace(config.model, api_key=SecretStr(canary)),
            )
            request = self.request(config)

            result = start_run(
                config, request, {"value": 0}, graph_factory=CompletedFactory()
            )

            self.assertEqual(result.status, DurableRunStatus.COMPLETED)
            for path in config.runtime_root.iterdir():
                if path.is_file():
                    self.assertNotIn(canary.encode(), path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()
