import os
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent.runtime.revision as revision_module
from agent.runtime.revision import (
    WorkspaceRevisionReason,
    WorkspaceRevisionStatus,
    detect_workspace_revision,
)
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    DurableBudgetState,
    RunIdentity,
    StartRequest,
    WorkspaceIdentity,
    semantic_config_digest,
    start_run,
)
from agent.runtime.durable import DurableRunStatus
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from tests.runtime._config_support import RunConfigTestCase


class _FakePipe:
    def __init__(self, *, continuous: bool) -> None:
        self.continuous = continuous
        self.bytes_read = 0
        self.closed = False

    def read(self, size: int) -> bytes:
        if not self.continuous:
            return b""
        self.bytes_read += size
        return b"x" * size

    def close(self) -> None:
        self.closed = True


class _FakeGitProcess:
    def __init__(self, *, stdout_continuous: bool, stderr_continuous: bool) -> None:
        self.pid = 999999
        self.stdout = _FakePipe(continuous=stdout_continuous)
        self.stderr = _FakePipe(continuous=stderr_continuous)
        self.returncode: int | None = None
        self.killed = False
        self.reaped = False
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.returncode is None:
            time.sleep(min(timeout or 0.001, 0.001))
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake-git", timeout)
        self.reaped = True
        return self.returncode


class _FakeProcessTree:
    def __init__(self, process: _FakeGitProcess) -> None:
        self.process = process
        self.closed = False

    def terminate(self) -> None:
        self.process.kill()

    def close(self) -> None:
        self.closed = True


class _RevisionState(DurableBudgetState):
    run_identity: RunIdentity | None = None
    run_config_digest: str | None = None
    agent_revision: AgentCodeRevision | None = None
    value: int = 0


class _CompletedFactory:
    def __call__(self, config, run_id, saver, clock):
        graph = StateGraph(_RevisionState)
        graph.add_node("finish", lambda state: {"value": state.value + 1})
        graph.add_edge(START, "finish")
        graph.add_edge("finish", END)
        return graph.compile(checkpointer=saver)


class _MutatingFactory:
    def __init__(self, tracked_file: Path) -> None:
        self.tracked_file = tracked_file

    def __call__(self, config, run_id, saver, clock):
        graph = StateGraph(_RevisionState)

        def mutate(state):
            self.tracked_file.write_text("two", encoding="utf-8")
            return {"value": state.value + 1}

        graph.add_node("mutate", mutate)
        graph.add_edge(START, "mutate")
        graph.add_edge("mutate", END)
        return graph.compile(checkpointer=saver)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )


