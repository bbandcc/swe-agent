"""R0a refusal only; never invoke a native profile, ACL or task process.

Backend import only loads DLL symbols. The guards are installed before either
execution seam is called, including during the expected pre-fix RED.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent.evaluation import windows_sandbox as sandbox


@contextmanager
def forbid_execution():
    from agent.evaluation import _windows_appcontainer as backend

    events = []

    def blocked(name):
        def reject(*_args, **_kwargs):
            events.append(name)
            raise backend._Win32Failure(sandbox.SandboxErrorCode.SETUP_FAILED,
                                        "Safety sentinel blocked " + name)
        return reject

    class NoNativeCalls:
        def __getattr__(self, name):
            return blocked("native." + name)

    with ExitStack() as stack:
        for name in ("_kernel32", "_advapi32", "_userenv"):
            stack.enter_context(patch.object(backend, name, NoNativeCalls()))
        for name in (
            "_create_profile", "_delete_profile", "_grant_workspace_acl",
            "_grant_python_toolchain_acl", "_create_pipe", "_open_nul_input",
            "_create_limited_job", "_startup_info", "_environment_block",
            "_remove_scratch_folder", "_close_handle",
        ):
            stack.enter_context(patch.object(backend, name, blocked(name)))
        for name in ("Popen", "run", "call", "check_call", "check_output"):
            stack.enter_context(patch.object(backend.subprocess, name, blocked("host." + name)))
        stack.enter_context(patch.object(Path, "mkdir", blocked("mkdir")))
        stack.enter_context(patch.object(os, "system", blocked("host.system")))
        yield events


@unittest.skipUnless(os.name == "nt", "Native backend import is Windows-only; no Linux backend is claimed.")
class WindowsProfileWithdrawalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="r0a-refusal-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # This is a valid regular executable path under the existing validator,
        # but deliberately contains no executable code. It is never launched.
        self.executable = self.root / "fixture.exe"
        self.executable.write_bytes(b"R0a authored non-executable sentinel\n")
        self.command = sandbox.SandboxCommand(self.root, (str(self.executable),))

    def test_public_valid_request_refused_before_resources(self):
        before = self.executable.read_bytes()
        with forbid_execution() as events:
            result = sandbox.run_in_windows_appcontainer(self.command)
        self.assertEqual(events, [], "profile/ACL/preparation/spawn was attempted")
        self.assertEqual(result.status, sandbox.SandboxStatus.FAILED)
        self.assertEqual(result.error_code, sandbox.SandboxErrorCode.SETUP_FAILED)
        self.assertIn("withdrawn", result.message.lower())
        self.assertFalse(result.started)
        self.assertFalse(result.token_is_appcontainer)
        self.assertIsNone(result.exit_code)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(self.executable.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.executable])

    def test_refusal_fields_have_no_execution_or_resource_usage(self):
        from agent.evaluation._windows_appcontainer import execute_appcontainer

        # Even an accidental unvalidated direct caller cannot allocate resources.
        with forbid_execution() as events:
            public = sandbox.run_in_windows_appcontainer(self.command)
            direct = execute_appcontainer(None)
        self.assertEqual(events, [])
        for result in (public, direct):
            self.assertEqual(result, sandbox.SandboxResult(
                status=sandbox.SandboxStatus.FAILED,
                error_code=sandbox.SandboxErrorCode.SETUP_FAILED,
                message="Windows AppContainer execution profile is withdrawn; "
                        "no resources were created and no command was started.",
                cleanup_complete=True,
            ))

    def test_invalid_request_limits_keep_priority_over_workspace(self):
        invalid = [None, object(), replace(self.command, argv=()),
                   replace(self.command, argv=[str(self.executable)]),
                   replace(self.command, argv=("bad\x00argv",)),
                   replace(self.command, timeout_seconds=float("nan")),
                   replace(self.command, timeout_seconds=float("inf")),
                   replace(self.command, timeout_seconds=True),
                   replace(self.command, memory_limit_bytes=True),
                   replace(self.command, cpu_rate_percent=101),
                   replace(self.command, cpu_time_seconds=0),
                   replace(self.command, output_limit_bytes=0),
                   replace(self.command, workspace_root=self.root / "missing", argv=())]
        with forbid_execution() as events:
            for command in invalid:
                with self.subTest(command=command):
                    result = sandbox.run_in_windows_appcontainer(command)
                    self.assertEqual(result.error_code, sandbox.SandboxErrorCode.INVALID_REQUEST)
                    self.assertEqual(result.status, sandbox.SandboxStatus.FAILED)
                    self.assertFalse(result.started)
                    self.assertTrue(result.cleanup_complete)
        self.assertEqual(events, [])

    def test_invalid_workspace_precedes_cwd_and_executable(self):
        not_directory = self.root / "file.txt"
        not_directory.write_bytes(b"owned data")
        with forbid_execution() as events:
            for workspace in (self.root / "missing", not_directory):
                result = sandbox.run_in_windows_appcontainer(replace(
                    self.command, workspace_root=workspace, cwd="../escape", argv=("relative.exe",),
                ))
                self.assertEqual(result.error_code, sandbox.SandboxErrorCode.INVALID_WORKSPACE)
                self.assertFalse(result.started)
                self.assertTrue(result.cleanup_complete)
        self.assertEqual(events, [])

    def test_cwd_executable_and_toolchain_validation_are_not_bypassed(self):
        invalid = (
            replace(self.command, cwd="../escape"),
            replace(self.command, cwd=str(self.root)),
            replace(self.command, cwd="missing-directory"),
            replace(self.command, argv=("relative.exe",)),
            replace(self.command, argv=(str(self.root / "missing.exe"),)),
            replace(self.command, python_toolchain_root=123),
            replace(self.command, python_toolchain_root=Path("relative-toolchain")),
        )
        with forbid_execution() as events:
            for command in invalid:
                with self.subTest(command=command):
                    result = sandbox.run_in_windows_appcontainer(command)
                    self.assertEqual(result.error_code, sandbox.SandboxErrorCode.INVALID_REQUEST)
                    self.assertFalse(result.started)
                    self.assertTrue(result.cleanup_complete)
        self.assertEqual(events, [])

    def test_valid_owned_sibling_toolchain_still_reaches_profile_refusal(self):
        with tempfile.TemporaryDirectory(prefix="toolchain-", dir=self.root.parent) as directory:
            toolchain = Path(directory)
            executable = toolchain / "python.exe"
            executable.write_bytes(b"authored non-executable fixture")
            with forbid_execution() as events:
                result = sandbox.run_in_windows_appcontainer(replace(
                    self.command, argv=(str(executable),), python_toolchain_root=toolchain,
                ))
            self.assertEqual(events, [])
            self.assertEqual(result.error_code, sandbox.SandboxErrorCode.SETUP_FAILED)
            self.assertIn("withdrawn", result.message)
            self.assertEqual(executable.read_bytes(), b"authored non-executable fixture")

    def test_task_consumer_keeps_identity_failure_and_cleanup_priority(self):
        from agent.evaluation import task_execution
        from agent.evaluation.task_workspace import (
            TaskWorkspaceErrorCode, TaskWorkspaceFailure, TaskWorkspaceResult,
        )
        from agent.workspace.policy import WorkspaceAccessPolicy

        failures = (None, TaskWorkspaceFailure(TaskWorkspaceErrorCode.CLEANUP_FAILED,
                                               "controlled owner cleanup failure"))
        for failure in failures:
            workspace = SimpleNamespace(
                root=self.root, task_id="r0a-fixture", revision="a" * 40,
                collect_patch=Mock(side_effect=AssertionError("refusal must not collect a success patch")),
                cleanup=Mock(return_value=failure),
            )
            # Only this caller-owned workspace is faked; the actual production
            # task consumer invokes the actual new public refusal function.
            with self.subTest(cleanup_failure=bool(failure)), forbid_execution() as events, patch.object(
                task_execution, "prepare_task_workspace", return_value=TaskWorkspaceResult(workspace),
            ):
                result = task_execution.execute_isolated_task(
                    repository_root=self.root, runtime_root=self.root / "unused-runtime",
                    task={"task_id": "r0a-fixture", "target": {"revision": "a" * 40}},
                    access_policy=WorkspaceAccessPolicy.default(), argv=self.command.argv,
                )
            self.assertEqual(events, [])
            self.assertEqual(result.status, task_execution.TaskExecutionStatus.FAILED)
            self.assertEqual(result.task_id, "r0a-fixture")
            self.assertEqual(result.revision, "a" * 40)
            self.assertEqual(result.sandbox_result.error_code, sandbox.SandboxErrorCode.SETUP_FAILED)
            self.assertFalse(result.sandbox_result.started)
            self.assertEqual(result.cleanup_complete, failure is None)
            self.assertEqual(result.error_code, task_execution.TaskExecutionErrorCode.CLEANUP_FAILED
                             if failure else task_execution.TaskExecutionErrorCode.SANDBOX_FAILED)
            self.assertIsNone(result.patch)
            workspace.collect_patch.assert_not_called()
            workspace.cleanup.assert_called_once_with()


    def test_direct_backend_refused_before_resources(self):
        from agent.evaluation._windows_appcontainer import execute_appcontainer

        command = sandbox._ValidatedCommand(
            workspace_root=self.root, cwd=self.root, argv=self.command.argv,
            timeout_seconds=30.0, memory_limit_bytes=512 * 1024 * 1024,
            cpu_rate_percent=50, cpu_time_seconds=300.0,
            output_limit_bytes=64 * 1024, python_toolchain_root=None,
        )
        with forbid_execution() as events:
            result = execute_appcontainer(command)
        self.assertEqual(events, [], "direct entry attempted native resources or host fallback")
        self.assertEqual(result.status, sandbox.SandboxStatus.FAILED)
        self.assertEqual(result.error_code, sandbox.SandboxErrorCode.SETUP_FAILED)
        self.assertIn("withdrawn", result.message.lower())
        self.assertFalse(result.started)
        self.assertFalse(result.token_is_appcontainer)
        self.assertIsNone(result.exit_code)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(list(self.root.iterdir()), [self.executable])


class UnsupportedPlatformWithdrawalTests(unittest.TestCase):
    def test_non_windows_precedes_invalid_input_without_backend_import(self):
        # No native module import or execution is needed for this platform gate.
        valid_shape = sandbox.SandboxCommand(Path("."), ("cmd.exe", "/c", "exit 0"))
        with patch.object(sandbox.os, "name", "posix"):
            for command in (valid_shape, None, object()):
                result = sandbox.run_in_windows_appcontainer(command)
                self.assertEqual(result.error_code, sandbox.SandboxErrorCode.UNSUPPORTED_PLATFORM)
                self.assertEqual(result.status, sandbox.SandboxStatus.FAILED)
                self.assertFalse(result.started)
                self.assertFalse(result.token_is_appcontainer)
                self.assertIsNone(result.exit_code)
                self.assertTrue(result.cleanup_complete)
