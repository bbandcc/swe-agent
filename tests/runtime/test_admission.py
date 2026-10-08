import io
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import threading
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
from agent.verification.process_tree import ProcessTree
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
    release_path = Path(sys.argv[6]) if len(sys.argv) > 6 else None
    release_timeout = float(sys.argv[7]) if release_path is not None else None
    config = RunConfig(
        workspace_root=workspace,
        runtime_root=runtime,
        model=ModelSettings("deepseek", "deepseek-v4-flash", "https://api.deepseek.com", None),
        model_max_output_tokens=64,
        verification_specs=(),
        timeout_seconds=180.0 if release_path is not None else 30.0,
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
                if release_path is None:
                    time.sleep(hold)
                else:
                    print("holding", flush=True)
                    expires = time.monotonic() + release_timeout
                    while not release_path.exists():
                        if time.monotonic() >= expires:
                            raise RuntimeError("release handshake timed out")
                        time.sleep(0.01)
                    if release_path.read_text(encoding="utf-8") != "release\\n":
                        raise RuntimeError("invalid release handshake signal")
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
            try:
                self.assertEqual(second.acquire(), AdmissionStatus.BUSY)
                self.assertNotIn("thread-a", first.lock_path.name)
                self.assertEqual(first.lock_path.parent, runtime.resolve() / "locks")
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
            finally:
                first.release()

            self.assertEqual(second.acquire(), AdmissionStatus.ACQUIRED)
            try:
                self.assertTrue(second.lock_path.exists())
            finally:
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

    @staticmethod
    def _handshake_line(
        process: subprocess.Popen[str], timeout: float = 60.0,
        *, readers: list[threading.Thread],
    ) -> str:
        lines: queue.Queue[str] = queue.Queue(maxsize=1)
        reader = threading.Thread(
            target=lambda: lines.put(process.stdout.readline().strip()), daemon=True
        )
        readers.append(reader)
        reader.start()
        try:
            line = lines.get(timeout=timeout)
        except queue.Empty as error:
            raise AssertionError("holder readiness handshake timed out") from error
        reader.join(timeout=1.0)
        if reader.is_alive():
            raise AssertionError("holder readiness reader did not finish")
        return line

    def _assert_durable_contention(
        self,
        *,
        holder_source: str = _DURABLE_PROCESS,
        contender_source: str = _DURABLE_PROCESS,
        release_timeout: float = 120.0,
        readiness_timeout: float = 60.0,
        contender_startup_gate: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime"
            marker = workspace / "side-effects.txt"
            release = root / "release.txt"
            processes = []
            readers = []
            events = []

            def event(name, **fields):
                events.append({"event": name, "monotonic_ns": time.perf_counter_ns(), **fields})

            def spawn(source, thread, *extra):
                event("spawn", thread=thread)
                process = subprocess.Popen(
                    [sys.executable, "-c", source, str(workspace), str(runtime),
                     str(marker), thread, "0.0", *extra],
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=os.name == "posix",
                )
                # Register Popen before any fallible tree initialization. Retain
                # the partially initialized object so an acquired Job can close.
                owner = [process, None, False]
                processes.append(owner)
                tree = ProcessTree.__new__(ProcessTree)
                owner[1] = tree
                ProcessTree.__init__(tree, process)
                owner[2] = True
                return process

            def signal_release():
                if not release.exists():
                    pending_signal = root / "release.tmp"
                    pending_signal.write_text("release\n", encoding="utf-8")
                    pending_signal.replace(release)

            try:
                first = spawn(holder_source, "thread-a", str(release), str(release_timeout))
                self.assertEqual(self._handshake_line(first, readers=readers), "factory")
                self.assertEqual(self._handshake_line(first, readiness_timeout, readers=readers), "holding")
                event("holder_ready", pid=first.pid)
                if first.poll() is not None:
                    self._read_json_output(first)
                    self.fail("holder exited before contender startup")
                if contender_startup_gate:
                    started = root / "contender-started"
                    startup_release = root / "contender-go"
                    prefix = textwrap.dedent(
                        f"""
                        import time
                        from pathlib import Path
                        Path({str(started)!r}).touch()
                        startup_release = Path({str(startup_release)!r})
                        startup_deadline = time.monotonic() + 60.0
                        while not startup_release.exists():
                            if time.monotonic() >= startup_deadline:
                                raise RuntimeError("contender startup handshake timed out")
                            time.sleep(0.01)
                        """
                    )
                    contender_source = prefix + contender_source
                second = spawn(contender_source, "thread-b")
                if contender_startup_gate:
                    startup_deadline = time.monotonic() + 60.0
                    while not started.exists():
                        if second.poll() is not None:
                            self._read_json_output(second)
                            self.fail("contender exited before startup handshake")
                        if time.monotonic() >= startup_deadline:
                            self.fail("contender startup handshake timed out")
                        time.sleep(0.01)
                    probe = WorkspaceAdmissionLock(runtime, self._identity(workspace, "overlap"))
                    try:
                        self.assertEqual(probe.acquire(), AdmissionStatus.BUSY)
                    finally:
                        probe.release()
                    self.assertIsNone(first.poll(), "holder exited while contender startup was gated")
                    self.assertEqual(marker.read_text(encoding="utf-8"), "thread-a\n")
                    event("contender_startup_gated_holder_busy")
                    startup_release.touch()
                second_result = self._read_json_output(second)
                event("contender_result", result=second_result)
                if first.poll() is not None:
                    self._read_json_output(first)
                    self.fail("holder exited before explicit release")
                signal_release()
                event("release_signal")
                first_result = self._read_json_output(first)
                marker_content = marker.read_text(encoding="utf-8")
                event("holder_result", result=first_result, marker=marker_content)
                self.assertEqual(
                    first_result, {"error": None, "factory": 1, "status": "completed"}
                )
                self.assertEqual(
                    second_result,
                    {"error": AdmissionErrorCode.BUSY.value, "factory": 0, "status": "rejected"},
                    f"unexpected contender result: {second_result}; marker={marker_content!r}",
                )
                self.assertEqual(marker_content, "thread-a\n")
            finally:
                # Unblock the holder on every error path before bounded tree cleanup.
                original_failure = sys.exception()
                errors = []
                try:
                    signal_release()
                except OSError as error:
                    errors.append("release signal failed: " + type(error).__name__)
                for process, tree, initialized in reversed(processes):
                    try:
                        if process.poll() is None:
                            if initialized:
                                tree.terminate()
                            else:
                                # Constructor failure has no usable tree seam.
                                # Popen uses a POSIX session; Windows taskkill
                                # targets descendants even before Job attachment.
                                if os.name == "posix":
                                    try:
                                        os.killpg(process.pid, signal.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                else:
                                    subprocess.run(
                                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, shell=False, timeout=5.0,
                                        check=False,
                                    )
                                try:
                                    process.wait(timeout=1.0)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=1.0)
                        process.communicate(timeout=10.0)
                        if process.poll() is None:
                            errors.append("child still running after cleanup")
                    except (OSError, subprocess.TimeoutExpired) as error:
                        errors.append(type(error).__name__)
                    finally:
                        try:
                            if tree is not None:
                                tree.close()
                        except AttributeError as error:
                            # __init__ failed before its optional Job field was
                            # assigned; there is no handle for close() to own.
                            if initialized or "_windows_job" not in str(error):
                                raise
                        except OSError as error:
                            errors.append("tree close failed: " + type(error).__name__)
                        for stream in (process.stdout, process.stderr):
                            try:
                                stream.close()
                            except OSError as error:
                                errors.append("pipe close failed: " + type(error).__name__)
                for reader in readers:
                    reader.join(timeout=1.0)
                    if reader.is_alive():
                        errors.append("readiness reader still running after child cleanup")
                probe = WorkspaceAdmissionLock(runtime, self._identity(workspace, "cleanup"))
                try:
                    if probe.acquire() is not AdmissionStatus.ACQUIRED:
                        errors.append("workspace lock remained held after child cleanup")
                finally:
                    probe.release()
                print("ADMISSION_HANDSHAKE_EVIDENCE " + json.dumps(events), flush=True)
                if errors:
                    raise AssertionError("child cleanup failed: " + ", ".join(errors)) from original_failure

    def test_real_subprocesses_same_workspace_allow_only_one_graph(self) -> None:
        self._assert_durable_contention()

    def test_holder_keeps_lock_while_contender_startup_is_gated(self) -> None:
        self._assert_durable_contention(contender_startup_gate=True)

    def test_handshake_detects_wrong_admission_with_extra_factory_and_write(self) -> None:
        # Partition the contender's real OS-lock namespace to simulate broken
        # workspace admission; do not mock acquire() or its result.
        fault = textwrap.dedent(
            """
            import tempfile
            fault_root = runtime / "fault-namespace"
            fault_root.mkdir()
            tempfile.tempdir = str(fault_root)
            """
        )
        source = _DURABLE_PROCESS.replace("factory = Factory()", fault + "\nfactory = Factory()")
        with self.assertRaisesRegex(AssertionError, "unexpected contender result") as raised:
            self._assert_durable_contention(contender_source=source)
        self.assertIn("'factory': 1", str(raised.exception))
        self.assertIn("thread-b", str(raised.exception))

    def test_handshake_timeout_is_failure_not_successful_busy(self) -> None:
        with self.assertRaisesRegex(AssertionError, "release handshake timed out"):
            self._assert_durable_contention(release_timeout=0.0)

    def test_holder_exception_before_release_is_failure(self) -> None:
        source = _DURABLE_PROCESS.replace(
            "expires = time.monotonic() + release_timeout",
            'raise RuntimeError("holder aborted before release")',
        )
        with self.assertRaisesRegex(AssertionError, "holder aborted before release"):
            self._assert_durable_contention(holder_source=source)


    def test_missing_holder_ready_signal_is_a_bounded_failure(self) -> None:
        source = _DURABLE_PROCESS.replace('print("holding", flush=True)', "pass")
        with self.assertRaisesRegex(AssertionError, "readiness handshake timed out"):
            self._assert_durable_contention(holder_source=source, readiness_timeout=0.05)

    def test_handshake_cleanup_failure_cannot_report_success(self) -> None:
        original_close = ProcessTree.close

        def close_then_fail(tree):
            original_close(tree)
            raise OSError("injected cleanup failure after actual close")

        with patch.object(ProcessTree, "close", close_then_fail):
            with self.assertRaisesRegex(AssertionError, "child cleanup failed"):
                self._assert_durable_contention()

    def _assert_tree_initialization_failure_is_cleaned(self, failing_spawn, *, before_init=False):
        original_init = ProcessTree.__init__
        captured = []
        captured_trees = []

        def initialize_then_fail(tree, process):
            captured.append(process)
            captured_trees.append(tree)
            if before_init and len(captured) == failing_spawn:
                raise RuntimeError("injected ProcessTree initialization failure")
            original_init(tree, process)
            if len(captured) == failing_spawn:
                raise RuntimeError("injected ProcessTree initialization failure")

        try:
            with patch.object(ProcessTree, "__init__", initialize_then_fail):
                with self.assertRaisesRegex(RuntimeError, "injected ProcessTree initialization failure"):
                    self._assert_durable_contention()
            self.assertEqual(len(captured), failing_spawn)
            for process in captured:
                self.assertIsNotNone(process.poll(), "initialized child remained running")
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
                self.assertFalse(Path(process.args[3]).parent.exists(), "coordination directory remained")
        finally:
            # The red test must also clean the leaked child from the old helper.
            for process in captured:
                if process.poll() is None:
                    tree = ProcessTree(process)
                    try:
                        tree.terminate()
                        process.communicate(timeout=10.0)
                    finally:
                        tree.close()
                for stream in (process.stdout, process.stderr):
                    stream.close()
            for tree in captured_trees:
                try:
                    tree.close()
                except AttributeError:
                    if not before_init:
                        raise

    def test_first_tree_initialization_failure_is_cleaned(self):
        self._assert_tree_initialization_failure_is_cleaned(1)

    def test_second_tree_initialization_failure_is_cleaned(self):
        self._assert_tree_initialization_failure_is_cleaned(2)

    def test_tree_failure_before_job_initialization_is_cleaned(self):
        self._assert_tree_initialization_failure_is_cleaned(1, before_init=True)

    def test_tree_initialization_and_cleanup_failures_preserve_both_errors(self):
        original_init = ProcessTree.__init__
        original_close = ProcessTree.close

        def initialize_then_fail(tree, process):
            original_init(tree, process)
            raise RuntimeError("injected ProcessTree initialization failure")

        def close_then_fail(tree):
            original_close(tree)
            raise OSError("injected cleanup failure after actual close")

        with patch.object(ProcessTree, "__init__", initialize_then_fail):
            with patch.object(ProcessTree, "close", close_then_fail):
                with self.assertRaisesRegex(AssertionError, "child cleanup failed") as raised:
                    self._assert_durable_contention()
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertIn("injected ProcessTree initialization failure", str(raised.exception.__cause__))
        self.assertIn("tree close failed", str(raised.exception))

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

    def test_rejects_workspace_lock_hard_link_without_touching_external(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime"
            external = root / "external-workspace.lock"
            external.write_bytes(b"")
            before = external.stat()
            lock = WorkspaceAdmissionLock(runtime, self._identity(workspace))
            lock.workspace_lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                lock.workspace_lock_path.hardlink_to(external)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"hard links are unavailable: {error}")
            with self.assertRaises(AdmissionLockError) as context:
                lock.acquire()
            self.assertEqual(context.exception.code, AdmissionErrorCode.INVALID)
            after = external.stat()
            self.assertEqual(external.read_bytes(), b"")
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertFalse(lock.runtime_lock_path.exists())

    def test_rejects_runtime_lock_hard_link_and_releases_workspace_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_workspace = root / "workspace-a"
            second_workspace = root / "workspace-b"
            first_workspace.mkdir()
            second_workspace.mkdir()
            runtime = root / "runtime"
            alternate_runtime = root / "alternate-runtime"
            external = root / "external-runtime.lock"
            external.write_bytes(b"")
            before = external.stat()
            first = WorkspaceAdmissionLock(
                runtime, self._identity(first_workspace, "first-thread")
            )
            second = WorkspaceAdmissionLock(
                runtime, self._identity(second_workspace, "same-thread")
            )
            second.runtime_lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                second.runtime_lock_path.hardlink_to(external)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"hard links are unavailable: {error}")
            self.assertEqual(first.acquire(), AdmissionStatus.ACQUIRED)
            with self.assertRaises(AdmissionLockError) as context:
                second.acquire()
            self.assertEqual(context.exception.code, AdmissionErrorCode.INVALID)
            self.assertFalse(second.acquired)
            after = external.stat()
            self.assertEqual(external.read_bytes(), b"")
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            first.release()

            released = WorkspaceAdmissionLock(
                alternate_runtime, self._identity(second_workspace, "same-thread")
            )
            self.assertEqual(released.acquire(), AdmissionStatus.ACQUIRED)
            released.release()

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
