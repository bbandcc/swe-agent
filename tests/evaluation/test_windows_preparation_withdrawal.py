"""R0b direct-call tests: owned data only, with preparation/process sentinels."""

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent.evaluation import task_execution as tasks
from agent.evaluation import frozen_environment as frozen
from agent.evaluation import isolated_source_build as fuzzy
from agent.evaluation import isolated_forbiddenfruit_build as fruit
from agent.evaluation import task_workspace as workspaces
from agent.workspace.policy import WorkspaceAccessPolicy


class PreparationAttempt(RuntimeError):
    """A test sentinel, never a real preparation failure."""


@contextmanager
def forbid_preparation():
    events = []
    public_entries = (
        (tasks, "execute_isolated_task", tasks.execute_isolated_task),
        (frozen, "stage_frozen_python_312", frozen.stage_frozen_python_312),
        (fuzzy, "build_pinned_fuzzysearch_wheel", fuzzy.build_pinned_fuzzysearch_wheel),
        (fruit, "build_pinned_forbiddenfruit_wheel", fruit.build_pinned_forbiddenfruit_wheel),
    )

    def block(name):
        def reject(*_args, **_kwargs):
            events.append(name)
            raise PreparationAttempt(name)
        return reject

    names = (
        "prepare_task_workspace", "stage_frozen_python_312", "stage_python_312",
        "validate_python_312", "run_in_windows_appcontainer", "_validate_uv_0121",
        "_run_uv_sync", "_hash_file", "_trusted_external_file", "_normal_user_token",
        "_identity", "_pinned_bytes", "_safe_bytes", "_python_identity",
        "_stage_sdist", "_stage_bootstrap", "_output_wheel", "_publish",
    )
    with ExitStack() as stack:
        for module in (tasks, frozen, fuzzy, fruit):
            for name in names:
                if module is frozen and name == "stage_frozen_python_312":
                    continue  # The public function under test must remain real.
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, block(module.__name__ + "." + name)))
        stack.enter_context(patch.object(tempfile, "mkdtemp", block("mkdtemp")))
        stack.enter_context(patch.object(Path, "mkdir", block("mkdir")))
        stack.enter_context(patch.object(Path, "open", block("file.open")))
        stack.enter_context(patch.object(frozen.shutil, "rmtree", block("rmtree")))
        stack.enter_context(patch.object(os, "system", block("host.system")))
        for name in ("Popen", "run", "call", "check_call", "check_output"):
            stack.enter_context(patch.object(subprocess, name, block("host." + name)))
        for module, name, original in public_entries:
            assert getattr(module, name) is original, "Guard replaced a public entry"
        yield events


class PreparationWithdrawalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="r0b-fixture-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.lock = b"# owned test data, never installed\n"
        (self.workspace / "uv.lock").write_bytes(self.lock)
        self.task = {
            "task_id": "r0b-fixture", "target": {"revision": "a" * 40},
            "allowed_edit_scope": [{"path": "app.py", "operation": "modify"}],
            "oracle": {"test_files": [{"path": "tests/test_app.py"}]},
        }

    def invoke(self, callback, **arguments):
        entered = []
        code = callback.__code__
        previous = sys.getprofile()

        def trace(frame, event, _arg):
            if event == "call" and frame.f_code is code:
                entered.append(callback.__module__ + "." + callback.__name__)

        with forbid_preparation() as events:
            sys.setprofile(trace)
            try:
                result = callback(**arguments)
            except (PreparationAttempt, frozen.FrozenEnvironmentError) as error:
                result = error
            finally:
                sys.setprofile(previous)
        self.assertEqual(entered, [callback.__module__ + "." + callback.__name__])
        return events, result

    def task_arguments(self, **kwargs):
        arguments = dict(
            repository_root=self.repository, task=kwargs.pop("task", self.task),
            runtime_root=self.runtime, access_policy=kwargs.pop("access_policy", WorkspaceAccessPolicy.default()),
            argv=("python", "-c", "must never run"),
        )
        arguments.update(kwargs)
        return arguments

    def test_task_refuses_before_prepare(self):
        arguments = self.task_arguments()
        events, result = self.invoke(tasks.execute_isolated_task, **arguments)
        self.assertEqual(events, [], "task preparation or execution was attempted")
        self.assertEqual(result.status, tasks.TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, tasks.TaskExecutionErrorCode.SANDBOX_FAILED)
        self.assertEqual(result.task_id, "r0b-fixture")
        self.assertEqual(result.revision, "a" * 40)
        self.assertTrue(result.cleanup_complete)
        self.assertIsNone(result.patch)
        self.assertFalse(result.sandbox_result.started)
        self.assertIn("withdrawn", result.sandbox_result.message)

    def frozen_arguments(self, **overrides):
        task = {**self.task, "environment": {
            "os_family": "Windows", "architecture": "AMD64",
            "python_version": "3.12", "lock_path": "uv.lock",
            "lock_sha256": hashlib.sha256(self.lock).hexdigest(),
        }}
        arguments = dict(
            task=task, workspace_root=self.workspace, workspace_revision="a" * 40,
            repository_root=self.repository, runtime_root=self.runtime,
            python_toolchain_root=self.root / "trusted-python", uv_executable=self.root / "uv.exe",
            destination=self.root / "toolchain-frozen",
        )
        arguments.update(overrides)
        return arguments

    def test_frozen_refuses_before_lock_uv_probe_sync_or_staging(self):
        arguments = self.frozen_arguments()
        with patch.object(frozen.platform, "system", return_value="Windows"), patch.object(
            frozen.platform, "machine", return_value="AMD64"
        ):
            events, result = self.invoke(frozen.stage_frozen_python_312, **arguments)
        self.assertEqual(events, [], "frozen preparation or executable probe was attempted")
        self.assertIsInstance(result, frozen.FrozenEnvironmentError)
        self.assertNotIsInstance(result, frozen.FrozenEnvironmentCleanupError)
        self.assertIn("withdrawn", str(result))
        self.assertFalse((self.root / "toolchain-frozen").exists())

    def builder_arguments(self, **overrides):
        arguments = dict(
            repository_root=self.repository, task=self.frozen_arguments()["task"], runtime_root=self.runtime,
            python_toolchain_root=self.root / "trusted-python",
            sdist_path=self.root / "source.tar.gz",
            setuptools_wheel_path=self.root / "setuptools.whl",
            wheel_wheel_path=self.root / "wheel.whl",
        )
        arguments.update(overrides)
        return arguments

    def assert_build_refusal(self, result):
        self.assertEqual(result.status, fuzzy.BuildStatus.FAILED)
        self.assertEqual(result.error_code, fuzzy.BuildErrorCode.SANDBOX_FAILED)
        self.assertTrue(result.cleanup_complete)
        self.assertIsNone(result.wheel)
        self.assertIsNone(result.provenance)
        self.assertFalse(result.sandbox_result.started)
        self.assertIsNone(result.sandbox_result.exit_code)
        self.assertTrue(result.sandbox_result.cleanup_complete)
        self.assertIn("withdrawn", result.sandbox_result.message)

    def test_fuzzysearch_refuses_before_token_artifacts_staging_and_sandbox(self):
        arguments = self.builder_arguments()
        with patch.object(fuzzy.platform, "system", return_value="Windows"):
            events, result = self.invoke(fuzzy.build_pinned_fuzzysearch_wheel, **arguments)
        self.assertEqual(events, [], "source build preparation was attempted")
        self.assert_build_refusal(result)

    def test_forbiddenfruit_refuses_before_host_python_probe_and_build(self):
        arguments = self.builder_arguments()
        with patch.object(fruit.platform, "system", return_value="Windows"):
            events, result = self.invoke(fruit.build_pinned_forbiddenfruit_wheel, **arguments)
        self.assertEqual(events, [], "forbiddenfruit preparation or host probe was attempted")
        self.assert_build_refusal(result)

    def test_builder_invalid_environment_is_rejected_without_preparation(self):
        arguments = self.builder_arguments()
        arguments["task"] = {**arguments["task"], "environment": {}}
        for entry in (fuzzy.build_pinned_fuzzysearch_wheel, fruit.build_pinned_forbiddenfruit_wheel):
            with self.subTest(entry=entry.__name__):
                events, result = self.invoke(entry, **arguments)
                self.assertEqual(events, [])
                self.assertEqual(result.error_code, fuzzy.BuildErrorCode.INVALID_INPUT)
                self.assertTrue(result.cleanup_complete)
                self.assertIsNone(result.wheel)
                self.assertIsNone(result.provenance)

    def test_task_invalid_shape_and_policy_preserve_identity_without_preparation(self):
        cases = (
            self.task_arguments(task=None),
            self.task_arguments(task={"task_id": "r0b-fixture", "target": {"revision": "a" * 40}}),
            self.task_arguments(access_policy=None),
            self.task_arguments(task={**self.task, "allowed_edit_scope": [{"path": "../escape", "operation": "modify"}]}),
        )
        for arguments in cases:
            with self.subTest(task=arguments["task"], policy=arguments["access_policy"]):
                events, result = self.invoke(tasks.execute_isolated_task, **arguments)
                self.assertEqual(events, [])
                self.assertEqual(result.error_code, tasks.TaskExecutionErrorCode.WORKSPACE_PREPARATION_FAILED)
                self.assertEqual(result.workspace_failure.code, workspaces.TaskWorkspaceErrorCode.INVALID_TASK)
                self.assertEqual(result.task_id, "r0b-fixture" if arguments["task"] else None)
                self.assertEqual(result.revision, "a" * 40 if arguments["task"] else None)
                self.assertTrue(result.cleanup_complete)
                self.assertIsNone(result.sandbox_result)
                self.assertIsNone(result.patch)

    def test_non_windows_requests_refuse_without_platform_probe_or_preparation(self):
        requests = (
            (tasks.execute_isolated_task, self.task_arguments()),
            (fuzzy.build_pinned_fuzzysearch_wheel, self.builder_arguments(task=None)),
            (fruit.build_pinned_forbiddenfruit_wheel, self.builder_arguments(task=None)),
        )
        for entry, arguments in requests:
            with self.subTest(entry=entry.__name__), patch.object(os, "name", "posix"):
                events, result = self.invoke(entry, **arguments)
            self.assertEqual(events, [])
            self.assertTrue(result.cleanup_complete)
            if entry is tasks.execute_isolated_task:
                self.assertEqual(result.sandbox_result.error_code, tasks.SandboxErrorCode.UNSUPPORTED_PLATFORM)
                self.assertEqual(result.task_id, "r0b-fixture")
                self.assertEqual(result.revision, "a" * 40)
                self.assertIsNone(result.patch)
                self.assertFalse(result.sandbox_result.started)
            else:
                self.assertEqual(result.error_code, fuzzy.BuildErrorCode.UNSUPPORTED_PLATFORM)
                self.assertIsNone(result.wheel)
                self.assertIsNone(result.provenance)

    def test_frozen_pure_input_errors_remain_errors_without_touching_paths(self):
        valid = self.frozen_arguments()
        bad_environment = (
            {},
            {**valid["task"]["environment"], "lock_path": "../escape"},
            {**valid["task"]["environment"], "lock_sha256": "bad"},
        )
        for environment in bad_environment:
            arguments = {**valid, "task": {**valid["task"], "environment": environment}}
            with self.subTest(environment=environment):
                events, result = self.invoke(frozen.stage_frozen_python_312, **arguments)
                self.assertEqual(events, [])
                self.assertIsInstance(result, frozen.FrozenEnvironmentError)
                self.assertNotIsInstance(result, frozen.FrozenEnvironmentCleanupError)
                self.assertNotIn("withdrawn", str(result))

    def test_refusal_never_cleans_caller_owned_storage(self):
        marker = self.runtime / "preexisting.txt"
        marker.write_bytes(b"caller owns this; not a resource created by execution")
        destination = self.root / "toolchain-frozen"
        destination.mkdir()
        (destination / "keep.txt").write_bytes(b"preexisting toolchain")
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        for entry, arguments in (
            (tasks.execute_isolated_task, self.task_arguments()),
            (frozen.stage_frozen_python_312, self.frozen_arguments()),
            (fuzzy.build_pinned_fuzzysearch_wheel, self.builder_arguments()),
            (fruit.build_pinned_forbiddenfruit_wheel, self.builder_arguments()),
        ):
            events, result = self.invoke(entry, **arguments)
            self.assertEqual(events, [])
            if entry is frozen.stage_frozen_python_312:
                self.assertIsInstance(result, frozen.FrozenEnvironmentError)
            else:
                self.assertTrue(result.cleanup_complete)
        after = {p.relative_to(self.root).as_posix(): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def prepare_owned_snapshot(self):
        # The fixture owns its Git history; checkout provenance and private
        # project commits are not dependencies. These files are data only.
        repository = self.repository
        (repository / "agent").mkdir()
        (repository / "tests").mkdir()
        (repository / "agent/outcome.py").write_bytes(b"# owned baseline data; never executed\n")
        (repository / "tests/test_graph_integration.py").write_bytes(b"# owned oracle marker; never collected\n")

        def git(*arguments):
            return subprocess.run(
                ["git", *arguments], cwd=repository, capture_output=True,
                check=True, shell=False, timeout=10,
            )

        git("init", "-q")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "user.name", "Owned snapshot fixture")
        git("add", ".")
        git("commit", "-qm", "owned data-only baseline")
        revision = git("rev-parse", "HEAD").stdout.decode("ascii").strip()
        task = {
            "task_id": "r0b-owned-snapshot",
            "target": {"revision": revision},
            "allowed_edit_scope": [{"path": "agent/outcome.py", "operation": "modify"}],
            "oracle": {"test_files": [{"path": "tests/test_graph_integration.py"}]},
        }
        prepared = workspaces.prepare_task_workspace(
            repository_root=repository, task=task,
            runtime_root=self.runtime, access_policy=WorkspaceAccessPolicy.default(),
        )
        self.assertTrue(prepared.ready, prepared.failure)
        owner = prepared.workspace
        self.addCleanup(owner.cleanup)
        return owner

    def test_independent_workspace_snapshot_patch_and_owner_cleanup_remain_usable(self):
        owner = self.prepare_owned_snapshot()
        self.assertEqual(owner.task_id, "r0b-owned-snapshot")
        self.assertFalse((owner.root / ".git").exists())
        self.assertFalse((owner.root / "tests/test_graph_integration.py").exists())
        target = owner.root / "agent/outcome.py"
        target.write_bytes(target.read_bytes() + b"\nOwned R0b data-only edit.\n")
        collected = owner.collect_patch()
        self.assertTrue(collected.valid, collected.failure)
        self.assertEqual([entry.path for entry in collected.entries], ["agent/outcome.py"])
        self.assertIsNone(owner.cleanup())
        self.assertFalse(owner.root.exists())
        self.assertIsNone(owner.cleanup())

    def test_independent_owner_cleanup_failure_is_reported_and_retry_cleans(self):
        owner = self.prepare_owned_snapshot()
        with patch.object(workspaces.shutil, "rmtree", side_effect=OSError("controlled cleanup fault")):
            failure = owner.cleanup()
        self.assertEqual(failure.code, workspaces.TaskWorkspaceErrorCode.CLEANUP_FAILED)
        self.assertTrue(owner.root.exists())
        self.assertIsNone(owner.cleanup())
        self.assertFalse(owner.root.exists())
