import os
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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

            (root / "tracked.txt").write_text("two", encoding="utf-8")
            dirty = detect_workspace_revision(root)
            self.assertEqual(dirty.status, WorkspaceRevisionStatus.KNOWN)
            self.assertEqual(dirty.head, clean.head)
            self.assertTrue(dirty.dirty)
            self.assertNotEqual(dirty.status_digest, clean.status_digest)
            self.assertNotEqual(dirty.diff_digest, clean.diff_digest)

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
            self.assertEqual(start["status"], "known")
            self.assertFalse(start["dirty"])
            self.assertEqual(end["status"], "known")
            self.assertTrue(end["dirty"])
            self.assertNotEqual(start["status_digest"], end["status_digest"])
            self.assertNotEqual(start["diff_digest"], end["diff_digest"])


if __name__ == "__main__":
    unittest.main()
