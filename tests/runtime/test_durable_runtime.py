import tempfile
import unittest
import sqlite3
from dataclasses import replace
from pathlib import Path
from pydantic import SecretStr

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agent.architect.graph import (
    ArchitectRuntime,
    ResearchEvaluation,
    ResearchStep,
    create_architect_workflow,
)
from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.editing import (
    EditResult,
    EditStatus,
    WorkspaceEditor,
    WorkspaceTransaction,
)
from agent.graph import create_workflow_graph
from agent.outcome import WorkflowOutcome
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    BudgetSnapshot,
    DurableBudgetBoundary,
    DurableBudgetState,
    DurableCallResult,
    ModelCallResult,
    ResumeRequest,
    RunIdentity,
    StartRequest,
    UsageMeasurement,
    UsageRecord,
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
from agent.verification import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
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


class GuardedDeadlineFactory:
    def __init__(self) -> None:
        self.compiles = 0
        self.external_calls = 0

    def __call__(self, config, run_id, saver, clock):
        self.compiles += 1
        boundary = DurableBudgetBoundary(run_id, clock=clock)
        builder = StateGraph(TinyState)
        builder.add_node(
            "reserve_model",
            lambda state: boundary.reserve_model(
                state, "model", {"value": state.value}
            ),
        )

        def dispatch(state):
            deadline_update = boundary.guard_dispatch(state)
            if deadline_update:
                return deadline_update
            self.external_calls += 1
            value, update = boundary.capture_model(
                state,
                ModelCallResult(
                    state.value + 1,
                    UsageMeasurement(
                        UsageStatus.PARTIAL, 1, 1, 2, None, None
                    ),
                    "a" * 64,
                ),
            )
            return {**update, "value": value}

        builder.add_node("dispatch_model", dispatch)
        builder.add_node("settle_model", boundary.settle)
        builder.add_edge(START, "reserve_model")
        builder.add_edge("reserve_model", "dispatch_model")
        builder.add_edge("dispatch_model", "settle_model")
        builder.add_edge("settle_model", END)
        return builder.compile(
            checkpointer=saver,
            interrupt_after=["dispatch_model"] if self.compiles == 1 else None,
        )


