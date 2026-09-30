from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
import ctypes
from pathlib import Path
from unittest.mock import patch

from agent.evaluation.windows_sandbox import (
    SandboxCommand,
    SandboxErrorCode,
    SandboxStatus,
    run_in_windows_appcontainer,
)
from tests.evaluation.real_appcontainer_preflight import require_normal_user_token


_RUN_REAL_APPCONTAINER = (
    os.name == "nt" and os.environ.get("S5B_RUN_APPCONTAINER_TESTS") == "1"
)


@unittest.skipUnless(os.name == "nt", "Windows AppContainer is Windows-only.")
class WindowsAppContainerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if _RUN_REAL_APPCONTAINER:
            require_normal_user_token()
        self._temporary = tempfile.TemporaryDirectory(prefix="s5b-appcontainer-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "workspace"
        self.root.mkdir()
        self.cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"

    def _run(self, *argv: str, **options):
        return run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(str(self.cmd), *argv),
                **options,
            )
        )

    @unittest.skipUnless(
        _RUN_REAL_APPCONTAINER,
        "Set S5B_RUN_APPCONTAINER_TESTS=1 and run from a normal, non-elevated Windows user terminal; do not run profile setup under Codex elevation.",
    )
    def test_integration_runner_has_non_elevated_integrity(self) -> None:
        require_normal_user_token()

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_workspace_is_writable_but_host_and_oracle_canaries_are_denied(self) -> None:
        host = Path(self._temporary.name) / "host-canary.txt"
        oracle = Path(self._temporary.name) / "oracle-canary.txt"
        host.write_text("S5B_HOST_CANARY", encoding="utf-8")
        oracle.write_text("S5B_ORACLE_CANARY", encoding="utf-8")
        marker = self.root / "workspace.txt"
        write = self._run("/d", "/c", "echo workspace>workspace.txt")
        host_read = self._run("/d", "/c", f'type "{host}"')
        oracle_read = self._run("/d", "/c", f'type "{oracle}"')
        host_write = self._run("/d", "/c", f'echo tampered>>"{host}"')
        oracle_write = self._run("/d", "/c", f'echo tampered>>"{oracle}"')

        self.assertEqual(write.status, SandboxStatus.COMPLETED, write)
        self.assertTrue(write.token_is_appcontainer)
        self.assertTrue(host_read.started)
        self.assertTrue(oracle_read.started)
        self.assertTrue(host_write.started)
        self.assertTrue(oracle_write.started)
        self.assertEqual(host_read.status, SandboxStatus.FAILED, host_read)
        self.assertEqual(oracle_read.status, SandboxStatus.FAILED, oracle_read)
        self.assertEqual(host_write.status, SandboxStatus.FAILED, host_write)
        self.assertEqual(oracle_write.status, SandboxStatus.FAILED, oracle_write)
        self.assertEqual(marker.read_text().strip(), "workspace")
        self.assertNotIn(b"S5B_HOST_CANARY", host_read.stdout + host_read.stderr)
        self.assertNotIn(b"S5B_ORACLE_CANARY", oracle_read.stdout + oracle_read.stderr)
        self.assertEqual(host.read_text(), "S5B_HOST_CANARY")
        self.assertEqual(oracle.read_text(), "S5B_ORACLE_CANARY")
        self.assertFalse((self.root / oracle.name).exists())
        self.assertEqual(oracle.read_text(), "S5B_ORACLE_CANARY")
        self.assertTrue(
            all(
                item.cleanup_complete
                for item in (write, host_read, oracle_read, host_write, oracle_write)
            )
        )

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_secret_environment_is_not_inherited(self) -> None:
        canaries = {
            "OPENAI_API_KEY": "S5B_API_KEY_CANARY",
            "HTTPS_PROXY": "http://s5b-user:s5b-password@proxy.invalid:8080",
        }
        previous = {name: os.environ.get(name) for name in canaries}
        os.environ.update(canaries)
        try:
            result = self._run(
                "/d",
                "/c",
                "if defined OPENAI_API_KEY exit /b 23 & if defined HTTPS_PROXY exit /b 24 & exit /b 0",
            )
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual(result.status, SandboxStatus.COMPLETED, result)
        self.assertTrue(result.token_is_appcontainer)
        self.assertEqual(result.exit_code, 0)
        for canary in canaries.values():
            self.assertNotIn(canary.encode(), result.stdout + result.stderr)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_allowlisted_paths_are_derived_not_copied_from_parent_environment(self) -> None:
        variable = "ProgramData"
        previous = os.environ.get(variable)
        os.environ[variable] = "S5B_PARENT_ENV_CANARY"
        try:
            result = self._run("/d", "/c", "echo %ProgramData%")
        finally:
            if previous is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous

        self.assertEqual(result.status, SandboxStatus.COMPLETED, result)
        self.assertTrue(result.token_is_appcontainer)
        self.assertNotIn(b"S5B_PARENT_ENV_CANARY", result.stdout + result.stderr)
        self.assertIn(b"ProgramData", result.stdout)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_default_network_policy_blocks_a_controlled_loopback_canary(self) -> None:
        curl = Path(os.environ["SystemRoot"]) / "System32" / "curl.exe"
        executable_control = run_in_windows_appcontainer(
            SandboxCommand(workspace_root=self.root, argv=(str(curl), "--version"), timeout_seconds=5)
        )
        self.assertEqual(executable_control.status, SandboxStatus.COMPLETED, executable_control)
        self.assertTrue(executable_control.started and executable_control.token_is_appcontainer)
        self.assertTrue(executable_control.cleanup_complete)
        self.assertIn(b"curl", executable_control.stdout.lower())
        accepted: list[bool] = []
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(8)
        port = listener.getsockname()[1]

        def accept_once() -> None:
            for _ in range(2):
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    accepted.append(False)
                    return
                else:
                    accepted.append(True)
                    connection.close()

        thread = threading.Thread(target=accept_once, daemon=True)
        thread.start()
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        deadline = time.monotonic() + 1
        while not accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(accepted, [True], "Host control could not reach the local listener.")

        result = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(str(curl), "--connect-timeout", "2", f"http://127.0.0.1:{port}/"),
                timeout_seconds=5,
            )
        )
        thread.join(timeout=9)
        listener.close()

        self.assertEqual(accepted, [True, False], "AppContainer reached the controlled listener.")
        self.assertTrue(result.started, result)
        self.assertTrue(result.token_is_appcontainer, result)
        self.assertTrue(result.cleanup_complete, result)
        self.assertNotEqual(result.status, SandboxStatus.COMPLETED, result)
        self.assertTrue(result.exit_code != 0 or result.error_code is not None)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_timeout_kills_descendant_before_late_workspace_side_effect(self) -> None:
        delayed = self.root / "delayed.cmd"
        delayed.write_text(
            f'@echo off\nping -n 4 127.0.0.1 >nul\necho late>"{self.root / "late.txt"}"\n',
            encoding="ascii",
        )
        command = (
            f'start "" /b "{self.cmd}" /d /c ""{delayed}"" & '
            "ping -n 30 127.0.0.1 >nul"
        )

        result = self._run("/d", "/c", command, timeout_seconds=1)
        time.sleep(4)

        self.assertEqual(result.status, SandboxStatus.TIMED_OUT, result)
        self.assertTrue(result.token_is_appcontainer)
        self.assertFalse((self.root / "late.txt").exists())
        self.assertTrue(result.cleanup_complete)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_cpu_and_memory_limits_are_applied_to_the_real_process_job(self) -> None:
        powershell = (
            Path(os.environ["SystemRoot"])
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        cpu_rate = 1
        cpu_time_limit = 30
        # Exercise the same inline FOR workload in both the control and timeout run.
        # AppContainer may deny executing a .cmd file from the writable workspace.
        cpu_control = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(str(self.cmd), "/d", "/c", "(for /L %i in (1,1,64) do @echo.>nul) & echo CPU_READY"),
                timeout_seconds=5, cpu_rate_percent=cpu_rate, cpu_time_seconds=cpu_time_limit,
            )
        )
        self.assertEqual(cpu_control.status, SandboxStatus.COMPLETED, cpu_control)
        self.assertIn(b"CPU_READY", cpu_control.stdout)
        self.assertTrue(cpu_control.started and cpu_control.token_is_appcontainer and cpu_control.cleanup_complete)
        started = time.monotonic()
        cpu = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(str(self.cmd), "/d", "/c", "echo CPU_STARTED>cpu-started.txt & for /L %i in (1,1,2147483647) do @echo.>nul"),
                timeout_seconds=5,
                cpu_rate_percent=cpu_rate,
                cpu_time_seconds=cpu_time_limit,
            )
        )
        elapsed = time.monotonic() - started
        self.assertEqual(cpu.status, SandboxStatus.TIMED_OUT, cpu)
        self.assertGreaterEqual(elapsed, 4.0)
        self.assertEqual((self.root / "cpu-started.txt").read_text(encoding="ascii").strip(), "CPU_STARTED")
        allowed_cpu = elapsed * cpu_rate / 100 * (os.cpu_count() or 1)
        self.assertGreater(cpu.cpu_time_seconds, 0.0)
        self.assertLessEqual(cpu.cpu_time_seconds, allowed_cpu + 0.5)
        self.assertLessEqual(cpu.cpu_time_seconds, cpu_time_limit)
        self.assertTrue(cpu.started and cpu.token_is_appcontainer and cpu.cleanup_complete)

        memory_limit = 128 * 1024 * 1024
        memory_control = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(str(powershell), "-NoProfile", "-NonInteractive", "-Command", "$x=New-Object byte[] 1048576; [Console]::Out.Write('MEMORY_READY')"),
                timeout_seconds=8, memory_limit_bytes=memory_limit,
            )
        )
        self.assertEqual(memory_control.status, SandboxStatus.COMPLETED, memory_control)
        self.assertIn(b"MEMORY_READY", memory_control.stdout)
        self.assertTrue(memory_control.started and memory_control.token_is_appcontainer and memory_control.cleanup_complete)

        memory = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "try { $x=New-Object byte[] 536870912; [Console]::Out.Write('OVERALLOCATED'); exit 0 } catch { exit 17 }",
                ),
                timeout_seconds=8,
                memory_limit_bytes=memory_limit,
            )
        )
        self.assertTrue(memory.started, memory)
        self.assertTrue(memory.token_is_appcontainer)
        self.assertEqual(memory.status, SandboxStatus.FAILED, memory)
        self.assertTrue(memory.cleanup_complete, memory)
        self.assertNotIn(b"OVERALLOCATED", memory.stdout)
        self.assertLessEqual(memory.peak_memory_bytes, memory_limit)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_stdout_and_stderr_are_drained_but_bounded(self) -> None:
        powershell = (
            Path(os.environ["SystemRoot"])
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        result = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::Out.Write('A'*50000); [Console]::Error.Write('B'*50000)",
                ),
                timeout_seconds=5,
                output_limit_bytes=1024,
            )
        )

        self.assertEqual(result.status, SandboxStatus.COMPLETED, result)
        self.assertEqual(len(result.stdout), 1024)
        self.assertEqual(len(result.stderr), 1024)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_invalid_workspace_is_structured(self) -> None:
        missing = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root / "missing",
                argv=(str(self.cmd), "/d", "/c", "exit 0"),
            )
        )
        self.assertEqual(missing.status, SandboxStatus.FAILED)
        self.assertEqual(missing.error_code, SandboxErrorCode.INVALID_WORKSPACE)

        # A read-only workspace path cannot be used as an AppContainer mount.
        self.root.rmdir()
        self.root.write_text("not a directory", encoding="utf-8")
        invalid = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=self.root,
                argv=(str(self.cmd), "/d", "/c", "exit 0"),
            )
        )
        self.assertEqual(invalid.status, SandboxStatus.FAILED)
        self.assertIn(
            invalid.error_code,
            {SandboxErrorCode.INVALID_WORKSPACE, SandboxErrorCode.SETUP_FAILED},
        )

    def test_invalid_python_toolchain_path_is_structured_before_profile(self) -> None:
        for source in (123, Path("relative-toolchain")):
            with self.subTest(source=source):
                result = run_in_windows_appcontainer(
                    SandboxCommand(
                        workspace_root=self.root,
                        argv=(str(self.cmd), "/d", "/c", "exit 0"),
                        python_toolchain_root=source,
                    )
                )
                self.assertEqual(result.status, SandboxStatus.FAILED)
                self.assertEqual(result.error_code, SandboxErrorCode.INVALID_REQUEST)
                self.assertFalse(result.started)

    def test_missing_appcontainer_backend_fails_closed(self) -> None:
        from agent.evaluation import _windows_appcontainer

        unavailable = _windows_appcontainer._Win32Failure(
            SandboxErrorCode.SETUP_FAILED,
            "simulated per-user AppContainer profile failure",
        )
        with patch.object(
            _windows_appcontainer,
            "_create_profile",
            side_effect=unavailable,
        ):
            result = self._run("/d", "/c", "exit 0")

        self.assertEqual(result.status, SandboxStatus.FAILED)
        self.assertEqual(result.error_code, SandboxErrorCode.SETUP_FAILED)
        self.assertFalse(result.started)
        self.assertTrue(result.cleanup_complete)

    def test_workspace_acl_mount_failure_fails_closed(self) -> None:
        from agent.evaluation import _windows_appcontainer

        failure = _windows_appcontainer._Win32Failure(
            SandboxErrorCode.SETUP_FAILED,
            "simulated workspace ACL failure",
        )
        with (
            patch.object(
                _windows_appcontainer,
                "_create_profile",
                return_value=ctypes.c_void_p(1),
            ),
            patch.object(_windows_appcontainer, "_delete_profile", return_value=True),
            patch.object(_windows_appcontainer._advapi32, "FreeSid", return_value=1),
            patch.object(
                _windows_appcontainer,
                "_grant_workspace_acl",
                side_effect=failure,
            ),
        ):
            result = self._run("/d", "/c", "exit 0")

        self.assertEqual(result.status, SandboxStatus.FAILED)
        self.assertEqual(result.error_code, SandboxErrorCode.SETUP_FAILED)
        self.assertFalse(result.started)
        self.assertTrue(result.cleanup_complete)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_profile_cleanup_failure_is_not_reported_as_success(self) -> None:
        from agent.evaluation import _windows_appcontainer

        real_delete = _windows_appcontainer._delete_profile
        profile_names: list[str] = []

        def report_failure(name: str) -> bool:
            profile_names.append(name)
            return False

        try:
            with patch.object(_windows_appcontainer, "_delete_profile", side_effect=report_failure):
                result = self._run("/d", "/c", "exit 0")
        finally:
            for name in profile_names:
                real_delete(name)

        self.assertEqual(result.status, SandboxStatus.FAILED)
        self.assertEqual(result.error_code, SandboxErrorCode.CLEANUP_FAILED)
        self.assertFalse(result.cleanup_complete)

    @unittest.skipUnless(_RUN_REAL_APPCONTAINER, "Requires the normal-user AppContainer integration run.")
    def test_private_appcontainer_temp_is_removed_after_success(self) -> None:
        result = self._run("/d", "/c", "echo scratch>%TEMP%\\scratch.txt")

        self.assertEqual(result.status, SandboxStatus.COMPLETED, result)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(
            [path.name for path in self.root.iterdir() if path.name.startswith(".s5b-runtime-")],
            [],
        )


@unittest.skipIf(os.name == "nt", "Windows-only unsupported-platform contract.")
class UnsupportedSandboxPlatformTests(unittest.TestCase):
    def test_non_windows_backend_fails_closed(self) -> None:
        result = run_in_windows_appcontainer(
            SandboxCommand(workspace_root=Path("."), argv=("cmd.exe", "/c", "exit 0"))
        )
        self.assertEqual(result.status, SandboxStatus.FAILED)
        self.assertEqual(result.error_code, SandboxErrorCode.UNSUPPORTED_PLATFORM)


if __name__ == "__main__":
    unittest.main()
