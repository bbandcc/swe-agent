import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from decimal import Decimal
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.editing import WriteIntent, WorkspaceEditor
from agent.editing.text import sha256
from agent.runtime import (
    BudgetErrorCode,
    BudgetSnapshot,
    DurableBudgetBoundary,
    ModelCallResult,
    UsageMeasurement,
    UsageStatus,
)
from agent.runtime.checkpointing import checkpoint_serializer


_CRASH_WORKER = r'''
import json
import os
from decimal import Decimal
from pathlib import Path

from langchain_core.messages import AIMessage
from pydantic import SecretStr

from agent.architect.graph import create_architect_workflow
from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.config import ModelSettings
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.editing import WorkspaceEditor
from agent.graph import create_workflow_graph
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    DurableBudgetBoundary,
    ModelCallResult,
    RunConfig,
    RunIdentity,
    StartRequest,
    ResumeRequest,
    TokenPricing,
    UsageMeasurement,
    UsageStatus,
    WorkspaceIdentity,
    semantic_config_digest,
)
from agent.runtime.durable import resume_run, start_run

root = Path(os.environ["S3_RECOVERY_ROOT"])
workspace = root / "workspace"
runtime_root = root / "runtime"
log_path = root / "model-calls.log"
workspace.mkdir(parents=True, exist_ok=True)
runtime_root.mkdir(parents=True, exist_ok=True)

config = RunConfig(
    workspace_root=workspace,
    runtime_root=runtime_root,
    model=ModelSettings(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key=SecretStr("test-secret"),
    ),
    model_max_output_tokens=128,
    verification_specs=(),
    timeout_seconds=900.0,
    max_steps=8,
    max_cost_usd=None,
    pricing=TokenPricing(
        input_cost_per_million_tokens=Decimal("0.25"),
        output_cost_per_million_tokens=Decimal("0.50"),
        source="test",
    ),
)
identity = RunIdentity(
    run_id="recovery-run",
    thread_id="recovery-thread",
    task_id="recovery-task",
    workspace=WorkspaceIdentity.from_root(workspace),
)
revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
request = StartRequest(identity, semantic_config_digest(config), revision)
is_create = os.environ.get("S3_RECOVERY_CREATE") == "1"
paths = (
    ("empty.py",)
    if is_create
    else (
        ("a.py", "b.py")
        if os.environ.get("S3_RECOVERY_MULTI") == "1"
        else ("app.py",)
    )
)
plan = ImplementationPlan(
    tasks=[
        ImplementationTask(
            file_path=path,
            logical_task="change value",
            atomic_tasks=[AtomicTask(atomic_task="change")],
        )
        for path in paths
    ]
)


def measured(value):
    return ModelCallResult(
        value=value,
        usage=UsageMeasurement(UsageStatus.PARTIAL, 1, 1, 2, None, None),
        response_digest="d" * 64,
    )


class CrashExecutor:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def commit(self, transaction):
        crash_path = os.environ.get("S3_RECOVERY_CRASH_PATH")
        should_crash = not crash_path or transaction.path == crash_path
        if os.environ.get("S3_RECOVERY_CRASH") == "before" and should_crash:
            os._exit(98)
        result = self.delegate.commit(transaction)
        if os.environ.get("S3_RECOVERY_CRASH") == "1" and should_crash:
            log_path.write_text(log_path.read_text(encoding="utf-8") + "commit\n", encoding="utf-8")
            os._exit(97)
        return result


def factory(current_config, run_id, saver, clock):
    boundary = DurableBudgetBoundary(run_id, clock=clock)
    delegate = DeveloperEditExecutor(WorkspaceEditor(current_config.workspace_root))
    executor = CrashExecutor(delegate)

    def record_call(value):
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(value + "\n")

    runtime = DeveloperRuntime(
        edit_executor=lambda: executor,
        load_codebase_structure=lambda: "app.py",
        research_atomic_task=lambda _: (record_call("research"), measured(AIMessage(content="ready")))[1],
        propose_existing_file_edit=lambda _: (record_call("edit"), measured("<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE"))[1],
        propose_new_file=(
            lambda _: (record_call("create"), measured(""))[1]
            if is_create
            else (_ for _ in ()).throw(AssertionError("unexpected create"))
        ),
    )
    developer = create_developer_workflow(
        runtime,
        research_tools=[],
        budget_boundary=boundary,
        run_id=run_id,
        task_id=identity.task_id,
    )
    interrupt = os.environ.get("S3_RECOVERY_INTERRUPT")
    if interrupt == "before_intent":
        developer = developer.builder.compile(
            interrupt_before=["prepare_write_intent"]
        )
    elif interrupt == "after_commit":
        developer = developer.builder.compile(
            interrupt_after=["commit_file_transaction"]
        )
    builder = create_workflow_graph(
        architect=lambda _: {"implementation_plan": plan},
        developer=developer,
        verification_specs=(),
        workspace_root=current_config.workspace_root,
        durable_runtime=True,
        clock=clock,
    )
    return builder.compile(checkpointer=saver)


if os.environ.get("S3_RECOVERY_MODE") == "start":
    result = start_run(
        config,
        request,
        {"implementation_research_scratchpad": []},
        graph_factory=factory,
        clock=lambda: 100.0,
    )
    print(json.dumps({"status": result.status.value}))
else:
    result = resume_run(
        config,
        ResumeRequest(identity, request.run_config_digest, revision),
        graph_factory=factory,
        clock=lambda: 150.0,
    )
    state = result.state or {}
    recovery = state.get("last_recovery_result")
    print(
        json.dumps(
            {
                "status": result.status.value,
                "recovery": getattr(getattr(recovery, "status", None), "value", None),
                "pending": state.get("pending_write") is not None,
                "steps": getattr(state.get("budget"), "steps_used", None),
                "calls": len(log_path.read_text(encoding="utf-8").splitlines()) if log_path.exists() else 0,
            }
        )
    )
'''


