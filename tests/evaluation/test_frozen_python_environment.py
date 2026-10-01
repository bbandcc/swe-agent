from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.evaluation.task_execution import (
    TaskExecutionErrorCode,
    TaskExecutionStatus,
    execute_isolated_task,
)
from agent.evaluation.windows_sandbox import SandboxResult, SandboxStatus
from agent.workspace.policy import WorkspaceAccessPolicy


class FrozenPythonEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="s5b2-frozen-env-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Frozen environment test")
        (self.repository / "src").mkdir()
        (self.repository / "src" / "answer.py").write_text("answer = 1\n", encoding="utf-8")
        (self.repository / "tests").mkdir()
        (self.repository / "tests" / "oracle.txt").write_text("oracle\n", encoding="utf-8")
        self.pinned_lock = b"version = 1\n# pinned lock\n"
        (self.repository / "uv.lock").write_bytes(self.pinned_lock)
        (self.repository / "pyproject.toml").write_text(
            '[project]\nname = "frozen-canary"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n',
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-qm", "pinned Python environment")
        self.revision = self._git("rev-parse", "HEAD").stdout.decode().strip()
        self.lock_digest = hashlib.sha256(self.pinned_lock).hexdigest()
        self.task = {
            "task_id": "frozen-python-canary",
            "target": {"revision": self.revision},
            "allowed_edit_scope": [{"path": "src/answer.py", "operation": "modify"}],
            "oracle": {"test_files": [{"path": "tests/oracle.txt"}]},
            "environment": {
                "os_family": platform.system(),
                "architecture": platform.machine(),
                "python_version": "3.12",
                "lock_path": "uv.lock",
                "lock_sha256": self.lock_digest,
            },
        }
        self.runtime_root = self.root / "runtime"
        self.python_root = self.root / "trusted-python"
        (self.python_root / "Lib" / "encodings").mkdir(parents=True)
        (self.python_root / "Lib" / "encodings" / "__init__.py").write_text("", encoding="utf-8")
        (self.python_root / "DLLs").mkdir()
        (self.python_root / "python.exe").write_bytes(b"trusted-python-placeholder")
        self.uv_executable = self.root / "uv.exe"
        self.uv_executable.write_bytes(b"trusted-uv-placeholder")

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args], cwd=self.repository, check=True,
            capture_output=True, shell=False, timeout=10,
        )

    def _execute(self, task=None):
        return execute_isolated_task(
            repository_root=self.repository,
            task=task or self.task,
            runtime_root=self.runtime_root,
            access_policy=WorkspaceAccessPolicy.default(),
            argv=("python", "-c", "import frozen_dependency"),
            python_toolchain_root=self.python_root,
            uv_executable=self.uv_executable,
        )

    def test_pinned_target_lock_builds_bounded_staged_import_environment(self) -> None:
        from agent.evaluation import task_execution

        uv_calls: list[tuple[list[str], dict[str, str], Path]] = []

        def uv_sync(command, workspace, environment):
            command = list(command)
            environment = dict(environment)
            project_environment = Path(environment["UV_PROJECT_ENVIRONMENT"])
            uv_calls.append((command, environment, workspace))
            packages = project_environment / "Lib" / "site-packages" / "frozen_dependency"
            packages.mkdir(parents=True)
            (packages / "__init__.py").write_text("VALUE = 'from pinned lock'\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        fake_python = self.python_root / "python.exe"

        observed = []

        def sandbox(command):
            observed.append(command)
            toolchain = command.python_toolchain_root
            staged = toolchain / "Lib" / "site-packages" / "frozen_dependency" / "__init__.py"
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.read_text(encoding="utf-8"), "VALUE = 'from pinned lock'\n")
            self.assertEqual(command.argv[1:3], ("-B", "-S"))
            self.assertEqual(uv_calls[0][2], command.workspace_root)
            self.assertTrue("--locked" in uv_calls[0][0])
            self.assertTrue("--no-build" in uv_calls[0][0])
            self.assertTrue("--link-mode" in uv_calls[0][0])
            self.assertEqual(uv_calls[0][0][0], str(self.uv_executable))
            self.assertNotIn("PYTHONPATH", uv_calls[0][1])
            self.assertFalse(Path(uv_calls[0][1]["UV_PROJECT_ENVIRONMENT"]).exists())
            return SandboxResult(
                status=SandboxStatus.COMPLETED,
                exit_code=0,
                started=True,
                token_is_appcontainer=True,
                cleanup_complete=True,
            )

        with (
            patch.dict(os.environ, {"SystemRoot": str(self.root / "Windows")}),
            patch("agent.evaluation.frozen_environment.validate_python_312", return_value=fake_python),
            patch("agent.evaluation.frozen_environment._validate_uv_0121"),
            patch("agent.evaluation.python_toolchain.validate_python_312", return_value=fake_python),
            patch("agent.evaluation.frozen_environment._run_uv_sync", side_effect=uv_sync),
            patch.object(task_execution, "run_in_windows_appcontainer", side_effect=sandbox),
        ):
            result = self._execute()

        self.assertEqual(result.status, TaskExecutionStatus.COMMAND_COMPLETED, result)
        self.assertEqual(result.error_code, None)
        self.assertEqual(result.patch.entries, ())
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(len(uv_calls), 1)
        self.assertEqual(len(observed), 1)

    def test_uv_version_must_be_exactly_0121_before_sync_or_dispatch(self) -> None:
        from agent.evaluation import task_execution

        fake_python = self.python_root / "python.exe"
        with (
            patch.dict(os.environ, {"SystemRoot": str(self.root / "Windows")}),
            patch(
                "agent.evaluation.frozen_environment._read_uv_version",
                return_value="uv 0.12.2 (wrong version)",
            ),
            patch("agent.evaluation.frozen_environment.validate_python_312", return_value=fake_python),
            patch("agent.evaluation.python_toolchain.validate_python_312", return_value=fake_python),
            patch("agent.evaluation.frozen_environment._run_uv_sync") as uv_sync,
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute()

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        uv_sync.assert_not_called()
        sandbox.assert_not_called()

    def test_uv_sync_disallows_source_distribution_builds(self) -> None:
        from agent.evaluation import task_execution

        fake_python = self.python_root / "python.exe"
        commands: list[list[str]] = []

        def fail_binary_only_sync(command, workspace, environment):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 1, b"", b"no wheel available")

        with (
            patch.dict(os.environ, {"SystemRoot": str(self.root / "Windows")}),
            patch("agent.evaluation.frozen_environment.validate_python_312", return_value=fake_python),
            patch("agent.evaluation.frozen_environment._validate_uv_0121"),
            patch("agent.evaluation.python_toolchain.validate_python_312", return_value=fake_python),
            patch("agent.evaluation.frozen_environment._run_uv_sync", side_effect=fail_binary_only_sync),
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute()

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        self.assertIn("--no-build", commands[0])
        sandbox.assert_not_called()

    def test_current_checkout_lock_cannot_replace_pinned_target_lock(self) -> None:
        from agent.evaluation import task_execution

        current_lock = b"different current checkout lock\n"
        (self.repository / "uv.lock").write_bytes(current_lock)
        task = {
            **self.task,
            "environment": {
                **self.task["environment"],
                "lock_sha256": hashlib.sha256(current_lock).hexdigest(),
            },
        }
        with (
            patch("agent.evaluation.frozen_environment._run_uv_sync") as uv,
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute(task)
        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        uv.assert_not_called()
        sandbox.assert_not_called()

    def test_wrong_lock_hash_is_rejected_before_uv_or_sandbox(self) -> None:
        from agent.evaluation import task_execution

        task = {
            **self.task,
            "environment": {**self.task["environment"], "lock_sha256": "0" * 64},
        }
        with (
            patch("agent.evaluation.frozen_environment._run_uv_sync") as uv,
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute(task)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        uv.assert_not_called()
        sandbox.assert_not_called()

    def test_changed_target_revision_with_old_lock_identity_is_rejected(self) -> None:
        from agent.evaluation import task_execution

        (self.repository / "uv.lock").write_bytes(b"second revision lock\n")
        self._git("add", "uv.lock")
        self._git("commit", "-qm", "different lock revision")
        other_revision = self._git("rev-parse", "HEAD").stdout.decode().strip()
        task = {**self.task, "target": {"revision": other_revision}}
        with (
            patch("agent.evaluation.frozen_environment._run_uv_sync") as uv,
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute(task)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        uv.assert_not_called()
        sandbox.assert_not_called()

    def test_wrong_runtime_identity_is_rejected_before_sandbox(self) -> None:
        from agent.evaluation import task_execution

        task = {
            **self.task,
            "environment": {**self.task["environment"], "python_version": "3.11"},
        }
        with (
            patch("agent.evaluation.frozen_environment._run_uv_sync") as uv,
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute(task)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        uv.assert_not_called()
        sandbox.assert_not_called()

    def test_wrong_interpreter_architecture_is_rejected_before_sync_or_sandbox(self) -> None:
        from agent.evaluation import task_execution

        with (
            patch.dict(os.environ, {"SystemRoot": str(self.root / "Windows")}),
            patch("agent.evaluation.frozen_environment._validate_uv_0121"),
            patch(
                "agent.evaluation.python_toolchain._probe_python_312",
                return_value=("CPython", "3.12", "32", "ARM64", "win-arm64"),
            ),
            patch("agent.evaluation.frozen_environment._run_uv_sync") as uv_sync,
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute()

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        uv_sync.assert_not_called()
        sandbox.assert_not_called()

    def test_frozen_task_cannot_fall_back_to_unpinned_python(self) -> None:
        from agent.evaluation import task_execution

        with patch.object(task_execution, "run_in_windows_appcontainer") as sandbox:
            result = execute_isolated_task(
                repository_root=self.repository,
                task=self.task,
                runtime_root=self.runtime_root,
                access_policy=WorkspaceAccessPolicy.default(),
                argv=("python", "-c", "pass"),
                python_toolchain_root=self.python_root,
            )
        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        sandbox.assert_not_called()

    def test_uv_install_failure_is_structured_and_does_not_dispatch(self) -> None:
        from agent.evaluation import task_execution

        fake_python = self.python_root / "python.exe"
        with (
            patch.dict(os.environ, {"SystemRoot": str(self.root / "Windows")}),
            patch("agent.evaluation.frozen_environment.validate_python_312", return_value=fake_python),
            patch("agent.evaluation.frozen_environment._validate_uv_0121"),
            patch("agent.evaluation.python_toolchain.validate_python_312", return_value=fake_python),
            patch(
                "agent.evaluation.frozen_environment._run_uv_sync",
                return_value=subprocess.CompletedProcess(["uv"], 1, b"", b"untrusted package output"),
            ) as uv,
            patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
        ):
            result = self._execute()
        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID)
        self.assertIsNone(result.sandbox_result)
        self.assertTrue(result.cleanup_complete)
        uv.assert_called_once()
        sandbox.assert_not_called()

    def test_frozen_environment_cleanup_failure_is_top_level_cleanup_failure(self) -> None:
        import shutil

        from agent.evaluation import task_execution

        fake_python = self.python_root / "python.exe"
        temporary_roots: list[Path] = []
        real_rmtree = shutil.rmtree

        def fake_sync(command, workspace, environment):
            temporary_roots.append(Path(environment["TEMP"]))
            site_packages = Path(environment["UV_PROJECT_ENVIRONMENT"]) / "Lib" / "site-packages"
            module = site_packages / "frozen_dependency"
            module.mkdir(parents=True)
            (module / "__init__.py").write_text("VALUE = 1\\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def fail_frozen_temp_cleanup(path, *args, **kwargs):
            candidate = Path(path)
            if candidate.name.startswith("s5b-frozen-python-"):
                raise PermissionError("injected frozen environment cleanup failure")
            return real_rmtree(path, *args, **kwargs)

        try:
            with (
                patch.dict(os.environ, {"SystemRoot": str(self.root / "Windows")}),
                patch("agent.evaluation.frozen_environment.validate_python_312", return_value=fake_python),
                patch("agent.evaluation.frozen_environment._validate_uv_0121"),
                patch("agent.evaluation.python_toolchain.validate_python_312", return_value=fake_python),
                patch("agent.evaluation.frozen_environment._run_uv_sync", side_effect=fake_sync),
                patch("agent.evaluation.frozen_environment.shutil.rmtree", side_effect=fail_frozen_temp_cleanup),
                patch.object(task_execution, "run_in_windows_appcontainer") as sandbox,
            ):
                result = self._execute()
        finally:
            for temporary_root in temporary_roots:
                real_rmtree(temporary_root, ignore_errors=True)

        self.assertEqual(result.status, TaskExecutionStatus.FAILED)
        self.assertEqual(result.error_code, TaskExecutionErrorCode.CLEANUP_FAILED)
        self.assertFalse(result.cleanup_complete)
        self.assertIsNone(result.patch)
        sandbox.assert_not_called()


if __name__ == "__main__":
    unittest.main()
