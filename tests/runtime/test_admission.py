import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent.config import ModelSettings
from agent.runtime import (
    AdmissionErrorCode,
    AdmissionKey,
    AdmissionLockError,
    AdmissionStatus,
    AgentCodeRevision,
    AgentRevisionStatus,
    DurableBudgetState,
    DurableRunResult,
    DurableRunStatus,
    RunConfig,
    RunIdentity,
    ResumeRequest,
    StartRequest,
    WorkspaceAdmissionLock,
    WorkspaceIdentity,
    semantic_config_digest,
    resume_run,
    start_run,
)
from agent.runtime.__main__ import main
from tests.runtime._config_support import RunConfigTestCase


class AdmissionState(DurableBudgetState):
    run_identity: RunIdentity | None = None
    run_config_digest: str | None = None
    agent_revision: AgentCodeRevision | None = None
    value: int = 0


_LOCK_PROCESS = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from agent.runtime import (
        AdmissionStatus, AgentRevisionStatus, RunIdentity,
        WorkspaceAdmissionLock, WorkspaceIdentity,
    )

    workspace = Path(sys.argv[1])
    runtime = Path(sys.argv[2])
    thread_id = sys.argv[3]
    hold = float(sys.argv[4])
    identity = RunIdentity(
        run_id="run-" + thread_id,
        thread_id=thread_id,
        task_id="task-" + thread_id,
        workspace=WorkspaceIdentity.from_root(workspace),
    )
    lock = WorkspaceAdmissionLock(runtime, identity)
    status = lock.acquire()
    print(status.value, flush=True)
    if status is AdmissionStatus.ACQUIRED:
        try:
            time.sleep(hold)
        finally:
            lock.release()
    """
)


_DURABLE_PROCESS = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from langchain_core.messages import HumanMessage
    from langgraph.constants import END, START
    from langgraph.graph import StateGraph
    from agent.config import ModelSettings
    from agent.runtime import (
        AgentCodeRevision, AgentRevisionStatus, DurableBudgetState,
        RunConfig, RunIdentity, StartRequest, WorkspaceIdentity,
        semantic_config_digest, start_run,
    )

    class State(DurableBudgetState):
        run_identity: RunIdentity | None = None
        run_config_digest: str | None = None
        agent_revision: AgentCodeRevision | None = None
        value: int = 0

    workspace = Path(sys.argv[1])
    runtime = Path(sys.argv[2])
    marker = Path(sys.argv[3])
    thread_id = sys.argv[4]
    hold = float(sys.argv[5])
    config = RunConfig(
        workspace_root=workspace,
        runtime_root=runtime,
        model=ModelSettings("deepseek", "deepseek-v4-flash", "https://api.deepseek.com", None),
        model_max_output_tokens=64,
        verification_specs=(),
        timeout_seconds=30.0,
        max_steps=4,
    )
    identity = RunIdentity(
        run_id="run-" + thread_id,
        thread_id=thread_id,
        task_id="task-" + thread_id,
        workspace=WorkspaceIdentity.from_root(workspace),
    )
    request = StartRequest(
        identity,
        semantic_config_digest(config),
        AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN),
    )

    class Factory:
        def __init__(self):
            self.calls = 0

        def __call__(self, config, run_id, saver, clock):
            self.calls += 1
            print("factory", flush=True)
            builder = StateGraph(State)

            def side_effect(state):
                with marker.open("a", encoding="utf-8") as handle:
                    handle.write(thread_id + "\\n")
                time.sleep(hold)
                return {"value": state.value + 1}

            builder.add_node("side_effect", side_effect)
            builder.add_edge(START, "side_effect")
            builder.add_edge("side_effect", END)
            return builder.compile(checkpointer=saver)

    factory = Factory()
    result = start_run(
        config,
        request,
        {"value": 0, "implementation_research_scratchpad": [HumanMessage(content="task")]},
        graph_factory=factory,
    )
    print(
        json.dumps(
            {"status": result.status.value, "error": result.error_code, "factory": factory.calls},
            sort_keys=True,
        ),
        flush=True,
    )
    """
)