class DurableRuntimeTests(RunConfigTestCase):
    def test_checkpoint_tuple_fields_accept_sequences_but_reject_scalars(self) -> None:
        usage = UsageRecord.unknown("call")
        values = (
            BudgetSnapshot(2, None, 200.0, [], []),
            DurableCallResult(["call"], [usage], ["d" * 64]),
            VerificationResult(
                "check",
                ["python"],
                ".",
                VerificationCheckStatus.PASS,
                0,
            ),
            EditResult(EditStatus.APPLIED, "app.py", task_ids=["task"]),
            WorkspaceTransaction(
                "app.py", True, "old", "new", "a" * 64, None, ["task"]
            ),
        )
        normalized = (
            values[0].reservations,
            values[0].usage,
            values[1].call_ids,
            values[1].usage,
            values[1].response_digests,
            values[2].argv,
            values[3].task_ids,
            values[4].task_ids,
        )
        self.assertTrue(all(isinstance(value, tuple) for value in normalized))

        invalid = (
            lambda: BudgetSnapshot(2, None, 200.0, "bad", ()),
            lambda: BudgetSnapshot(2, None, 200.0, (), 7),
            lambda: DurableCallResult("x", [usage], ["d" * 64]),
            lambda: DurableCallResult(["call"], 7, ["d" * 64]),
            lambda: DurableCallResult(["call"], [usage], "d" * 64),
            lambda: VerificationResult(
                "check",
                "python",
                ".",
                VerificationCheckStatus.PASS,
                0,
            ),
            lambda: EditResult(EditStatus.APPLIED, "app.py", task_ids="task"),
            lambda: WorkspaceTransaction(
                "app.py", True, "old", "new", "a" * 64, None, 7
            ),
        )
        for factory in invalid:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

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

    def test_real_production_chain_repairs_after_sqlite_close_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            spec = VerificationSpec("unit", ("unused",))
            config = self.make_config(
                root,
                verification_specs=(spec,),
                max_cost_usd=None,
                max_steps=8,
            )
            request = self.request(config)
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="app.py",
                        logical_task="change value",
                        atomic_tasks=[AtomicTask(atomic_task="change")],
                    )
                ]
            )

            def measured(value):
                return ModelCallResult(
                    value=value,
                    usage=UsageMeasurement(
                        UsageStatus.PARTIAL, 1, 1, 2, None, None
                    ),
                    response_digest="d" * 64,
                )

            architect_runtime = ArchitectRuntime(
                plan_next_step=lambda _: measured(
                    ResearchStep(reasoning="r", hypothesis="h")
                ),
                check_research_step=lambda _: measured(
                    ResearchEvaluation(is_valid=True, reasoning="ok")
                ),
                conduct_research=lambda _: measured(AIMessage(content="enough")),
                extract_implementation_plan=lambda _: measured(plan),
                load_codebase_structure=lambda: "app.py",
            )
            executor = DeveloperEditExecutor(WorkspaceEditor(workspace))

            def propose(values):
                before = values["file_content"]
                after = (
                    before.replace("value = 1", "value = 'BROKEN'")
                    if "BROKEN" not in before
                    else before.replace("value = 'BROKEN'", "value = 2")
                )
                return measured(
                    f"<<<<<<< SEARCH\n{before}=======\n{after}>>>>>>> REPLACE"
                )

            developer_runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: measured(AIMessage(content="ready")),
                propose_existing_file_edit=propose,
                propose_new_file=lambda _: self.fail("unexpected create"),
            )

            class Runner:
                def __init__(self) -> None:
                    self.statuses = iter(
                        (
                            VerificationCheckStatus.PASS,
                            VerificationCheckStatus.FAIL,
                            VerificationCheckStatus.PASS,
                        )
                    )

                def run(self, configured: VerificationSpec) -> VerificationResult:
                    status = next(self.statuses)
                    return VerificationResult.create(
                        name=configured.name,
                        argv=configured.argv,
                        cwd=configured.cwd,
                        status=status,
                        exit_code=(
                            0 if status is VerificationCheckStatus.PASS else 1
                        ),
                        stderr=(
                            "regression"
                            if status is VerificationCheckStatus.FAIL
                            else ""
                        ),
                    )

            runner = Runner()

            class ProductionFactory:
                def __init__(self) -> None:
                    self.compiles = 0

                def __call__(self, current_config, run_id, saver, clock):
                    self.compiles += 1
                    boundary = DurableBudgetBoundary(run_id, clock=clock)
                    architect = create_architect_workflow(
                        architect_runtime,
                        research_tools=[],
                        budget_boundary=boundary,
                    )
                    developer = create_developer_workflow(
                        developer_runtime,
                        research_tools=[],
                        budget_boundary=boundary,
                    )
                    builder = create_workflow_graph(
                        architect=architect,
                        developer=developer,
                        verification_specs=current_config.verification_specs,
                        verification_runner=runner,
                        workspace_root=current_config.workspace_root,
                        durable_runtime=True,
                        clock=clock,
                    )
                    return builder.compile(
                        checkpointer=saver,
                        interrupt_after=(
                            ["prepare_repair"] if self.compiles == 1 else None
                        ),
                    )

            factory = ProductionFactory()
            started = start_run(
                config,
                request,
                {
                    "implementation_research_scratchpad": [
                        HumanMessage(content="task")
                    ]
                },
                graph_factory=factory,
                clock=lambda: 100.0,
            )
            after_start = target.read_text(encoding="utf-8")
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
            self.assertEqual(after_start, "value = 'BROKEN'\n")
            self.assertEqual(
                resumed.status, DurableRunStatus.COMPLETED, resumed.message
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(
                resumed.state["outcome"], WorkflowOutcome.COMPLETED, resumed.state
            )
            self.assertEqual(
                resumed.state["verification_status"], VerificationStatus.VERIFIED
            )
            self.assertEqual(resumed.state["repair_attempts"], 1)
            self.assertEqual(resumed.state["budget"].steps_used, 8)
            self.assertEqual(
                resumed.state["budget"].deadline_at,
                100.0 + config.timeout_seconds,
            )
            self.assertEqual(resumed.state["run_identity"], request.identity)

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

    def test_pre_dispatch_deadline_result_resumes_at_settle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(
                Path(directory),
                verification_specs=(),
                max_cost_usd=None,
                timeout_seconds=100.0,
            )
            request = self.request(config)
            factory = GuardedDeadlineFactory()
            ticks = iter((100.0, 100.0, 201.0))

            started = start_run(
                config,
                request,
                {"value": 0},
                graph_factory=factory,
                clock=lambda: next(ticks),
            )
            resumed = resume_run(
                config,
                ResumeRequest(
                    request.identity,
                    request.run_config_digest,
                    request.agent_revision,
                ),
                graph_factory=factory,
                clock=lambda: 201.0,
            )

            self.assertEqual(started.status, DurableRunStatus.FAILED)
            self.assertEqual(started.error_code, "timeout_overrun")
            self.assertEqual(resumed.status, DurableRunStatus.FAILED)
            self.assertEqual(resumed.error_code, "timeout_overrun")
            self.assertEqual(factory.external_calls, 0)
            self.assertEqual(resumed.state["budget"].steps_used, 1)
            self.assertEqual(
                resumed.state["budget"].reservations[0].status.value,
                "failed",
            )
            self.assertEqual(
                resumed.state["budget"].usage[0].status,
                UsageStatus.UNKNOWN,
            )
            self.assertIsNone(resumed.state["budget"].usage[0].cost_microusd)

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
