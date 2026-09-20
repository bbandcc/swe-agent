import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from pydantic import SecretStr

from agent.architect.graph import (
    ArchitectRuntime,
    ResearchEvaluation,
    ResearchStep,
    create_architect_workflow,
)
from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.config import ModelSettings
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.editing import (
    TransactionResult,
    WorkspaceEditor,
    WorkspaceTransaction,
)
from agent.graph import AgentState, create_workflow_graph
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionReason,
    AgentRevisionStatus,
    BudgetErrorCode,
    BudgetSnapshot,
    DurableBudgetBoundary,
    KnownSecretFilter,
    ModelRetryPolicy,
    RunConfig,
    RunIdentity,
    REDACTION_MARKER,
    ResumeRequest,
    StartRequest,
    WorkspaceIdentity,
    resume_run,
    semantic_config_digest,
    start_run,
)
from agent.runtime.__main__ import main
from agent.runtime.durable import DurableRunStatus
from agent.verification import (
    REPORT_SCHEMA,
    VerificationCase,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationReport,
    VerificationResult,
    VerificationSpec,
)
from tests.runtime._config_support import RunConfigTestCase


class SecretFinalSealTests(RunConfigTestCase):
    def test_architect_secret_filter_only_stops_after_secret_model_error(self) -> None:
        canary = "S3_ARCHITECT_SECRET"
        calls = {"check": 0, "conduct": 0, "extract": 0, "tool": 0}

        def plan_next_step(_values):
            raise RuntimeError(f"architect provider leaked {canary}")

        def check_research_step(_values):
            calls["check"] += 1
            return ResearchEvaluation(reasoning="ok", is_valid=True)

        def conduct_research(_values):
            calls["conduct"] += 1
            return AIMessage(content="done")

        def extract_implementation_plan(_values):
            calls["extract"] += 1
            return ImplementationPlan(tasks=[])

        @tool
        def inspect_file(path: str) -> str:
            """Record whether a tool was invoked."""
            calls["tool"] += 1
            return path

        runtime = ArchitectRuntime(
            plan_next_step=plan_next_step,
            check_research_step=check_research_step,
            conduct_research=conduct_research,
            extract_implementation_plan=extract_implementation_plan,
            load_codebase_structure=lambda: "app.py",
        )
        result = create_architect_workflow(
            runtime,
            research_tools=[inspect_file],
            secret_filter=KnownSecretFilter((canary,)),
        ).invoke(
            {"implementation_research_scratchpad": [HumanMessage(content="task")]}
        )

        self.assertEqual(calls, {"check": 0, "conduct": 0, "extract": 0, "tool": 0})
        self.assertEqual(
            result["runtime_error_code"],
            BudgetErrorCode.SENSITIVE_DATA_DETECTED,
        )
        self.assertNotIn(canary, repr(result))

    def test_node_exception_canary_is_safe_in_wal_checkpoint(self) -> None:
        canary = "S3_FINAL_SECRET"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._secret_config(root, canary, verification_specs=())
            workspace_identity = WorkspaceIdentity.from_root(config.workspace_root)
            identity = RunIdentity(
                run_id="run-node-error",
                thread_id="thread-node-error",
                task_id="task-node-error",
                workspace=workspace_identity,
            )

            secret_filter = KnownSecretFilter.from_run_config(config)

            def raises(_: AgentState):
                raise RuntimeError(f"provider response leaked {canary}")

            child_builder = StateGraph(AgentState)
            child_builder.add_node("raises", secret_filter.wrap_node(raises))
            child_builder.add_edge(START, "raises")
            child_builder.add_edge("raises", END)
            child = child_builder.compile()
            database = config.runtime_root / "checkpoints.sqlite"
            config.runtime_root.mkdir(parents=True)
            connection = sqlite3.connect(database, check_same_thread=False)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal",
                )
                saver = SqliteSaver(connection)
                saver.setup()
                graph = create_workflow_graph(
                    architect=child,
                    developer=child,
                    verification_specs=(),
                    workspace_root=config.workspace_root,
                    durable_runtime=True,
                    secret_filter=secret_filter,
                ).compile(checkpointer=saver)
                result = graph.invoke(
                    {
                        "run_identity": identity,
                        "run_config_digest": semantic_config_digest(config),
                        "agent_revision": AgentCodeRevision(
                            None,
                            AgentRevisionStatus.UNKNOWN,
                            AgentRevisionReason.NOT_GIT,
                        ),
                        "budget": BudgetSnapshot.create(
                            max_steps=4,
                            max_cost_usd=Decimal("1"),
                            deadline_at=10_000.0,
                        ),
                    },
                    {
                        "configurable": {"thread_id": identity.thread_id},
                        "recursion_limit": 200,
                    },
                    durability="sync",
                )
                connection.commit()

                self.assertEqual(
                    result["runtime_error_code"],
                    BudgetErrorCode.SENSITIVE_DATA_DETECTED,
                )
                self.assertNotIn(canary, repr(result))
                self.assertEqual(result["outcome"].value, "failed")
                for suffix in ("", "-wal", "-shm"):
                    path = Path(f"{database}{suffix}")
                    self.assertTrue(path.exists(), suffix)
                    self.assertNotIn(canary.encode(), path.read_bytes(), suffix)
            finally:
                connection.close()

    def test_unexpected_errors_propagate_but_interrupts_are_not_swallowed(self) -> None:
        secret_filter = KnownSecretFilter(("S3_FINAL_SECRET",))

        def unexpected(_):
            raise RuntimeError("ordinary programming failure")

        with self.assertRaisesRegex(RuntimeError, "ordinary programming failure"):
            secret_filter.wrap_node(unexpected)({})

        def interrupt(_):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            secret_filter.wrap_node(interrupt)({})

        def system_exit(_):
            raise SystemExit("stop")

        with self.assertRaises(SystemExit):
            secret_filter.wrap_node(system_exit)({})

    def test_identity_secret_is_rejected_before_sqlite_and_cli_does_not_echo_it(
        self,
    ) -> None:
        canary = "S3_IDENTITY_SECRET"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._secret_config(root, canary, verification_specs=())
            normal_workspace = WorkspaceIdentity.from_root(config.workspace_root)
            digest = semantic_config_digest(config)
            revision = AgentCodeRevision(
                None, AgentRevisionStatus.UNKNOWN, AgentRevisionReason.NOT_GIT
            )

            for command in ("start", "resume"):
                for field in ("run_id", "thread_id", "task_id"):
                    with self.subTest(command=command, field=field):
                        identity = RunIdentity(
                            run_id=canary if field == "run_id" else "run-1",
                            thread_id=canary if field == "thread_id" else "thread-1",
                            task_id=canary if field == "task_id" else "task-1",
                            workspace=normal_workspace,
                        )
                        request = (
                            StartRequest(identity, digest, revision)
                            if command == "start"
                            else ResumeRequest(identity, digest, revision)
                        )
                        result = (
                            start_run(
                                config,
                                request,
                                {"value": 0},
                                graph_factory=lambda *_: self.fail(
                                    "identity secret must stop before graph/SQLite"
                                ),
                            )
                            if command == "start"
                            else resume_run(
                                config,
                                request,
                                graph_factory=lambda *_: self.fail(
                                    "identity secret must stop before graph/SQLite"
                                ),
                            )
                        )
                        self.assertEqual(
                            result.error_code,
                            BudgetErrorCode.SENSITIVE_DATA_DETECTED.value,
                        )
                        self.assertNotIn(canary, json.dumps(result.summary.to_dict()))
                        self.assertEqual(result.summary.run_id, REDACTION_MARKER)
                        self.assertFalse(config.runtime_root.exists())

            secret_workspace = root / canary
            secret_workspace.mkdir()
            workspace_config = self._secret_config(
                root,
                canary,
                workspace_root=secret_workspace,
                runtime_root=root / "workspace-runtime",
                verification_specs=(),
            )
            identity = RunIdentity(
                run_id="run-1",
                thread_id="thread-1",
                task_id="task-1",
                workspace=WorkspaceIdentity.from_root(secret_workspace),
            )
            result = start_run(
                workspace_config,
                StartRequest(identity, semantic_config_digest(workspace_config), revision),
                {"value": 0},
            )
            self.assertEqual(
                result.error_code, BudgetErrorCode.SENSITIVE_DATA_DETECTED.value
            )
            self.assertNotIn(canary, json.dumps(result.summary.to_dict()))
            self.assertFalse((root / "workspace-runtime").exists())

            output = io.StringIO()
            with (
                patch("agent.runtime.__main__.load_run_config", return_value=config),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "start",
                        "--run-id",
                        canary,
                        "--thread-id",
                        "thread-cli",
                        "--task-id",
                        "task-cli",
                        "--task",
                        "inspect",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertEqual(
                payload["error_code"],
                BudgetErrorCode.SENSITIVE_DATA_DETECTED.value,
            )
            self.assertNotIn(canary, output.getvalue())
            self.assertEqual(payload["run_id"], REDACTION_MARKER)

    def test_sensitive_edit_stops_parent_before_post_verification(self) -> None:
        canary = "S3_EDIT_SECRET"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "app.py"
            original = "value = 1\n"
            target.write_text(original, encoding="utf-8", newline="")
            before_mtime = target.stat().st_mtime_ns
            config = self._secret_config(
                root,
                canary,
                workspace_root=workspace,
                verification_specs=(
                    VerificationSpec(
                        "unit",
                        ("python", "-m", "pytest", "--junitxml", "report.xml"),
                        report_path="report.xml",
                    ),
                ),
            )
            secret_filter = KnownSecretFilter.from_run_config(config)
            architect = create_architect_workflow(
                ArchitectRuntime(
                    plan_next_step=lambda _: ResearchStep(
                        reasoning="inspect", hypothesis="inspect"
                    ),
                    check_research_step=lambda _: ResearchEvaluation(
                        reasoning="ok", is_valid=True
                    ),
                    conduct_research=lambda _: AIMessage(content="done"),
                    extract_implementation_plan=lambda _: ImplementationPlan(
                        tasks=[
                            ImplementationTask(
                                file_path="app.py",
                                logical_task="edit",
                                atomic_tasks=[AtomicTask(atomic_task="edit")],
                            )
                        ]
                    ),
                    load_codebase_structure=lambda: "app.py",
                ),
                secret_filter=secret_filter,
            )
            post_calls: list[str] = []

            class Runner:
                def run(self, spec):
                    post_calls.append(spec.name)
                    return VerificationResult.create(
                        name=spec.name,
                        argv=spec.argv,
                        cwd=spec.cwd,
                        status=VerificationCheckStatus.PASS,
                        exit_code=0,
                        report=VerificationReport(
                            check_id=spec.name,
                            report_schema=REPORT_SCHEMA,
                            cases=(
                                VerificationCase(
                                    check_id=spec.name,
                                    case_id="target",
                                    status=VerificationCaseStatus.PASS,
                                ),
                            ),
                        ),
                    )

            developer = create_developer_workflow(
                DeveloperRuntime(
                    edit_executor=lambda: DeveloperEditExecutor(
                        WorkspaceEditor(workspace, secret_filter=secret_filter)
                    ),
                    load_codebase_structure=lambda: "app.py",
                    research_atomic_task=lambda _: AIMessage(content="research"),
                    propose_existing_file_edit=lambda _: (
                        "<<<<<<< SEARCH\nvalue = 1\n=======\n"
                        f"value = '{canary}'\n>>>>>>> REPLACE"
                    ),
                    propose_new_file=lambda _: "value = 2\n",
                ),
                secret_filter=secret_filter,
            )

            identity = RunIdentity(
                run_id="run-edit-secret",
                thread_id="thread-edit-secret",
                task_id="task-edit-secret",
                workspace=WorkspaceIdentity.from_root(workspace),
            )
            result = start_run(
                config,
                StartRequest(identity, semantic_config_digest(config), AgentCodeRevision(None, AgentRevisionStatus.UNKNOWN, AgentRevisionReason.NOT_GIT)),
                {"implementation_research_scratchpad": []},
                graph_factory=lambda _config, _run_id, saver, clock: create_workflow_graph(
                    architect=architect,
                    developer=developer,
                    verification_specs=config.verification_specs,
                    verification_runner=Runner(),
                    workspace_root=workspace,
                    durable_runtime=True,
                    clock=clock,
                    secret_filter=secret_filter,
                ).compile(checkpointer=saver),
                clock=lambda: 100.0,
            )

            self.assertEqual(result.status, DurableRunStatus.FAILED)
            self.assertEqual(
                result.error_code, BudgetErrorCode.SENSITIVE_DATA_DETECTED.value
            )
            self.assertEqual(post_calls, ["unit"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(target.stat().st_mtime_ns, before_mtime)
            self.assertNotIn(canary, repr(result.state))

    def test_code_bearing_filter_error_stops_before_next_model_reservation(self) -> None:
        canary = "S3_WORKING_COPY_SECRET"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._secret_config(root, canary, verification_specs=())
            secret_filter = KnownSecretFilter.from_run_config(config)
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="app.py",
                        logical_task="edit",
                        atomic_tasks=[AtomicTask(atomic_task="edit")],
                    )
                ]
            )
            begin_calls = 0
            model_calls = 0

            def research(_step):
                nonlocal model_calls
                model_calls += 1
                return AIMessage(content="unused")

            class LeakyExecutor:
                canonical_plan_path = staticmethod(lambda path: path)

                def begin(self, path):
                    nonlocal begin_calls
                    begin_calls += 1
                    return TransactionResult(
                        transaction=WorkspaceTransaction(
                            path=path,
                            existed=True,
                            original_content="safe\n",
                            working_content=f"unsafe {canary}\n",
                            base_hash="a" * 64,
                            original_mode=0o644,
                        )
                    )

            runtime = DeveloperRuntime(
                edit_executor=lambda: LeakyExecutor(),
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=research,
                propose_existing_file_edit=lambda _: "unused",
                propose_new_file=lambda _: "unused",
            )
            boundary = DurableBudgetBoundary("run-filter", clock=lambda: 1.0)
            graph = create_developer_workflow(
                runtime,
                budget_boundary=boundary,
                secret_filter=secret_filter,
            )
            result = graph.invoke(
                {
                    "implementation_plan": plan,
                    "budget": BudgetSnapshot.create(
                        max_steps=4,
                        max_cost_usd=Decimal("1"),
                        deadline_at=100.0,
                    ),
                }
            )

            self.assertEqual(begin_calls, 1)
            self.assertEqual(model_calls, 0)
            self.assertEqual(result["budget"].reservations, ())
            self.assertEqual(
                result["runtime_error_code"],
                BudgetErrorCode.SENSITIVE_DATA_DETECTED,
            )

            proposal_update = secret_filter.sanitize_node_update(
                {"edit_proposal": {"new_text": canary}}
            )
            self.assertEqual(
                proposal_update["runtime_error_code"],
                BudgetErrorCode.SENSITIVE_DATA_DETECTED,
            )

    def _secret_config(self, root: Path, canary: str, **changes) -> RunConfig:
        config = self.make_config(
            root,
            model=ModelSettings(
                provider="deepseek",
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                api_key=SecretStr(canary),
            ),
            verification_specs=(),
            max_cost_usd=Decimal("1"),
            model_retry_policy=ModelRetryPolicy(max_attempts=1),
        )
        return replace(config, **changes)


if __name__ == "__main__":
    unittest.main()