class AdmissionLockTests(RunConfigTestCase):
    def _identity(self, workspace: Path, thread_id: str = "thread-1") -> RunIdentity:
        return RunIdentity(
            run_id="run-" + thread_id,
            thread_id=thread_id,
            task_id="task-" + thread_id,
            workspace=WorkspaceIdentity.from_root(workspace),
        )

    @staticmethod
    def _spawn_lock(workspace: Path, runtime: Path, thread_id: str, hold: float):
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                _LOCK_PROCESS,
                str(workspace),
                str(runtime),
                thread_id,
                str(hold),
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def _read_json_output(process: subprocess.Popen[str]) -> dict[str, object]:
        stdout, stderr = process.communicate(timeout=60)
        if process.returncode != 0:
            raise AssertionError(f"subprocess failed: {stderr}\n{stdout}")
        if not stdout.splitlines():
            raise AssertionError(f"subprocess produced no JSON: {stderr}")
        return json.loads(stdout.splitlines()[-1])

    @staticmethod
    def _wait_process(process: subprocess.Popen[str]) -> str:
        stdout, stderr = process.communicate(timeout=60)
        if process.returncode != 0:
            raise AssertionError(f"subprocess failed: {stderr}\n{stdout}")
        return stdout

    def test_key_is_hashed_and_workspace_scope_ignores_thread_for_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime"
            alternate_runtime = root / "alternate-runtime"
            first = WorkspaceAdmissionLock(runtime, self._identity(workspace, "thread-a"))
            second = WorkspaceAdmissionLock(runtime, self._identity(workspace, "thread-b"))
            alternate = WorkspaceAdmissionLock(
                alternate_runtime, self._identity(workspace, "thread-a")
            )

            self.assertEqual(first.acquire(), AdmissionStatus.ACQUIRED)
            self.assertEqual(second.acquire(), AdmissionStatus.BUSY)
            self.assertNotIn("thread-a", first.lock_path.name)
            self.assertEqual(first.lock_path.parent, runtime / "locks")
            self.assertNotEqual(first.workspace_lock_path, first.runtime_lock_path)
            self.assertNotIn("thread-a", first.workspace_lock_path.name)
            self.assertEqual(len(first.key.workspace_key), 64)
            self.assertEqual(len(first.key.runtime_thread_key), 64)
            self.assertEqual(len(first.key.stable_key), 64)
            self.assertNotEqual(first.key.stable_key, second.key.stable_key)
            self.assertEqual(first.key.workspace_key, alternate.key.workspace_key)
            self.assertNotEqual(
                first.key.runtime_thread_key,
                alternate.key.runtime_thread_key,
            )
            first.release()
            self.assertEqual(second.acquire(), AdmissionStatus.ACQUIRED)
            second.release()

    def test_runtime_thread_scope_blocks_different_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_workspace = root / "workspace-a"
            second_workspace = root / "workspace-b"
            first_workspace.mkdir()
            second_workspace.mkdir()
            runtime = root / "runtime"
            first = WorkspaceAdmissionLock(
                runtime, self._identity(first_workspace, "same-thread")
            )
            second = WorkspaceAdmissionLock(
                runtime, self._identity(second_workspace, "same-thread")
            )
            self.assertEqual(first.acquire(), AdmissionStatus.ACQUIRED)
            self.assertEqual(second.acquire(), AdmissionStatus.BUSY)
            first.release()
            self.assertEqual(second.acquire(), AdmissionStatus.ACQUIRED)
            second.release()

    def test_different_workspaces_can_be_admitted_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_workspace = root / "workspace-a"
            second_workspace = root / "workspace-b"
            first_workspace.mkdir()
            second_workspace.mkdir()
            runtime = root / "runtime"
            first = self._spawn_lock(first_workspace, runtime, "a", 0.2)
            second = self._spawn_lock(second_workspace, runtime, "b", 0.2)
            self.assertEqual(first.stdout.readline().strip(), "acquired")
            self.assertEqual(second.stdout.readline().strip(), "acquired")
            self._wait_process(first)
            self._wait_process(second)

    def test_normal_release_and_forced_termination_do_not_leave_busy_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime-a"
            alternate_runtime = root / "runtime-b"

            holder = self._spawn_lock(workspace, runtime, "holder", 0.1)
            self.assertEqual(holder.stdout.readline().strip(), "acquired")
            self._wait_process(holder)
            next_process = self._spawn_lock(
                workspace, alternate_runtime, "next", 0.1
            )
            self.assertEqual(next_process.stdout.readline().strip(), "acquired")
            self._wait_process(next_process)

            crashed = self._spawn_lock(workspace, runtime, "crashed", 30.0)
            self.assertEqual(crashed.stdout.readline().strip(), "acquired")
            crashed.terminate()
            crashed.wait(timeout=10)
            crashed.communicate(timeout=10)
            recovered_workspace = self._spawn_lock(
                workspace, alternate_runtime, "recovered", 0.1
            )
            self.assertEqual(recovered_workspace.stdout.readline().strip(), "acquired")
            self._wait_process(recovered_workspace)
            other_workspace = root / "other-workspace"
            other_workspace.mkdir()
            recovered_runtime = self._spawn_lock(
                other_workspace, runtime, "crashed", 0.1
            )
            self.assertEqual(recovered_runtime.stdout.readline().strip(), "acquired")
            self._wait_process(recovered_runtime)

    def test_durable_busy_result_happens_before_graph_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=())
            identity = self._identity(config.workspace_root)
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN),
            )
            holder = WorkspaceAdmissionLock(config.runtime_root, identity)
            self.assertEqual(holder.acquire(), AdmissionStatus.ACQUIRED)
            factory_calls = 0

            def factory(*_args):
                nonlocal factory_calls
                factory_calls += 1
                raise AssertionError("BUSY must stop before graph construction")

            try:
                result = start_run(
                    config,
                    request,
                    {"value": 0},
                    graph_factory=factory,
                )
            finally:
                holder.release()

            self.assertEqual(result.status, DurableRunStatus.REJECTED)
            self.assertEqual(result.error_code, AdmissionErrorCode.BUSY.value)
            self.assertEqual(factory_calls, 0)

    def test_resume_busy_result_happens_before_sqlite_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=())
            identity = self._identity(config.workspace_root, "resume-thread")
            revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
            request = ResumeRequest(
                identity,
                semantic_config_digest(config),
                revision,
            )
            holder = WorkspaceAdmissionLock(config.runtime_root, identity)
            self.assertEqual(holder.acquire(), AdmissionStatus.ACQUIRED)
            factory_calls = 0

            def factory(*_args):
                nonlocal factory_calls
                factory_calls += 1
                raise AssertionError("BUSY must stop before SQLite preflight")

            try:
                result = resume_run(
                    config,
                    request,
                    graph_factory=factory,
                )
            finally:
                holder.release()

            self.assertEqual(result.status, DurableRunStatus.REJECTED)
            self.assertEqual(result.error_code, AdmissionErrorCode.BUSY.value)
            self.assertEqual(factory_calls, 0)

    def test_unexpected_graph_error_releases_admission_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=())
            identity = self._identity(config.workspace_root)
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN),
            )

            def factory(*_args):
                raise RuntimeError("expected test failure")

            with self.assertRaisesRegex(RuntimeError, "expected test failure"):
                start_run(config, request, {"value": 0}, graph_factory=factory)

            lock = WorkspaceAdmissionLock(config.runtime_root, identity)
            self.assertEqual(lock.acquire(), AdmissionStatus.ACQUIRED)
            lock.release()

    def test_cli_busy_is_structured_json_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), verification_specs=())
            revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
            result = DurableRunResult(
                DurableRunStatus.REJECTED,
                error_code=AdmissionErrorCode.BUSY.value,
                run_id="run-1",
            )
            with (
                patch("agent.runtime.__main__.load_run_config", return_value=config),
                patch("agent.runtime.__main__.semantic_config_digest", return_value="a" * 64),
                patch("agent.runtime.__main__.detect_agent_code_revision", return_value=revision),
                patch("agent.runtime.__main__.start_run", return_value=result),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(
                        [
                            "start",
                            "--run-id",
                            "run-1",
                            "--thread-id",
                            "thread-1",
                            "--task-id",
                            "task-1",
                            "--task",
                            "inspect",
                        ]
                    )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["error_code"], AdmissionErrorCode.BUSY.value)

    def test_real_subprocesses_same_workspace_allow_only_one_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime"
            marker = workspace / "side-effects.txt"
            command = [
                sys.executable,
                "-c",
                _DURABLE_PROCESS,
                str(workspace),
                str(runtime),
                str(marker),
            ]
            first = subprocess.Popen(
                [*command, "thread-a", "20.0"],
                cwd=Path(__file__).resolve().parents[2],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                first_line = first.stdout.readline().strip()
                if first_line != "factory":
                    stdout, stderr = first.communicate(timeout=20)
                    self.fail(
                        f"first process did not enter factory: {stderr}\n{stdout}"
                    )
                time.sleep(0.1)
                second = subprocess.Popen(
                    [*command, "thread-b", "0.0"],
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                second_result = self._read_json_output(second)
                first_result = self._read_json_output(first)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=10)

            self.assertEqual(first_result, {"error": None, "factory": 1, "status": "completed"})
            self.assertEqual(second_result["status"], "rejected")
            self.assertEqual(second_result["error"], AdmissionErrorCode.BUSY.value)
            self.assertEqual(second_result["factory"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "thread-a\n")

    def test_real_subprocesses_same_workspace_different_runtime_are_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime_a = root / "runtime-a"
            runtime_b = root / "runtime-b"
            marker = workspace / "side-effects.txt"
            command = [
                sys.executable,
                "-c",
                _DURABLE_PROCESS,
                str(workspace),
                str(runtime_a),
                str(marker),
            ]
            first = subprocess.Popen(
                [*command, "thread-a", "20.0"],
                cwd=Path(__file__).resolve().parents[2],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(first.stdout.readline().strip(), "factory")
                second = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _DURABLE_PROCESS,
                        str(workspace),
                        str(runtime_b),
                        str(marker),
                        "thread-b",
                        "0.0",
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                second_result = self._read_json_output(second)
                first_result = self._read_json_output(first)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=10)

            self.assertEqual(first_result["status"], "completed")
            self.assertEqual(second_result["status"], "rejected")
            self.assertEqual(second_result["error"], AdmissionErrorCode.BUSY.value)
            self.assertEqual(second_result["factory"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "thread-a\n")

    def test_real_subprocesses_same_runtime_thread_block_different_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_workspace = root / "workspace-a"
            second_workspace = root / "workspace-b"
            first_workspace.mkdir()
            second_workspace.mkdir()
            runtime = root / "runtime"
            first_marker = first_workspace / "side-effects.txt"
            second_marker = second_workspace / "side-effects.txt"
            command = [sys.executable, "-c", _DURABLE_PROCESS]
            first = subprocess.Popen(
                [
                    *command,
                    str(first_workspace),
                    str(runtime),
                    str(first_marker),
                    "same-thread",
                    "20.0",
                ],
                cwd=Path(__file__).resolve().parents[2],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(first.stdout.readline().strip(), "factory")
                second = subprocess.Popen(
                    [
                        *command,
                        str(second_workspace),
                        str(runtime),
                        str(second_marker),
                        "same-thread",
                        "0.0",
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                second_result = self._read_json_output(second)
                first_result = self._read_json_output(first)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=10)

            self.assertEqual(first_result["status"], "completed")
            self.assertEqual(second_result["status"], "rejected")
            self.assertEqual(second_result["error"], AdmissionErrorCode.BUSY.value)
            self.assertEqual(second_result["factory"], 0)
            self.assertEqual(first_marker.read_text(encoding="utf-8"), "same-thread\n")
            self.assertFalse(second_marker.exists())

    @unittest.skipUnless(os.name == "nt", "Windows path case semantics")
    def test_windows_case_variant_has_same_workspace_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "Workspace"
            workspace.mkdir()
            first = AdmissionKey.from_identity(self._identity(workspace, "a"))
            alternate = WorkspaceIdentity.from_root(Path(str(workspace).lower()))
            second = AdmissionKey.from_identity(
                RunIdentity("run-b", "b", "task-b", alternate)
            )
            self.assertEqual(first.workspace_key, second.workspace_key)

    def test_rejects_runtime_root_symlink_without_lock_file_outside(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime = root / "runtime"
            external = root / "external"
            workspace.mkdir()
            runtime.mkdir()
            external.mkdir()
            link = root / "runtime-link"
            try:
                link.symlink_to(runtime, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            with self.assertRaises(AdmissionLockError) as context:
                WorkspaceAdmissionLock(link, self._identity(workspace))
            self.assertEqual(context.exception.code, AdmissionErrorCode.INVALID)
            self.assertEqual(list(external.iterdir()), [])

    def test_rejects_final_lock_file_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime"
            external = root / "external.lock"
            external.write_bytes(b"keep")
            lock = WorkspaceAdmissionLock(runtime, self._identity(workspace))
            lock.workspace_lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                lock.workspace_lock_path.symlink_to(external)
            except OSError as error:
                self.skipTest(f"file symlinks are unavailable: {error}")
            with self.assertRaises(AdmissionLockError) as context:
                lock.acquire()
            self.assertEqual(context.exception.code, AdmissionErrorCode.INVALID)
            self.assertEqual(external.read_bytes(), b"keep")
            self.assertFalse(lock.runtime_lock_path.exists())

    def test_rejects_runtime_lock_file_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime"
            external = root / "external.lock"
            external.write_bytes(b"keep")
            lock = WorkspaceAdmissionLock(runtime, self._identity(workspace))
            lock.runtime_lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                lock.runtime_lock_path.symlink_to(external)
            except OSError as error:
                self.skipTest(f"file symlinks are unavailable: {error}")
            with self.assertRaises(AdmissionLockError) as context:
                lock.acquire()
            self.assertEqual(context.exception.code, AdmissionErrorCode.INVALID)
            self.assertEqual(external.read_bytes(), b"keep")
            self.assertFalse(lock.workspace_lock_path.exists())

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows path type")
    def test_rejects_lock_directory_junction_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime = root / "runtime"
            external = root / "external"
            workspace.mkdir()
            runtime.mkdir()
            external.mkdir()
            lock_directory = runtime / "locks"
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(lock_directory), str(external)],
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                self.skipTest(
                    f"junction creation unavailable: {completed.stderr.strip()}"
                )
            lock = WorkspaceAdmissionLock(runtime, self._identity(workspace))
            with self.assertRaises(AdmissionLockError) as context:
                lock.acquire()
            self.assertEqual(context.exception.code, AdmissionErrorCode.INVALID)
            self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