class WorkspaceRevisionTests(RunConfigTestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")

    def test_clean_and_dirty_git_revision_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_git(root, "init").returncode, 0)
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            (root / "tracked.txt").write_text("one", encoding="utf-8")
            self.assertEqual(_git(root, "add", "tracked.txt").returncode, 0)
            self.assertEqual(_git(root, "commit", "-m", "initial").returncode, 0)

            clean = detect_workspace_revision(root)
            self.assertEqual(clean.status, WorkspaceRevisionStatus.KNOWN)
            self.assertIsNotNone(clean.head)
            self.assertFalse(clean.dirty)
            self.assertEqual(len(clean.status_digest), 64)
            self.assertEqual(len(clean.diff_digest), 64)
            clean_repeat = detect_workspace_revision(root)
            self.assertEqual(clean_repeat.status_digest, clean.status_digest)
            self.assertEqual(clean_repeat.diff_digest, clean.diff_digest)

            (root / "tracked.txt").write_text("two", encoding="utf-8")
            dirty = detect_workspace_revision(root)
            self.assertEqual(dirty.status, WorkspaceRevisionStatus.KNOWN)
            self.assertEqual(dirty.head, clean.head)
            self.assertTrue(dirty.dirty)
            self.assertNotEqual(dirty.status_digest, clean.status_digest)
            self.assertNotEqual(dirty.diff_digest, clean.diff_digest)
            dirty_repeat = detect_workspace_revision(root)
            self.assertEqual(dirty_repeat.status_digest, dirty.status_digest)
            self.assertEqual(dirty_repeat.diff_digest, dirty.diff_digest)

    def test_non_git_workspace_is_unknown_without_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = detect_workspace_revision(directory)
            self.assertEqual(revision.status, WorkspaceRevisionStatus.UNKNOWN)
            self.assertEqual(revision.reason, WorkspaceRevisionReason.NOT_GIT)
            self.assertIsNone(revision.head)
            self.assertIsNone(revision.status_digest)
            self.assertIsNone(revision.diff_digest)

    def test_git_failure_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = detect_workspace_revision(
                directory,
                git_executable=os.fspath(Path(directory) / "missing-git"),
            )
            self.assertEqual(revision.status, WorkspaceRevisionStatus.UNKNOWN)
            self.assertEqual(revision.reason, WorkspaceRevisionReason.GIT_UNAVAILABLE)

    def test_oversized_status_is_unknown_without_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_git(root, "init").returncode, 0)
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            (root / "tracked.txt").write_text("baseline", encoding="utf-8")
            self.assertEqual(_git(root, "add", "tracked.txt").returncode, 0)
            self.assertEqual(_git(root, "commit", "-m", "initial").returncode, 0)
            for index in range(12):
                name = f"untracked-{index}-" + ("x" * 96)
                (root / name).write_text("ignored from revision output", encoding="utf-8")

            with patch(
                "agent.runtime.revision._WORKSPACE_REVISION_STDOUT_MAX_BYTES",
                256,
                create=True,
            ):
                revision = detect_workspace_revision(root)

            self.assertEqual(revision.status, WorkspaceRevisionStatus.UNKNOWN)
            self.assertEqual(revision.reason, WorkspaceRevisionReason.QUERY_FAILED)
            self.assertIsNone(revision.head)
            self.assertIsNone(revision.dirty)
            self.assertIsNone(revision.status_digest)
            self.assertIsNone(revision.diff_digest)

    def test_oversized_binary_diff_is_unknown_without_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_git(root, "init").returncode, 0)
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            binary = root / "payload.bin"
            binary.write_bytes(os.urandom(4 * 1024 * 1024))
            self.assertEqual(_git(root, "add", "payload.bin").returncode, 0)
            self.assertEqual(_git(root, "commit", "-m", "initial").returncode, 0)
            binary.write_bytes(os.urandom(4 * 1024 * 1024))

            with patch(
                "agent.runtime.revision._WORKSPACE_REVISION_STDOUT_MAX_BYTES",
                64 * 1024,
                create=True,
            ):
                revision = detect_workspace_revision(root)

            self.assertEqual(revision.status, WorkspaceRevisionStatus.UNKNOWN)
            self.assertEqual(revision.reason, WorkspaceRevisionReason.QUERY_FAILED)
            self.assertIsNone(revision.head)
            self.assertIsNone(revision.dirty)
            self.assertIsNone(revision.status_digest)
            self.assertIsNone(revision.diff_digest)

    def test_pipe_overflow_consumes_only_cap_plus_probe_and_reaps_git(self) -> None:
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                limit = 64
                process = _FakeGitProcess(
                    stdout_continuous=stream == "stdout",
                    stderr_continuous=stream == "stderr",
                )
                with (
                    patch.object(
                        revision_module, "_WORKSPACE_REVISION_STDOUT_MAX_BYTES", limit
                    ),
                    patch.object(
                        revision_module, "_WORKSPACE_REVISION_STDERR_MAX_BYTES", limit
                    ),
                    patch.object(
                        revision_module.subprocess,
                        "Popen",
                        return_value=process,
                    ),
                    patch(
                        "agent.verification.process_tree.ProcessTree",
                        _FakeProcessTree,
                    ),
                ):
                    revision = detect_workspace_revision(
                        ".", git_executable="fake-git"
                    )

                consumed = (
                    process.stdout.bytes_read
                    if stream == "stdout"
                    else process.stderr.bytes_read
                )
                self.assertEqual(consumed, limit + 1)
                self.assertLessEqual(process.stdout.bytes_read, limit + 1)
                self.assertLessEqual(process.stderr.bytes_read, limit + 1)
                self.assertTrue(process.killed)
                self.assertTrue(process.reaped)
                self.assertEqual(revision.status, WorkspaceRevisionStatus.UNKNOWN)
                self.assertEqual(revision.reason, WorkspaceRevisionReason.QUERY_FAILED)
                self.assertIsNone(revision.head)
                self.assertIsNone(revision.dirty)
                self.assertIsNone(revision.status_digest)
                self.assertIsNone(revision.diff_digest)

    def test_git_timeout_terminates_and_reaps_without_partial_evidence(self) -> None:
        process = _FakeGitProcess(
            stdout_continuous=False,
            stderr_continuous=False,
        )
        with (
            patch.object(
                revision_module.subprocess,
                "Popen",
                return_value=process,
            ),
            patch(
                "agent.verification.process_tree.ProcessTree",
                _FakeProcessTree,
            ),
        ):
            revision = detect_workspace_revision(
                ".", git_executable="fake-git", timeout_seconds=0.01
            )

        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)
        self.assertTrue(process.wait_calls)
        self.assertTrue(all(value is not None for value in process.wait_calls))
        self.assertEqual(revision.status, WorkspaceRevisionStatus.UNKNOWN)
        self.assertEqual(revision.reason, WorkspaceRevisionReason.QUERY_FAILED)
        self.assertIsNone(revision.head)
        self.assertIsNone(revision.dirty)
        self.assertIsNone(revision.status_digest)
        self.assertIsNone(revision.diff_digest)

    def test_durable_record_contains_start_and_end_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            self.assertEqual(_git(workspace, "init").returncode, 0)
            _git(workspace, "config", "user.email", "test@example.invalid")
            _git(workspace, "config", "user.name", "Test")
            (workspace / "tracked.txt").write_text("one", encoding="utf-8")
            self.assertEqual(_git(workspace, "add", "tracked.txt").returncode, 0)
            self.assertEqual(
                _git(workspace, "commit", "-m", "initial").returncode, 0
            )
            config = self.make_config(root, workspace_root=workspace, verification_specs=())
            identity = RunIdentity(
                run_id="revision-run",
                thread_id="revision-thread",
                task_id="revision-task",
                workspace=WorkspaceIdentity.from_root(workspace),
            )
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN),
            )
            result = start_run(
                config,
                request,
                {"value": 0},
                graph_factory=_CompletedFactory(),
                clock=lambda: 100.0,
            )
            self.assertEqual(result.status, DurableRunStatus.COMPLETED)
            self.assertIsNotNone(result.summary.record_ref)
            record_path = config.runtime_root / result.summary.record_ref
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["workspace_revision_start"]["status"], "known"
            )
            self.assertEqual(payload["workspace_revision_end"]["status"], "known")
            self.assertEqual(
                payload["workspace_revision_start"]["head"],
                payload["workspace_revision_end"]["head"],
            )

    def test_durable_record_captures_clean_to_dirty_revision_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            self.assertEqual(_git(workspace, "init").returncode, 0)
            _git(workspace, "config", "user.email", "test@example.invalid")
            _git(workspace, "config", "user.name", "Test")
            tracked = workspace / "tracked.txt"
            tracked.write_text("one", encoding="utf-8")
            self.assertEqual(_git(workspace, "add", "tracked.txt").returncode, 0)
            self.assertEqual(
                _git(workspace, "commit", "-m", "initial").returncode, 0
            )
            config = self.make_config(
                root, workspace_root=workspace, verification_specs=()
            )
            identity = RunIdentity(
                run_id="revision-lifecycle-run",
                thread_id="revision-lifecycle-thread",
                task_id="revision-lifecycle-task",
                workspace=WorkspaceIdentity.from_root(workspace),
            )
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN),
            )
            result = start_run(
                config,
                request,
                {"value": 0},
                graph_factory=_MutatingFactory(tracked),
                clock=lambda: 100.0,
            )
            self.assertEqual(result.status, DurableRunStatus.COMPLETED)
            self.assertIsNotNone(result.summary.record_ref)
            record_path = config.runtime_root / result.summary.record_ref
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            start = payload["workspace_revision_start"]
            end = payload["workspace_revision_end"]
            self.assertEqual(payload["schema_version"], 2)
            expected_revision_fields = {
                "status",
                "head",
                "dirty",
                "status_digest",
                "diff_digest",
                "reason",
            }
            self.assertEqual(set(start), expected_revision_fields)
            self.assertEqual(set(end), expected_revision_fields)
            self.assertEqual(start["status"], "known")
            self.assertFalse(start["dirty"])
            self.assertEqual(end["status"], "known")
            self.assertTrue(end["dirty"])
            self.assertNotEqual(start["status_digest"], end["status_digest"])
            self.assertNotEqual(start["diff_digest"], end["diff_digest"])
            record_bytes = record_path.read_bytes()
            self.assertNotIn(b"tracked.txt", record_bytes)
            self.assertNotIn(b"two", record_bytes)


if __name__ == "__main__":
    unittest.main()
