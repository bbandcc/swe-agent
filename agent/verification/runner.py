"""Run configured verification commands inside one workspace boundary."""

import hashlib
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from agent.verification.contracts import (
    VerificationCheckStatus,
    VerificationReport,
    VerificationResult,
    VerificationSpec,
)
from agent.verification.process_tree import ProcessTree
from agent.verification.report import VerificationReportError, parse_junit_xml
from agent.workspace import WorkspacePathResolver

_TRUNCATION_MARKER = b"\n... output truncated ...\n"
_MAX_REPORT_BYTES = 4 * 1024 * 1024


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

        report_path: Path | None = None
        report_before: tuple[object, ...] | None = None
        if spec.report_path is not None:
            report_resolution = self._resolver.resolve_file(
                spec.report_path, must_exist=False
            )
            if not report_resolution.ok or report_resolution.path is None:
                return _execution_error(
                    spec,
                    report_resolution.message
                    or "The verification report path is outside the workspace.",
                    started_at,
                )
            report_path = report_resolution.path
            report_before = _report_signature(report_path)

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
        process_tree = ProcessTree(process)

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
            process_tree.terminate()
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

        process_tree.close()

        report = None
        if report_path is not None and status not in {
            VerificationCheckStatus.TIMEOUT,
            VerificationCheckStatus.EXECUTION_ERROR,
        }:
            report, report_message = _load_report(
                report_path,
                before=report_before,
                check_id=spec.name,
            )
            if report_message:
                message = _append_message(message, report_message)

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
            report=report,
            allowed_failure_case_ids=spec.allowed_failure_case_ids,
        )


def _validate_spec(spec: VerificationSpec) -> str | None:
    if not spec.name.strip():
        return "Verification name must not be empty."
    if not spec.argv or any(not item or "\x00" in item for item in spec.argv):
        return "Verification argv must contain non-empty strings."
    if not math.isfinite(spec.timeout_seconds) or spec.timeout_seconds <= 0:
        return "Verification timeout must be a finite positive number."
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
        allowed_failure_case_ids=spec.allowed_failure_case_ids,
    )


def _report_signature(path: Path) -> tuple[object, ...] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return ("error", type(error).__name__, str(error))
    if stat.st_size > _MAX_REPORT_BYTES:
        return ("oversize", stat.st_size, stat.st_mtime_ns)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        return ("error", type(error).__name__, str(error))
    return (stat.st_size, stat.st_mtime_ns, digest)


def _load_report(
    path: Path,
    *,
    before: tuple[object, ...] | None,
    check_id: str,
) -> tuple[VerificationReport | None, str | None]:
    after = _report_signature(path)
    if after is None:
        return None, "Structured verification report is missing."
    if after == before:
        return None, "Structured verification report was not refreshed."
    if after and after[0] == "oversize":
        return None, "Structured verification report exceeds the bounded size."
    try:
        data = path.read_bytes()
    except OSError as error:
        return None, f"Structured verification report could not be read: {error}"
    if len(data) > _MAX_REPORT_BYTES:
        return None, "Structured verification report exceeds the bounded size."
    try:
        return parse_junit_xml(data, check_id=check_id), None
    except VerificationReportError as error:
        return None, f"Structured verification report is invalid: {error}"


def _append_message(current: str, addition: str) -> str:
    if not current:
        return addition
    return f"{current} {addition}"
