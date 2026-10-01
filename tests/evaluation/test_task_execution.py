from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tests.evaluation.real_appcontainer_preflight import require_normal_user_token
from unittest.mock import patch

from agent.evaluation.task_execution import (
    TaskExecutionErrorCode,
    TaskExecutionStatus,
    execute_isolated_task,
)
from agent.evaluation.windows_sandbox import SandboxErrorCode, SandboxResult, SandboxStatus
from agent.workspace.policy import WorkspaceAccessPolicy


_RUN_REAL_APPCONTAINER = (
    os.name == "nt" and os.environ.get("S5B_RUN_APPCONTAINER_TESTS") == "1"
)

_HIDDEN_PATH_COMMAND = (
    "if exist private\\secret.txt (exit /b 40) else "
    "(if exist tests\\oracle.txt (exit /b 41) else (echo changed>src\\answer.txt))"
)


class TaskExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        if _RUN_REAL_APPCONTAINER:
            require_normal_user_token()
        self.temporary = tempfile.TemporaryDirectory(prefix="s5b-task-execution-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "baseline"
        self.repository.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "S5b execution test")
        (self.repository / "src").mkdir()
        (self.repository / "src" / "answer.txt").write_text("baseline\n", encoding="utf-8")
        (self.repository / "tests").mkdir()
        (self.repository / "tests" / "oracle.txt").write_text("oracle-canary\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "pinned")
        self.revision = self._git("rev-parse", "HEAD").stdout.decode().strip()
        self.task = {
            "task_id": "execution-canary",
            "target": {"revision": self.revision},
            "allowed_edit_scope": [{"path": "src/answer.txt", "operation": "modify"}],
            "oracle": {"test_files": [{"path": "tests/oracle.txt"}]},
        }
        self.runtime_root = self.root / "runtime"

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repository,
            check=True,
            capture_output=True,
            shell=False,
            timeout=10,
        )

    def _execute(self, *, access_policy: WorkspaceAccessPolicy, **kwargs):
        return execute_isolated_task(
            repository_root=self.repository,
            task=self.task,
            runtime_root=self.runtime_root,
            access_policy=access_policy,
            argv=kwargs.pop("argv", ("cmd.exe", "/d", "/c", "exit 0")),
            **kwargs,
        )

    @unittest.skipUnless(os.name == "nt", "Checks the Windows cmd canary syntax.")
    def test_hidden_path_canary_command_writes_only_when_paths_are_absent(self) -> None:
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        control = self.root / "cmd-control"
        (control / "src").mkdir(parents=True)
        answer = control / "src" / "answer.txt"

        success = subprocess.run(
            (str(cmd), "/d", "/c", _HIDDEN_PATH_COMMAND),
            cwd=control, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(answer.read_bytes(), b"changed\r\n")

        answer.unlink()
        (control / "private").mkdir()
        (control / "private" / "secret.txt").write_text("HIDDEN-CANARY", encoding="utf-8")
        denied = subprocess.run(
            (str(cmd), "/d", "/c", _HIDDEN_PATH_COMMAND),
            cwd=control, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(denied.returncode, 40, denied)
        self.assertFalse(answer.exists())

        (control / "private" / "secret.txt").unlink()
        (control / "tests").mkdir()
        (control / "tests" / "oracle.txt").write_text("ORACLE-CANARY", encoding="utf-8")
        denied = subprocess.run(
            (str(cmd), "/d", "/c", _HIDDEN_PATH_COMMAND),
            cwd=control, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(denied.returncode, 41, denied)
        self.assertFalse(answer.exists())

    def test_default_policy_is_explicit_and_omission_is_rejected(self) -> None:
        completed = SandboxResult(
            status=SandboxStatus.COMPLETED,
            exit_code=0,
            started=True,
            cleanup_complete=True,
        )
        with patch(
            "agent.evaluation.task_execution.run_in_windows_appcontainer",
            return_value=completed,
        ):
            result = self._execute(access_policy=WorkspaceAccessPolicy.default())
        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertTrue(result.cleanup_complete)

        with patch("agent.evaluation.task_execution.run_in_windows_appcontainer") as runner:
            missing_runtime = self.root / "missing-policy-runtime"
            with self.assertRaises(TypeError):
                execute_isolated_task(
                    repository_root=self.repository,
                    task=self.task,
                    runtime_root=missing_runtime,
                    argv=("cmd.exe", "/d", "/c", "exit 0"),
                )
        runner.assert_not_called()
        self.assertFalse(missing_runtime.exists())

    def test_invalid_policy_fails_before_copy_or_sandbox_dispatch(self) -> None:
        from agent.evaluation import task_execution

        for policy in (None, object()):
            self.runtime_root = self.root / f"invalid-policy-{type(policy).__name__}"
            with self.subTest(policy=policy), patch.object(
                task_execution, "run_in_windows_appcontainer"
            ) as runner:
                result = self._execute(access_policy=policy)
                runner.assert_not_called()
                self.assertEqual(result.status, TaskExecutionStatus.FAILED)
                self.assertEqual(
                    result.error_code,
                    TaskExecutionErrorCode.WORKSPACE_PREPARATION_FAILED,
                )
                self.assertEqual(result.workspace_failure.code.value, "invalid_task")
                self.assertFalse(self.runtime_root.exists())

    def test_preparation_failure_never_starts_sandbox_or_returns_success(self) -> None:
        from agent.evaluation import task_execution
        from agent.evaluation.task_workspace import TaskWorkspaceErrorCode

        task = {**self.task, "target": {"revision": "0" * 40}}
        with patch.object(task_execution, "run_in_windows_appcontainer") as runner:
            result = execute_isolated_task(
                repository_root=self.repository,
                task=task,
                runtime_root=self.runtime_root,
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("cmd.exe", "/c", "exit 0"),
            )
            self.assertEqual(result.status, TaskExecutionStatus.FAILED)
            self.assertEqual(result.error_code, TaskExecutionErrorCode.WORKSPACE_PREPARATION_FAILED)
            self.assertIsNone(result.patch)
            self.assertTrue(result.cleanup_complete)

            for runtime in (self.repository, self.root):
                with self.subTest(runtime=runtime):
                    overlap = execute_isolated_task(
                        repository_root=self.repository,
                        task=self.task,
                        runtime_root=runtime,
                        access_policy=WorkspaceAccessPolicy.default(),
                        argv=("cmd.exe", "/d", "/c", "exit 0"),
                    )
                    self.assertEqual(overlap.status, TaskExecutionStatus.FAILED)
                    self.assertEqual(
                        overlap.error_code,
                        TaskExecutionErrorCode.WORKSPACE_PREPARATION_FAILED,
                    )
                    self.assertEqual(
                        overlap.workspace_failure.code,
                        TaskWorkspaceErrorCode.INVALID_TASK,
                    )
                    self.assertIsNone(overlap.patch)
                    self.assertTrue(overlap.cleanup_complete)
                    self.assertFalse(any(self.root.glob("task-*")))

        runner.assert_not_called()

    def test_trusted_hidden_scope_conflict_never_starts_sandbox(self) -> None:
        from agent.evaluation import task_execution

        with patch.object(task_execution, "run_in_windows_appcontainer") as runner:
            result = self._execute(access_policy=WorkspaceAccessPolicy(hidden_paths=("src/answer.txt",)))
        runner.assert_not_called()
        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.workspace_failure.code.value, "invalid_task")
        self.assertFalse(self.runtime_root.exists())

    def test_trusted_hidden_content_is_absent_before_sandbox_dispatch(self) -> None:
        from agent.evaluation import task_execution

        (self.repository / "private").mkdir()
        (self.repository / "private" / "secret.txt").write_text("HIDDEN-CANARY", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "hidden path")
        task = {**self.task, "target": {"revision": self._git("rev-parse", "HEAD").stdout.decode().strip()}}

        def inspect(command):
            self.assertFalse((command.workspace_root / "private").exists())
            self.assertFalse((command.workspace_root / "tests" / "oracle.txt").exists())
            return SandboxResult(status=SandboxStatus.COMPLETED, exit_code=0, started=True, cleanup_complete=True)

        with patch.object(task_execution, "run_in_windows_appcontainer", side_effect=inspect):
            result = execute_isolated_task(
                repository_root=self.repository, task=task, runtime_root=self.runtime_root,
                access_policy=WorkspaceAccessPolicy(hidden_paths=("private",)),
                argv=("cmd.exe", "/c", "exit 0"),
            )
        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertEqual((self.repository / "private" / "secret.txt").read_text(encoding="utf-8"), "HIDDEN-CANARY")

    def test_missing_trusted_python_toolchain_fails_before_sandbox(self) -> None:
        from agent.evaluation import task_execution

        with patch.object(task_execution, "run_in_windows_appcontainer") as runner:
            result = self._execute(
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("python", "-c", "print(1)"),
                python_toolchain_root=self.root / "missing",
            )
        runner.assert_not_called()
        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.TOOLCHAIN_INVALID)

    def test_python_toolchain_cannot_originate_from_untrusted_task_repository(self) -> None:
        from agent.evaluation import task_execution

        with patch.object(task_execution, "run_in_windows_appcontainer") as runner:
            result = self._execute(
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("python", "-c", "print(1)"),
                python_toolchain_root=self.repository,
            )
        runner.assert_not_called()
        self.assertEqual(result.error_code, TaskExecutionErrorCode.TOOLCHAIN_INVALID)

    @unittest.skipUnless(os.name == "nt", "Windows Python toolchain contract.")
    def test_python_toolchain_is_staged_outside_editable_workspace(self) -> None:
        from agent.evaluation import task_execution

        observed = []
        def control(command):
            observed.append(command)
            executable = Path(command.argv[0])
            self.assertFalse(executable.is_relative_to(command.workspace_root))
            self.assertEqual(command.python_toolchain_root, executable.parent)
            self.assertEqual(command.argv[1:3], ("-B", "-S"))
            completed = subprocess.run(
                [str(executable), "-B", "-S", "-c", "import sys, pathlib, unittest, json; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.executable).resolve().parent; print(sys.version_info[:2])"],
                cwd=command.workspace_root, env={"SystemRoot": os.environ["SystemRoot"], "PYTHONHOME": str(executable.parent)},
                capture_output=True, timeout=10, shell=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(b"(3, 12)", completed.stdout)
            return SandboxResult(status=SandboxStatus.COMPLETED, exit_code=0, started=True, token_is_appcontainer=True, cleanup_complete=True)

        with patch.object(task_execution, "run_in_windows_appcontainer", side_effect=control):
            result = self._execute(
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("python", "-c", "print('ok')"),
                python_toolchain_root=Path(sys.base_prefix),
            )
        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].python_toolchain_root.exists())

    def test_sandbox_failure_is_not_accepted_and_never_collects_patch(self) -> None:
        from agent.evaluation import task_execution

        failure = SandboxResult(
            status=SandboxStatus.FAILED,
            error_code=SandboxErrorCode.PROCESS_FAILED,
            exit_code=23,
            cleanup_complete=True,
        )
        with patch.object(task_execution, "run_in_windows_appcontainer", return_value=failure):
            result = self._execute(access_policy=WorkspaceAccessPolicy.default())

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.SANDBOX_FAILED)
        self.assertEqual(result.sandbox_result, failure)
        self.assertIsNone(result.patch)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(self._git("status", "--porcelain").stdout, b"")

    def test_timeout_is_not_accepted_and_does_not_publish_patch(self) -> None:
        from agent.evaluation import task_execution

        timeout = SandboxResult(
            status=SandboxStatus.TIMED_OUT,
            error_code=SandboxErrorCode.TIMEOUT,
            cleanup_complete=True,
        )
        with patch.object(task_execution, "run_in_windows_appcontainer", return_value=timeout):
            result = self._execute(access_policy=WorkspaceAccessPolicy.default())

        self.assertEqual(result.status, TaskExecutionStatus.TIMED_OUT)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.TIMEOUT)
        self.assertIsNone(result.patch)
        self.assertTrue(result.cleanup_complete)

    @unittest.skipUnless(
        _RUN_REAL_APPCONTAINER,
        "Set S5B_RUN_APPCONTAINER_TESTS=1 and run from a normal, non-elevated Windows user terminal.",
    )
    def test_sibling_baseline_copy_cannot_be_read_or_modified(self) -> None:
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        read = self._execute(
            access_policy=WorkspaceAccessPolicy.default(),
            argv=(str(cmd), "/d", "/c", "type ..\\baseline\\src\\answer.txt"),
            timeout_seconds=10,
        )
        write = self._execute(
            access_policy=WorkspaceAccessPolicy.default(),
            argv=(str(cmd), "/d", "/c", "echo attacked>..\\baseline\\src\\answer.txt"),
            timeout_seconds=10,
        )

        self.assertEqual(read.status, TaskExecutionStatus.FAILED, read)
        self.assertEqual(write.status, TaskExecutionStatus.FAILED, write)
        self.assertTrue(read.sandbox_result.started)
        self.assertTrue(write.sandbox_result.started)
        self.assertTrue(read.sandbox_result.token_is_appcontainer)
        self.assertTrue(write.sandbox_result.token_is_appcontainer)
        self.assertNotIn(b"baseline", read.sandbox_result.stdout.lower())
        self.assertIsNone(read.patch)
        self.assertIsNone(write.patch)
        self.assertEqual(
            (self.repository / "src" / "answer.txt").read_text(encoding="utf-8"),
            "baseline\n",
        )
        self.assertEqual(self._git("status", "--porcelain").stdout, b"")

    @unittest.skipUnless(
        _RUN_REAL_APPCONTAINER,
        "Set S5B_RUN_APPCONTAINER_TESTS=1 and run from a normal, non-elevated Windows user terminal.",
    )
    def test_real_sandbox_collects_only_pinned_task_patch_then_cleans_copy(self) -> None:
        from agent.evaluation.task_workspace import _git_text

        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        result = self._execute(
            access_policy=WorkspaceAccessPolicy.default(),
            argv=(
                str(cmd),
                "/d",
                "/c",
                "echo changed>src\\answer.txt & "
                "if exist tests\\oracle.txt exit /b 19 & exit /b 0",
            ),
            timeout_seconds=10,
        )

        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertEqual(result.task_id, "execution-canary")
        self.assertEqual(result.revision, self.revision)
        self.assertTrue(result.sandbox_result.token_is_appcontainer)
        self.assertTrue(result.cleanup_complete)
        self.assertTrue(result.patch.valid)
        self.assertEqual([entry.path for entry in result.patch.entries], ["src/answer.txt"])
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.decode().strip(), self.revision)
        self.assertEqual(self._git("status", "--porcelain").stdout, b"")

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires opt-in normal-user AppContainer integration run.")
    def test_trusted_hidden_content_is_absent_in_isolated_execution(self) -> None:
        (self.repository / "private").mkdir()
        (self.repository / "private" / "secret.txt").write_text("HIDDEN-CANARY", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "hidden path canary")
        task = {**self.task, "target": {"revision": self._git("rev-parse", "HEAD").stdout.decode().strip()}}
        baseline_file = self.repository / "src" / "answer.txt"
        private_file = self.repository / "private" / "secret.txt"
        baseline_before = baseline_file.read_bytes()
        private_before = private_file.read_bytes()
        repository_head_before = self._git("rev-parse", "HEAD").stdout
        repository_status_before = self._git("status", "--porcelain").stdout
        self.assertEqual(repository_status_before, b"")
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        result = execute_isolated_task(
            repository_root=self.repository, task=task, runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy(hidden_paths=("private",)),
            argv=(str(cmd), "/d", "/c", _HIDDEN_PATH_COMMAND),
        )
        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertTrue(result.sandbox_result.started and result.sandbox_result.token_is_appcontainer)
        self.assertTrue(result.sandbox_result.cleanup_complete)
        self.assertNotIn(b"HIDDEN-CANARY", result.sandbox_result.stdout + result.sandbox_result.stderr)
        self.assertEqual([item.path for item in result.patch.entries], ["src/answer.txt"])
        self.assertNotEqual(result.patch.entries[0].before_sha256, result.patch.entries[0].after_sha256)
        baseline_after = baseline_file.read_bytes()
        private_after = private_file.read_bytes()
        repository_head_after = self._git("rev-parse", "HEAD").stdout
        repository_status_after = self._git("status", "--porcelain").stdout
        self.assertEqual(baseline_after, baseline_before)
        self.assertEqual(private_after, private_before)
        self.assertEqual(repository_head_after, repository_head_before)
        self.assertEqual(repository_status_after, repository_status_before)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires opt-in normal-user AppContainer integration run.")
    def test_trusted_python_312_runs_and_cannot_modify_toolchain(self) -> None:
        (self.repository / "tests" / "test_public.py").write_text(
            "import pathlib, sys, unittest\n"
            "class PythonControl(unittest.TestCase):\n"
            "    def test_runtime(self):\n"
            "        self.assertEqual(sys.version_info[:2], (3, 12))\n"
            "        self.assertEqual(pathlib.Path(sys.prefix).resolve(), pathlib.Path(sys.executable).resolve().parent)\n"
            "        with self.assertRaises(PermissionError):\n"
            "            (pathlib.Path(sys.prefix) / 'task-wrote.txt').write_text('unsafe')\n"
            "        pathlib.Path('src/answer.txt').write_text('python-ok\\n')\n",
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-qm", "public Python check")
        task = {**self.task, "target": {"revision": self._git("rev-parse", "HEAD").stdout.decode().strip()}}
        result = execute_isolated_task(
            repository_root=self.repository, task=task, runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy.default(),
            argv=("python", "-m", "unittest", "discover", "-s", "tests", "-v"),
            python_toolchain_root=Path(sys.base_prefix), timeout_seconds=20,
        )
        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertTrue(result.sandbox_result.started and result.sandbox_result.token_is_appcontainer)
        self.assertTrue(result.sandbox_result.cleanup_complete)
        self.assertIn(b"Ran 1 test", result.sandbox_result.stderr)
        self.assertEqual([item.path for item in result.patch.entries], ["src/answer.txt"])
        self.assertFalse((Path(sys.base_prefix) / "task-wrote.txt").exists())

    @unittest.skipUnless(
        _RUN_REAL_APPCONTAINER,
        "Set S5B_RUN_APPCONTAINER_TESTS=1 and run from a normal, non-elevated Windows user terminal.",
    )
    def test_frozen_s5a_dependencies_import_in_appcontainer(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        manifest = json.loads((project_root / "evals" / "s5a" / "tasks.v1.json").read_text(encoding="utf-8"))
        task = next(item for item in manifest["tasks"] if item["task_id"] == "symbols-python-exact-source-v1")
        self.assertEqual(task["target"]["revision"], "7ed6bc9ee41b0f5586c0da4c980d790afb59f945")
        self.assertEqual(
            task["environment"]["lock_sha256"],
            "c270b21033292b4c578ca30cbe06a79718a33f272d2a05570aaaa31f57dca04f",
        )
        uv_path = os.environ.get("S5B_UV_EXECUTABLE")
        self.assertTrue(uv_path, "Set S5B_UV_EXECUTABLE to an absolute trusted uv.exe path.")
        uv_executable = Path(uv_path).resolve(strict=True)
        import tree_sitter_languages

        source_dependency = Path(tree_sitter_languages.__file__).resolve(strict=True)
        source_literal = repr(str(source_dependency))
        python_code = (
            "import importlib, os, pathlib, sys\n"
            "site = pathlib.Path(sys.prefix) / 'Lib' / 'site-packages'\n"
            "assert pathlib.Path(sys.executable).resolve().parent == pathlib.Path(sys.prefix).resolve()\n"
            "assert pathlib.Path(os.environ['PYTHONPATH']).resolve() == site.resolve()\n"
            "assert 'S5B_PARENT_PYTHONPATH_CANARY' not in os.environ.get('PYTHONPATH', '')\n"
            "assert not any(name.startswith('UV_') for name in os.environ)\n"
            "mods = [importlib.import_module(name) for name in ('langchain_core', 'tree_sitter', 'tree_sitter_languages')]\n"
            "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(site.resolve()) for m in mods)\n"
            "try:\n"
            f"    pathlib.Path({source_literal}).read_bytes()\n"
            "except (PermissionError, FileNotFoundError):\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('source dependency environment is readable')\n"
            "package_file = pathlib.Path(importlib.import_module('tree_sitter_languages').__file__)\n"
            "try:\n"
            "    package_file.write_bytes(package_file.read_bytes() + b'\\n# task mutation')\n"
            "except PermissionError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('staged dependency is writable')\n"
            "print('FROZEN_IMPORT_CANARY_OK')\n"
        )
        task_runtime = self.root / "real-frozen-runtime"
        previous = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = "S5B_PARENT_PYTHONPATH_CANARY"
        try:
            result = execute_isolated_task(
                repository_root=project_root,
                task=task,
                runtime_root=task_runtime,
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("python", "-c", python_code),
                python_toolchain_root=Path(sys.base_prefix),
                uv_executable=uv_executable,
                timeout_seconds=120,
            )
        finally:
            if previous is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous

        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.patch.entries, ())
        self.assertTrue(result.cleanup_complete)
        self.assertTrue(result.sandbox_result.started)
        self.assertTrue(result.sandbox_result.token_is_appcontainer)
        self.assertTrue(result.sandbox_result.cleanup_complete)
        self.assertIn(b"FROZEN_IMPORT_CANARY_OK", result.sandbox_result.stdout)


if __name__ == "__main__":
    unittest.main()