class DurableWriteRecoveryTests(unittest.TestCase):
    def _compiled_child_with_pending_intent(
        self, root: Path, *, path: str = "app.py"
    ):
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n", encoding="utf-8", newline="")
        connection = sqlite3.connect(
            root / "child-checkpoint.sqlite", check_same_thread=False
        )
        saver = SqliteSaver(connection, serde=checkpoint_serializer())
        saver.setup()
        calls: list[str] = []

        def measured(value):
            return ModelCallResult(
                value=value,
                usage=UsageMeasurement(
                    UsageStatus.PARTIAL, 1, 1, 2, None, None
                ),
                response_digest="d" * 64,
            )

        runtime = DeveloperRuntime(
            edit_executor=lambda: DeveloperEditExecutor(WorkspaceEditor(workspace)),
            load_codebase_structure=lambda: path,
            research_atomic_task=lambda _: (
                calls.append("research"),
                measured(AIMessage(content="ready")),
            )[1],
            propose_existing_file_edit=lambda _: (
                calls.append("edit"),
                measured(
                    "<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE"
                ),
            )[1],
            propose_new_file=lambda _: self.fail("unexpected create"),
        )
        developer = create_developer_workflow(
            runtime,
            research_tools=[],
            budget_boundary=DurableBudgetBoundary(
                "recovery-run", clock=lambda: 100.0
            ),
            run_id="recovery-run",
            task_id="recovery-task",
        )
        compiled = developer.builder.compile(
            checkpointer=saver,
            interrupt_after=["prepare_write_intent"],
        )
        plan = ImplementationPlan(
            tasks=[
                ImplementationTask(
                    file_path=path,
                    logical_task="change value",
                    atomic_tasks=[AtomicTask(atomic_task="change")],
                )
            ]
        )
        thread_config = {"configurable": {"thread_id": "recovery-thread"}}
        compiled.invoke(
            {
                "implementation_plan": plan,
                "budget": BudgetSnapshot.create(
                    max_steps=4, max_cost_usd=None, deadline_at=200.0
                ),
            },
            thread_config,
            durability="sync",
        )
        state = compiled.get_state(thread_config).values
        self.assertIsNotNone(state.get("pending_write"))
        self.assertIsNotNone(state.get("current_file_transaction"))
        return connection, compiled, thread_config, state, calls, workspace

    def _run_worker(
        self,
        root: Path,
        *,
        mode: str,
        crash: str,
        multi: bool = False,
        crash_path: str | None = None,
        create: bool = False,
        interrupt: str = "",
    ):
        env = os.environ.copy()
        env.update(
            {
                "S3_RECOVERY_ROOT": str(root),
                "S3_RECOVERY_MODE": mode,
                "S3_RECOVERY_CRASH": crash,
                "S3_RECOVERY_MULTI": "1" if multi else "0",
                "S3_RECOVERY_CRASH_PATH": crash_path or "",
                "S3_RECOVERY_CREATE": "1" if create else "0",
                "S3_RECOVERY_INTERRUPT": interrupt,
                "PYTHONPATH": str(Path.cwd()),
            }
        )
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(_CRASH_WORKER)],
            cwd=Path.cwd(),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_durable_stale_write_intent_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection, compiled, thread_config, state, calls, workspace = (
                self._compiled_child_with_pending_intent(root)
            )
            try:
                pending = state["pending_write"]
                transaction = state["current_file_transaction"]
                stale = WriteIntent.create(
                    run_id=pending.run_id,
                    task_id=pending.task_id,
                    task_index=pending.task_index + 1,
                    repair_attempt=pending.repair_attempt,
                    transaction=transaction,
                    expected_after_hash=sha256(
                        transaction.working_content.encode("utf-8")
                    ),
                )
                compiled.update_state(
                    thread_config,
                    {"pending_write": stale},
                    as_node="prepare_write_intent",
                )
                target = workspace / "app.py"
                before = target.stat()
                resumed = compiled.invoke(
                    None, thread_config, durability="sync"
                )

                self.assertEqual(
                    resumed["runtime_error_code"],
                    BudgetErrorCode.RECOVERY_CONFLICT,
                )
                self.assertEqual(resumed["pending_write"], stale)
                self.assertEqual(resumed["current_task_idx"], 0)
                self.assertEqual(calls, ["research", "edit"])
                self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")
                self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
            finally:
                connection.close()

    def test_durable_pending_directory_link_replacement_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection, compiled, thread_config, state, calls, workspace = (
                self._compiled_child_with_pending_intent(root, path="nested/app.py")
            )
            try:
                external = root / "external"
                external.mkdir()
                external_file = external / "app.py"
                external_file.write_text("outside\n", encoding="utf-8", newline="")
                nested = workspace / "nested"
                shutil.rmtree(nested)
                try:
                    if os.name == "nt":
                        linked = subprocess.run(
                            ["cmd", "/c", "mklink", "/J", str(nested), str(external)],
                            capture_output=True,
                            text=True,
                        )
                        if linked.returncode != 0:
                            self.skipTest("directory junctions are unavailable")
                    else:
                        nested.symlink_to(external, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"directory links are unavailable: {error}")

                external_before = external_file.stat()
                resumed = compiled.invoke(
                    None, thread_config, durability="sync"
                )

                self.assertEqual(
                    resumed["runtime_error_code"],
                    BudgetErrorCode.RECOVERY_CONFLICT,
                )
                self.assertIsNotNone(resumed["pending_write"])
                self.assertEqual(resumed["current_task_idx"], 0)
                self.assertEqual(calls, ["research", "edit"])
                self.assertEqual(external_file.read_text(encoding="utf-8"), "outside\n")
                self.assertEqual(
                    external_file.stat().st_mtime_ns, external_before.st_mtime_ns
                )
            finally:
                connection.close()

    def test_sqlite_resume_reconciles_replace_after_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
            first = self._run_worker(root, mode="start", crash="1")
            self.assertEqual(first.returncode, 97, first.stderr)
            self.assertEqual(
                (workspace / "app.py").read_text(encoding="utf-8"),
                "value = 2\n",
            )
            calls_before = (root / "model-calls.log").read_text(encoding="utf-8").splitlines()
            self.assertIn("commit", calls_before)

            resumed = self._run_worker(root, mode="resume", crash="0")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["recovery"], "already_applied")
            self.assertFalse(payload["pending"])
            self.assertEqual(payload["calls"], len(calls_before))
            self.assertEqual(
                (workspace / "app.py").read_text(encoding="utf-8"),
                "value = 2\n",
            )
            repeated = self._run_worker(root, mode="resume", crash="0")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_payload = json.loads(repeated.stdout.strip().splitlines()[-1])
            self.assertEqual(repeated_payload["status"], "completed")
            self.assertEqual(repeated_payload["calls"], len(calls_before))

    def test_sqlite_resume_applies_checkpointed_intent_that_was_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
            first = self._run_worker(root, mode="start", crash="before")
            self.assertEqual(first.returncode, 98, first.stderr)
            self.assertEqual(
                (workspace / "app.py").read_text(encoding="utf-8"),
                "value = 1\n",
            )
            calls_before = (root / "model-calls.log").read_text(encoding="utf-8").splitlines()
            self.assertNotIn("commit", calls_before)

            resumed = self._run_worker(root, mode="resume", crash="0")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["recovery"], "safe_to_apply")
            self.assertFalse(payload["pending"])
            self.assertEqual(payload["calls"], len(calls_before))
            self.assertEqual(
                (workspace / "app.py").read_text(encoding="utf-8"),
                "value = 2\n",
            )

    def test_sqlite_resume_reconciles_create_before_and_after_commit(self):
        for crash in ("before", "1"):
            with self.subTest(crash=crash), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                workspace.mkdir()
                first = self._run_worker(
                    root, mode="start", crash=crash, create=True
                )
                self.assertEqual(first.returncode, 98 if crash == "before" else 97, first.stderr)
                target = workspace / "empty.py"
                if crash == "before":
                    self.assertFalse(target.exists())
                else:
                    self.assertTrue(target.exists())
                    self.assertEqual(target.read_bytes(), b"")

                resumed = self._run_worker(
                    root, mode="resume", crash="0", create=True
                )
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                payload = json.loads(resumed.stdout.strip().splitlines()[-1])
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(
                    payload["recovery"],
                    "safe_to_apply" if crash == "before" else "already_applied",
                )
                self.assertTrue(target.exists())
                self.assertEqual(target.read_bytes(), b"")

    def test_sqlite_resume_after_intent_boundary_does_not_replay_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            first = self._run_worker(
                root, mode="start", crash="0", interrupt="before_intent"
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_payload = json.loads(first.stdout.strip().splitlines()[-1])
            self.assertEqual(first_payload["status"], "paused")
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")
            calls_before = (root / "model-calls.log").read_text(encoding="utf-8").splitlines()

            resumed = self._run_worker(root, mode="resume", crash="0")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["recovery"], "safe_to_apply")
            self.assertEqual(payload["calls"], len(calls_before))
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_sqlite_resume_after_commit_result_checkpoint_only_clears_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            first = self._run_worker(
                root, mode="start", crash="0", interrupt="after_commit"
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_payload = json.loads(first.stdout.strip().splitlines()[-1])
            self.assertEqual(first_payload["status"], "paused")
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            calls_before = (root / "model-calls.log").read_text(encoding="utf-8").splitlines()

            resumed = self._run_worker(root, mode="resume", crash="0")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["calls"], len(calls_before))
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_sqlite_resume_stops_on_third_value_without_clearing_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            first = self._run_worker(root, mode="start", crash="before")
            self.assertEqual(first.returncode, 98, first.stderr)
            target.write_text("value = 99\n", encoding="utf-8")
            calls_before = (root / "model-calls.log").read_text(encoding="utf-8").splitlines()

            resumed = self._run_worker(root, mode="resume", crash="0")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["recovery"], "conflict")
            self.assertTrue(payload["pending"])
            self.assertEqual(payload["calls"], len(calls_before))
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 99\n")

    def test_multi_file_partial_commit_resumes_only_pending_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "a.py").write_text("value = 1\n", encoding="utf-8")
            (workspace / "b.py").write_text("value = 1\n", encoding="utf-8")
            first = self._run_worker(
                root,
                mode="start",
                crash="before",
                multi=True,
                crash_path="b.py",
            )
            self.assertEqual(first.returncode, 98, first.stderr)
            self.assertEqual((workspace / "a.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((workspace / "b.py").read_text(encoding="utf-8"), "value = 1\n")
            calls_before = (root / "model-calls.log").read_text(encoding="utf-8").splitlines()

            resumed = self._run_worker(
                root,
                mode="resume",
                crash="0",
                multi=True,
                crash_path="b.py",
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["recovery"], "safe_to_apply")
            self.assertEqual(payload["calls"], len(calls_before))
            self.assertEqual((workspace / "a.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((workspace / "b.py").read_text(encoding="utf-8"), "value = 2\n")


if __name__ == "__main__":
    unittest.main()
