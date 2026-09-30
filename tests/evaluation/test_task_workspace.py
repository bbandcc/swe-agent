from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.evaluation.task_workspace import (
    TaskWorkspaceErrorCode,
    prepare_task_workspace,
)
from agent.workspace.policy import WorkspaceAccessPolicy


class TaskWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.repository = self.root / "baseline"
        self.repository.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "S5b test")
        (self.repository / "src").mkdir()
        (self.repository / "src" / "answer.txt").write_text(
            "baseline\n", encoding="utf-8"
        )
        (self.repository / "tests").mkdir()
        (self.repository / "tests" / "oracle.txt").write_text(
            "oracle-canary\n", encoding="utf-8"
        )
        (self.repository / "credentials").mkdir()
        (self.repository / "credentials" / "token.txt").write_text(
            "credential-canary\n", encoding="utf-8"
        )
        (self.repository / ".env").write_text("dotenv-canary\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "pinned baseline")
        self.revision = self._git("rev-parse", "HEAD").stdout.decode().strip()
        self.runtime_root = self.root / "runtime"
        self.task = {
            "task_id": "canary-task",
            "target": {"revision": self.revision},
            "allowed_edit_scope": [
                {"path": "src/answer.txt", "operation": "modify"},
                {"path": "src/new.txt", "operation": "create"},
            ],
            "oracle": {"test_files": [{"path": "tests/oracle.txt"}]},
        }

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repository,
            capture_output=True,
            check=True,
            shell=False,
            timeout=10,
        )

    def _prepare(
        self,
        *,
        access_policy: WorkspaceAccessPolicy,
        task_id: str = "canary-task",
    ):
        task = {**self.task, "task_id": task_id}
        return prepare_task_workspace(
            repository_root=self.repository,
            task=task,
            runtime_root=self.runtime_root,
            access_policy=access_policy,
        )

    def test_default_policy_is_explicit_and_omission_is_rejected(self) -> None:
        prepared = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy.default(),
        )
        self.assertTrue(prepared.ready, prepared.failure)
        self.addCleanup(prepared.workspace.cleanup)
        self.assertEqual(
            prepared.workspace.policy,
            WorkspaceAccessPolicy(
                hidden_paths=WorkspaceAccessPolicy.default().hidden_paths,
                oracle_paths=("tests/oracle.txt",),
            ),
        )

        missing_runtime = self.root / "missing-policy-runtime"
        with self.assertRaises(TypeError):
            prepare_task_workspace(
                repository_root=self.repository,
                task=self.task,
                runtime_root=missing_runtime,
            )
        self.assertFalse(missing_runtime.exists())

    def test_invalid_policy_is_structured_before_workspace_copy(self) -> None:
        for policy in (None, object()):
            runtime = self.root / f"invalid-policy-{type(policy).__name__}"
            with self.subTest(policy=policy):
                result = prepare_task_workspace(
                    repository_root=self.repository,
                    task=self.task,
                    runtime_root=runtime,
                    access_policy=policy,
                )
                self.assertFalse(result.ready)
                self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)
                self.assertFalse(runtime.exists())

    def test_pinned_task_copies_are_independent_and_exclude_oracle_and_git(self) -> None:
        first = self._prepare(access_policy=WorkspaceAccessPolicy.default())
        second = self._prepare(access_policy=WorkspaceAccessPolicy.default())
        self.assertTrue(first.ready, first.failure)
        self.assertTrue(second.ready, second.failure)
        self.addCleanup(first.workspace.cleanup)
        self.addCleanup(second.workspace.cleanup)

        one = first.workspace
        two = second.workspace
        self.assertEqual(one.revision, self.revision)
        self.assertEqual(two.revision, self.revision)
        self.assertFalse((one.root / ".git").exists())
        self.assertFalse((two.root / ".git").exists())
        self.assertFalse((one.root / "tests" / "oracle.txt").exists())
        self.assertFalse((two.root / "tests" / "oracle.txt").exists())
        self.assertFalse((one.root / "credentials").exists())
        self.assertFalse((one.root / ".env").exists())
        self.assertFalse(one.policy.check_read("tests/oracle.txt").allowed)
        self.assertFalse(one.policy.check_write("tests/oracle.txt").allowed)
        self.assertFalse(one.policy.check_read("credentials/token.txt").allowed)
        self.assertFalse(one.policy.check_write(".env").allowed)

        (one.root / "src" / "answer.txt").write_text("task one\n", encoding="utf-8")
        self.assertEqual((two.root / "src" / "answer.txt").read_text(), "baseline\n")
        self.assertEqual(
            (self.repository / "src" / "answer.txt").read_text(), "baseline\n"
        )
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.decode().strip(), self.revision)
        self.assertEqual(self._git("status", "--porcelain").stdout, b"")
        self.assertEqual(
            (self.repository / "credentials" / "token.txt").read_text(encoding="utf-8"),
            "credential-canary\n",
        )

    def test_trusted_hidden_and_oracle_paths_are_removed_before_execution(self) -> None:
        (self.repository / "private").mkdir()
        (self.repository / "private" / "secret.txt").write_text("hidden-canary", encoding="utf-8")
        (self.repository / "trusted-oracle.txt").write_text("trusted-canary", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "trusted protected paths")
        task = {**self.task, "target": {"revision": self._git("rev-parse", "HEAD").stdout.decode().strip()}}
        policy = WorkspaceAccessPolicy(hidden_paths=("private",), oracle_paths=("trusted-oracle.txt",))

        result = prepare_task_workspace(
            repository_root=self.repository, task=task, runtime_root=self.runtime_root,
            access_policy=policy,
        )

        self.assertTrue(result.ready, result.failure)
        self.addCleanup(result.workspace.cleanup)
        for path in ("private", "trusted-oracle.txt", "tests/oracle.txt", "credentials", ".env"):
            self.assertFalse((result.workspace.root / path).exists(), path)
            self.assertFalse(result.workspace.policy.check_read(path).allowed, path)
            self.assertFalse(result.workspace.policy.check_write(path).allowed, path)
        (result.workspace.root / "private").mkdir()
        (result.workspace.root / "private" / "secret.txt").write_text("replacement", encoding="utf-8")
        patch = result.workspace.collect_patch()
        self.assertFalse(patch.valid)
        self.assertEqual(patch.failure.code, TaskWorkspaceErrorCode.WRITE_DENIED)

    def test_public_sibling_of_oracle_remains_available_to_python_checks(self) -> None:
        (self.repository / "tests" / "test_public.py").write_text("import unittest\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "public check")
        task = {**self.task, "target": {"revision": self._git("rev-parse", "HEAD").stdout.decode().strip()}}
        result = prepare_task_workspace(
            repository_root=self.repository, task=task, runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy.default(),
        )
        self.assertTrue(result.ready, result.failure)
        self.addCleanup(result.workspace.cleanup)
        self.assertEqual((result.workspace.root / "tests" / "test_public.py").read_text(encoding="utf-8"), "import unittest\n")
        self.assertFalse((result.workspace.root / "tests" / "oracle.txt").exists())

    def test_edit_scope_conflicting_with_trusted_protection_rejects_before_copy(self) -> None:
        for policy in (
            WorkspaceAccessPolicy(hidden_paths=("src/answer.txt",)),
            WorkspaceAccessPolicy(oracle_paths=("src/answer.txt",)),
        ):
            with self.subTest(policy=policy):
                result = prepare_task_workspace(
                    repository_root=self.repository, task=self.task, runtime_root=self.runtime_root,
                    access_policy=policy,
                )
                self.assertFalse(result.ready)
                self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)
                self.assertFalse(self.runtime_root.exists())

    def test_patch_is_collected_from_task_tree_with_hash_and_size_evidence(self) -> None:
        prepared = self._prepare(access_policy=WorkspaceAccessPolicy.default())
        self.assertTrue(prepared.ready, prepared.failure)
        workspace = prepared.workspace
        self.addCleanup(workspace.cleanup)
        changed = b"updated\n"
        (workspace.root / "src" / "answer.txt").write_bytes(changed)
        (workspace.root / "src" / "new.txt").write_bytes(b"created\n")

        patch = workspace.collect_patch()

        self.assertTrue(patch.valid, patch.failure)
        evidence = {entry.path: entry for entry in patch.entries}
        self.assertEqual(set(evidence), {"src/answer.txt", "src/new.txt"})
        self.assertEqual(
            evidence["src/answer.txt"].after_sha256,
            hashlib.sha256(changed).hexdigest(),
        )
        self.assertGreater(evidence["src/answer.txt"].patch_size, 0)
        self.assertEqual(
            hashlib.sha256(evidence["src/answer.txt"].patch).hexdigest(),
            evidence["src/answer.txt"].patch_sha256,
        )

    def test_out_of_scope_change_is_rejected_without_patch_success(self) -> None:
        prepared = self._prepare(access_policy=WorkspaceAccessPolicy.default())
        self.assertTrue(prepared.ready, prepared.failure)
        workspace = prepared.workspace
        self.addCleanup(workspace.cleanup)
        (workspace.root / "src" / "outside.txt").write_text("bad", encoding="utf-8")

        patch = workspace.collect_patch()

        self.assertFalse(patch.valid)
        self.assertEqual(patch.failure.code, TaskWorkspaceErrorCode.WRITE_DENIED)
        self.assertEqual(patch.entries, ())

    def test_invalid_revision_and_scope_fail_closed(self) -> None:
        bad_revision = prepare_task_workspace(
            repository_root=self.repository,
            task={**self.task, "target": {"revision": "main"}},
            runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy.default(),
        )
        self.assertFalse(bad_revision.ready)
        self.assertEqual(bad_revision.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)

        bad_scope_task = {
            **self.task,
            "allowed_edit_scope": [{"path": "../host.txt", "operation": "modify"}],
        }
        bad_scope = prepare_task_workspace(
            repository_root=self.repository,
            task=bad_scope_task,
            runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy.default(),
        )
        self.assertFalse(bad_scope.ready)
        self.assertEqual(bad_scope.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)

    def test_runtime_root_inside_baseline_repository_is_rejected(self) -> None:
        runtime = self.repository / "runtime"
        result = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=runtime,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)
        self.assertFalse(runtime.exists())
        self.assertEqual(self._git("status", "--porcelain").stdout, b"")

    def test_runtime_root_equal_to_repository_is_rejected(self) -> None:
        result = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=self.repository,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)
        self.assertEqual(self._git("status", "--porcelain").stdout, b"")

    def test_repository_nested_under_runtime_root_is_rejected(self) -> None:
        result = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=self.root,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)
        self.assertFalse(any(self.root.glob("task-*")))
        self.assertEqual(self._git("status", "--porcelain").stdout, b"")

    def test_runtime_root_symlink_is_rejected_without_following_target(self) -> None:
        target = self.root / "runtime-target"
        target.mkdir()
        link = self.root / "runtime-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")

        result = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=link,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)
        self.assertEqual(list(target.iterdir()), [])

    def test_runtime_root_junction_is_rejected_without_following_target(self) -> None:
        if os.name != "nt":
            self.skipTest("junction roots are a Windows path type")
        target = self.root / "junction-target"
        target.mkdir()
        junction = self.root / "runtime-junction"
        completed = subprocess.run(
            [
                "pwsh.exe", "-NoLogo", "-NoProfile", "-CommandWithArgs",
                "New-Item -ItemType Junction -Path $args[0] -Target $args[1] | Out-Null",
                str(junction), str(target),
            ],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            self.skipTest(f"junction creation unavailable: {completed.stderr}")

        result = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=junction,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)
        self.assertEqual(list(target.iterdir()), [])

    def test_runtime_below_linked_ancestor_is_canonicalized_and_created(self) -> None:
        real_parent = self.root / "real-runtime-parent"
        real_parent.mkdir()
        linked_parent = self.root / "linked-runtime-parent"
        try:
            linked_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        runtime = linked_parent / "runtime"

        result = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=runtime,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertTrue(result.ready, result.failure)
        self.assertTrue(result.workspace.root.is_relative_to((real_parent / "runtime").resolve()))
        self.addCleanup(result.workspace.cleanup)

    def test_runtime_below_junction_ancestor_is_canonicalized_and_created(self) -> None:
        if os.name != "nt":
            self.skipTest("junctions are a Windows path type")
        real_parent = self.root / "real-junction-parent"
        real_parent.mkdir()
        junction_parent = self.root / "linked-junction-parent"
        completed = subprocess.run(
            [
                "pwsh.exe", "-NoLogo", "-NoProfile", "-CommandWithArgs",
                "New-Item -ItemType Junction -Path $args[0] -Target $args[1] | Out-Null",
                str(junction_parent), str(real_parent),
            ],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            self.skipTest(f"junction creation unavailable: {completed.stderr}")
        runtime = junction_parent / "runtime"

        result = prepare_task_workspace(
            repository_root=self.repository,
            task=self.task,
            runtime_root=runtime,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertTrue(result.ready, result.failure)
        self.assertTrue(result.workspace.root.is_relative_to((real_parent / "runtime").resolve()))
        self.addCleanup(result.workspace.cleanup)

    def test_oracle_scope_must_not_be_empty(self) -> None:
        result = prepare_task_workspace(
            repository_root=self.repository,
            task={**self.task, "oracle": {"test_files": []}},
            runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy.default(),
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.INVALID_TASK)

    def test_prepare_reports_cleanup_failure_instead_of_hiding_it(self) -> None:
        from agent.evaluation import task_workspace

        with (
            patch.object(task_workspace, "_checkout_snapshot", side_effect=OSError("injected")),
            patch.object(task_workspace.shutil, "rmtree", side_effect=OSError("cleanup")),
        ):
            result = self._prepare(access_policy=WorkspaceAccessPolicy.default())

        self.assertFalse(result.ready)
        self.assertEqual(result.failure.code, TaskWorkspaceErrorCode.CLEANUP_FAILED)
        leftovers = list(self.runtime_root.glob("task-*"))
        for leftover in leftovers:
            shutil.rmtree(leftover, ignore_errors=False)

    def test_execute_fails_without_patch_when_sandbox_cleanup_is_incomplete(self) -> None:
        from agent.evaluation import task_execution
        from agent.evaluation.task_execution import (
            TaskExecutionErrorCode,
            TaskExecutionStatus,
            execute_isolated_task,
        )
        from agent.evaluation.windows_sandbox import SandboxResult, SandboxStatus

        incomplete = SandboxResult(
            status=SandboxStatus.COMPLETED,
            exit_code=0,
            started=True,
            cleanup_complete=False,
        )
        with patch.object(task_execution, "run_in_windows_appcontainer", return_value=incomplete):
            result = execute_isolated_task(
                repository_root=self.repository,
                task=self.task,
                runtime_root=self.runtime_root,
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("cmd.exe", "/d", "/c", "exit 0"),
            )

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.CLEANUP_FAILED)
        self.assertFalse(result.cleanup_complete)
        self.assertIsNone(result.patch)

    def test_execute_fails_without_patch_when_task_workspace_cleanup_fails(self) -> None:
        from agent.evaluation import task_execution, task_workspace
        from agent.evaluation.task_execution import (
            TaskExecutionErrorCode,
            TaskExecutionStatus,
            execute_isolated_task,
        )
        from agent.evaluation.windows_sandbox import SandboxResult, SandboxStatus

        completed = SandboxResult(
            status=SandboxStatus.COMPLETED,
            exit_code=0,
            started=True,
            cleanup_complete=True,
        )
        with (
            patch.object(task_execution, "run_in_windows_appcontainer", return_value=completed),
            patch.object(task_workspace, "_remove_container", return_value=False),
        ):
            result = execute_isolated_task(
                repository_root=self.repository,
                task=self.task,
                runtime_root=self.runtime_root,
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("cmd.exe", "/d", "/c", "exit 0"),
            )

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.CLEANUP_FAILED)
        self.assertFalse(result.cleanup_complete)
        self.assertIsNone(result.patch)

    def test_execute_rejects_out_of_scope_patch_after_cleanup(self) -> None:
        from agent.evaluation import task_execution
        from agent.evaluation.task_execution import (
            TaskExecutionErrorCode,
            TaskExecutionStatus,
            execute_isolated_task,
        )
        from agent.evaluation.windows_sandbox import SandboxResult, SandboxStatus

        def write_out_of_scope(command):
            (command.workspace_root / "rogue.txt").write_bytes(b"outside declared scope")
            return SandboxResult(
                status=SandboxStatus.COMPLETED,
                exit_code=0,
                started=True,
                cleanup_complete=True,
            )

        with patch.object(
            task_execution, "run_in_windows_appcontainer", side_effect=write_out_of_scope
        ):
            result = execute_isolated_task(
                repository_root=self.repository,
                task=self.task,
                runtime_root=self.runtime_root,
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("cmd.exe", "/d", "/c", "exit 0"),
            )

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.PATCH_COLLECTION_FAILED)
        self.assertTrue(result.cleanup_complete)
        self.assertIsNotNone(result.patch)
        self.assertFalse(result.patch.valid)
        self.assertEqual(result.patch.entries, ())

    def test_workspace_cleanup_failure_is_structured_and_retryable(self) -> None:
        from agent.evaluation import task_workspace

        prepared = self._prepare(access_policy=WorkspaceAccessPolicy.default())
        self.assertTrue(prepared.ready, prepared.failure)
        workspace = prepared.workspace

        with patch.object(task_workspace.shutil, "rmtree", side_effect=OSError("cleanup")):
            failure = workspace.cleanup()

        self.assertEqual(failure.code, TaskWorkspaceErrorCode.CLEANUP_FAILED)
        self.assertTrue(workspace.root.exists())
        self.assertIsNone(workspace.cleanup())
        self.assertFalse(workspace.root.exists())

    def test_cleanup_is_explicit_and_idempotent(self) -> None:
        prepared = self._prepare(access_policy=WorkspaceAccessPolicy.default())
        self.assertTrue(prepared.ready, prepared.failure)
        workspace = prepared.workspace
        root = workspace.root

        first = workspace.cleanup()
        second = workspace.cleanup()

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
