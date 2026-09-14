"""Run configured verification commands inside one workspace boundary."""

import hashlib
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from agent.verification.contracts import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationSpec,
)
from agent.workspace import WorkspacePathResolver

_TRUNCATION_MARKER = b"\n... output truncated ...\n"


class VerificationRunner:
    """Execute argv checks without a shell and return bounded diagnostics."""

    def __init__(self, workspace_root: str | Path) -> None:
        self._resolver = WorkspacePathResolver(workspace_root)

    def run(self, spec: VerificationSpec) -> VerificationResult:
        started_at = time.monotonic()
        validation_error = _validate_spec(spec)
        if validation_error is not None:
            return _execution_error(spec, validation_error, started_at)

        resolution = self._resolver.resolve_directory(spec.cwd)
        if not resolution.ok or resolution.path is None:
            return _execution_error(
                spec,
                resolution.message or "The verification cwd is outside the workspace.",
                started_at,
            )

        try:
            process = subprocess.Popen(
                list(spec.argv),
                cwd=resolution.path,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except (OSError, ValueError) as error:
            return _execution_error(
                spec,
                f"Verification process could not start: {error}",
                started_at,
            )

        stdout_capture = _BoundedOutput(spec.max_output_bytes)
        stderr_capture = _BoundedOutput(spec.max_output_bytes)
        assert process.stdout is not None and process.stderr is not None
        readers = (
            threading.Thread(
                target=stdout_capture.consume, args=(process.stdout,), daemon=True
            ),
            threading.Thread(
                target=stderr_capture.consume, args=(process.stderr,), daemon=True
            ),
        )
        for reader in readers:
            reader.start()

        exit_code: int | None = None
        message = ""
        try:
            exit_code = process.wait(timeout=spec.timeout_seconds)
            status = (
                VerificationCheckStatus.PASS
                if exit_code == 0
                else VerificationCheckStatus.FAIL
            )
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            status = VerificationCheckStatus.TIMEOUT
            message = (
                f"Verification timed out after {spec.timeout_seconds:g} seconds."
            )
        for reader in readers:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            process.stdout.close()
            process.stderr.close()
            for reader in readers:
                reader.join(timeout=1)
            status = VerificationCheckStatus.EXECUTION_ERROR
            message = "Verification output pipes did not close after process exit."

        stdout, stdout_truncated, stdout_digest = stdout_capture.result()
        stderr, stderr_truncated, stderr_digest = stderr_capture.result()
        capture_error = stdout_capture.error or stderr_capture.error
        if capture_error is not None:
            status = VerificationCheckStatus.EXECUTION_ERROR
            message = f"Verification output could not be captured: {capture_error}"

        return VerificationResult.create(
            name=spec.name,
            argv=spec.argv,
            cwd=resolution.relative_path or ".",
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started_at,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            message=message,
        )


def _validate_spec(spec: VerificationSpec) -> str | None:
    if not spec.name.strip():
        return "Verification name must not be empty."
    if not spec.argv or any(not item or "\x00" in item for item in spec.argv):
        return "Verification argv must contain non-empty strings."
    if spec.timeout_seconds <= 0:
        return "Verification timeout must be greater than zero."
    if spec.max_output_bytes < len(_TRUNCATION_MARKER) + 2:
        return "Verification max_output_bytes is too small."
    return None


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        content_budget = limit - len(_TRUNCATION_MARKER)
        self._limit = limit
        self._head_limit = content_budget // 2
        self._tail_limit = content_budget - self._head_limit
        self._buffer = bytearray()
        self._head = b""
        self._tail = bytearray()
        self._truncated = False
        self._digest = hashlib.sha256()
        self.error: str | None = None

    def consume(self, stream: BinaryIO) -> None:
        try:
            try:
                while chunk := stream.read(8192):
                    self._digest.update(chunk)
                    self._append(chunk)
            except (OSError, ValueError) as error:
                self.error = f"{type(error).__name__}: {error}"
        finally:
            stream.close()

    def result(self) -> tuple[str, bool, str]:
        bounded = (
            self._head + _TRUNCATION_MARKER + self._tail
            if self._truncated
            else bytes(self._buffer)
        )
        return (
            bounded.decode("utf-8", errors="replace"),
            self._truncated,
            self._digest.hexdigest(),
        )

    def _append(self, chunk: bytes) -> None:
        if not self._truncated:
            combined = self._buffer + chunk
            if len(combined) <= self._limit:
                self._buffer = combined
                return
            self._truncated = True
            self._head = bytes(combined[: self._head_limit])
            self._tail = bytearray(combined[-self._tail_limit :])
            self._buffer.clear()
            return

        self._tail.extend(chunk)
        if len(self._tail) > self._tail_limit:
            del self._tail[: -self._tail_limit]


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        process.kill()
    process.wait()


def _execution_error(
    spec: VerificationSpec, message: str, started_at: float
) -> VerificationResult:
    return VerificationResult.create(
        name=spec.name,
        argv=spec.argv,
        cwd=spec.cwd,
        status=VerificationCheckStatus.EXECUTION_ERROR,
        exit_code=None,
        duration_seconds=time.monotonic() - started_at,
        message=message,
    )
