import io
import json
import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import SecretStr

from agent.config import ModelSettings
from agent.editing import (
    EditErrorCode,
    EditOperation,
    EditProposal,
    EditStatus,
    WorkspaceEditor,
)
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionReason,
    AgentRevisionStatus,
    BudgetErrorCode,
    BudgetSnapshot,
    KnownSecretFilter,
    ModelRetryPolicy,
    RunConfig,
    RunIdentity,
    REDACTION_MARKER,
    StartRequest,
    TokenPricing,
    WorkspaceIdentity,
    semantic_config_digest,
)
from agent.runtime.__main__ import main
from agent.runtime.durable import DurableRunStatus, start_run
from agent.architect.graph import (
    ArchitectRuntime,
    ResearchEvaluation,
    ResearchStep,
    create_architect_workflow,
)
from agent.common.entities import (
    AtomicTask,
    ImplementationPlan,
    ImplementationTask,
    PlanStatus,
)
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.developer.state import DeveloperStatus
from agent.graph import create_workflow_graph
from agent.verification import (
    REPORT_SCHEMA,
    VerificationCase,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationReport,
    VerificationResult,
    VerificationSpec,
)


class KnownSecretFilterTests(unittest.TestCase):
    def test_stream_redacts_secret_split_across_chunks(self) -> None:
        secret_filter = KnownSecretFilter(("CANARY-123",))
        stream = secret_filter.stream()

        output = b"".join(
            (
                stream.feed(b"prefix CANARY-"),
                stream.feed(b"123 suffix"),
                stream.finish(),
            )
        )

        self.assertEqual(
            output,
            f"prefix {REDACTION_MARKER} suffix".encode("utf-8"),
        )
        self.assertTrue(stream.redacted)
        self.assertNotIn(b"CANARY-123", output)

    def test_stream_redacts_when_first_chunk_is_only_secret_prefix(self) -> None:
        secret_filter = KnownSecretFilter(("SECRET",))
        stream = secret_filter.stream()

        output = b"".join(
            (stream.feed(b"SE"), stream.feed(b"CRET"), stream.finish())
        )

        self.assertEqual(output, REDACTION_MARKER.encode("utf-8"))
        self.assertTrue(stream.redacted)

    def test_diagnostic_message_is_redacted_without_code_rejection(self) -> None:
        secret_filter = KnownSecretFilter(("CANARY-123",))

        result = secret_filter.sanitize(
            AIMessage(content="diagnostic CANARY-123"),
        )

        self.assertTrue(result.redacted)
        self.assertFalse(result.code_bearing)
        self.assertIn(REDACTION_MARKER, result.value.content)
        self.assertNotIn("CANARY-123", result.value.content)

    def test_state_update_rejects_code_bearing_secret(self) -> None:
        secret_filter = KnownSecretFilter(("CANARY-123",))

        safe = secret_filter.sanitize_node_update(
            {
                "implementation_research_scratchpad": [
                    HumanMessage(content="task CANARY-123")
                ],
                "current_file_content": "value = 'CANARY-123'\n",
            }
        )

        self.assertNotIn("CANARY-123", repr(safe))
        self.assertEqual(
            safe["runtime_error_code"], BudgetErrorCode.SENSITIVE_DATA_DETECTED
        )
        self.assertIn(REDACTION_MARKER, safe["current_file_content"])
        diagnostic = secret_filter.sanitize_node_update(
            {"runtime_message": f"provider exception CANARY-123"}
        )
        self.assertIn(REDACTION_MARKER, diagnostic["runtime_message"])
        self.assertNotIn("CANARY-123", diagnostic["runtime_message"])

    def test_workspace_source_secret_is_rejected_without_touching_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = "value = 'CANARY-123'\n"
            target.write_text(original, encoding="utf-8", newline="")
            before_mtime = target.stat().st_mtime_ns
            editor = WorkspaceEditor(
                root, secret_filter=KnownSecretFilter(("CANARY-123",))
            )

            snapshot = editor.snapshot("app.py")
            result = editor.apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash="a" * 64,
                    old_text="CANARY-123",
                    new_text="safe",
                )
            )

            self.assertEqual(snapshot.error_code, EditErrorCode.SENSITIVE_DATA_DETECTED)
            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(
                result.error_code, EditErrorCode.SENSITIVE_DATA_DETECTED
            )
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(target.stat().st_mtime_ns, before_mtime)

    def test_edit_proposal_secret_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = "value = 1\n"
            target.write_text(original, encoding="utf-8", newline="")
            before_mtime = target.stat().st_mtime_ns
            editor = WorkspaceEditor(
                root, secret_filter=KnownSecretFilter(("CANARY-123",))
            )

            result = editor.apply(
                EditProposal(
                    task_id="task-1.step-1",
                    path="app.py",
                    operation=EditOperation.EDIT,
                    base_hash=hashlib.sha256(original.encode("utf-8")).hexdigest(),
                    old_text="value = 1",
                    new_text="value = 'CANARY-123'",
                )
            )

            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(
                result.error_code, EditErrorCode.SENSITIVE_DATA_DETECTED
            )
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(target.stat().st_mtime_ns, before_mtime)

    def test_developer_model_output_is_filtered_before_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            secret_filter = KnownSecretFilter(("CANARY-123",))
            runtime = DeveloperRuntime(
                edit_executor=lambda: DeveloperEditExecutor(
                    WorkspaceEditor(root, secret_filter=secret_filter)
                ),
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: AIMessage(
                    content="diagnostic CANARY-123"
                ),
                propose_existing_file_edit=lambda _: (
                    "<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n"
                    ">>>>>>> REPLACE"
                ),
                propose_new_file=lambda _: "value = 2\n",
            )
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="app.py",
                        logical_task="update value",
                        atomic_tasks=[AtomicTask(atomic_task="change value")],
                    )
                ]
            )

            result = create_developer_workflow(
                runtime, secret_filter=secret_filter
            ).invoke({"implementation_plan": plan})

            self.assertNotIn("CANARY-123", repr(result))
            self.assertEqual(result["developer_status"], DeveloperStatus.COMPLETED)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_real_sqlite_state_files_and_cli_summary_contain_no_canary(self) -> None:
        canary = "CANARY-123"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime_root = root / "runtime"
            workspace.mkdir()
            (workspace / "app.py").write_text(
                "value = 1\n", encoding="utf-8", newline=""
            )
            config = RunConfig(
                workspace_root=workspace,
                runtime_root=runtime_root,
                model=ModelSettings(
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    base_url="https://api.deepseek.com",
                    api_key=SecretStr(canary),
                ),
                model_max_output_tokens=128,
                verification_specs=(VerificationSpec("unit", ("unused",)),),
                timeout_seconds=900.0,
                max_steps=12,
                max_cost_usd=Decimal("2"),
                pricing=TokenPricing(Decimal("0.1"), Decimal("0.2"), "test"),
                model_retry_policy=ModelRetryPolicy(max_attempts=1),
            )
            secret_filter = KnownSecretFilter.from_run_config(config)

            @tool
            def inspect_file(path: str) -> str:
                """Return a synthetic diagnostic containing the configured canary."""
                return f"tool output for {path}: {canary}"

            conduct_calls = 0

            def conduct_research(_):
                nonlocal conduct_calls
                conduct_calls += 1
                if conduct_calls == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "inspect_file",
                                "args": {"path": "app.py"},
                                "id": "tool-1",
                            }
                        ],
                    )
                return AIMessage(content=f"model diagnostic {canary}")

            architect_runtime = ArchitectRuntime(
                plan_next_step=lambda _: ResearchStep(
                    reasoning=f"reason {canary}", hypothesis="inspect"
                ),
                check_research_step=lambda _: ResearchEvaluation(
                    reasoning=f"check {canary}", is_valid=True
                ),
                conduct_research=conduct_research,
                extract_implementation_plan=lambda _: ImplementationPlan(
                    status=PlanStatus.READY,
                    tasks=[
                        ImplementationTask(
                            file_path="app.py",
                            logical_task="update value",
                            atomic_tasks=[
                                AtomicTask(
                                    atomic_task=f"update value {canary}"
                                )
                            ],
                        )
                    ],
                ),
                load_codebase_structure=lambda: "app.py",
            )

            class Runner:
                def run(self, spec):
                    return VerificationResult.create(
                        name=spec.name,
                        argv=spec.argv,
                        cwd=spec.cwd,
                        status=VerificationCheckStatus.PASS,
                        exit_code=0,
                        stdout=f"stdout {canary}",
                        stderr=f"stderr {canary}",
                        message=f"message {canary}",
                        report=VerificationReport(
                            check_id=spec.name,
                            report_schema=REPORT_SCHEMA,
                            cases=(
                                VerificationCase(
                                    check_id=spec.name,
                                    case_id="case",
                                    status=VerificationCaseStatus.PASS,
                                ),
                            ),
                        ),
                    )

            def factory(config, run_id, saver, clock):
                architect = create_architect_workflow(
                    architect_runtime,
                    research_tools=[inspect_file],
                    secret_filter=secret_filter,
                )
                developer = create_developer_workflow(
                    DeveloperRuntime(
                        edit_executor=lambda: DeveloperEditExecutor(
                            WorkspaceEditor(workspace, secret_filter=secret_filter)
                        ),
                        load_codebase_structure=lambda: "app.py",
                        research_atomic_task=lambda _: AIMessage(
                            content=f"developer diagnostic {canary}"
                        ),
                        propose_existing_file_edit=lambda _: (
                            "<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n"
                            ">>>>>>> REPLACE"
                        ),
                        propose_new_file=lambda _: "value = 2\n",
                    ),
                    research_tools=[],
                    secret_filter=secret_filter,
                )
                return create_workflow_graph(
                    architect=architect,
                    developer=developer,
                    verification_specs=config.verification_specs,
                    verification_runner=Runner(),
                    workspace_root=workspace,
                    durable_runtime=True,
                    clock=clock,
                    secret_filter=secret_filter,
                ).compile(checkpointer=saver)

            identity = RunIdentity(
                run_id="run-secret",
                thread_id="thread-secret",
                task_id="task-secret",
                workspace=WorkspaceIdentity.from_root(workspace),
            )
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                AgentCodeRevision(
                    None,
                    AgentRevisionStatus.UNKNOWN,
                    AgentRevisionReason.NOT_GIT,
                ),
            )
            started = start_run(
                config,
                request,
                {
                    "implementation_research_scratchpad": [
                        HumanMessage(content=f"initial task {canary}")
                    ]
                },
                graph_factory=factory,
                clock=lambda: 100.0,
            )

            self.assertEqual(started.status, DurableRunStatus.COMPLETED)
            self.assertNotIn(canary, repr(started.state))
            database = runtime_root / "checkpoints.sqlite"
            self.assertTrue(database.exists())
            for path in runtime_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(canary.encode(), path.read_bytes())

            output = io.StringIO()
            with (
                patch("agent.runtime.__main__.load_run_config", return_value=config),
                patch("agent.runtime.__main__.start_run", return_value=started),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "start",
                        "--run-id",
                        identity.run_id,
                        "--thread-id",
                        identity.thread_id,
                        "--task-id",
                        identity.task_id,
                        "--task",
                        canary,
                    ]
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(output.getvalue())
            self.assertNotIn(canary, output.getvalue())
            self.assertEqual(payload["run_id"], identity.run_id)


if __name__ == "__main__":
    unittest.main()
