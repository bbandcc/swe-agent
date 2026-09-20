"""Run configured verification commands inside one workspace boundary."""

import hashlib
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO

from agent.verification.contracts import (
    VerificationCheckStatus,
    VerificationReport,
    VerificationResult,
    VerificationSpec,
    is_pytest_junitxml_producer,
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
        owned_report_dir: Path | None = None
        effective_argv = list(spec.argv)
        if spec.report_path is not None:
            report_resolution = self._resolver.resolve_file(
                spec.report_path, must_exist=False
            )
            if not report_resolution.ok:
                return _execution_error(
                    spec,
                    report_resolution.message
                    or "The verification report path is outside the workspace.",
                    started_at,
                )
            assert report_resolution.path is not None
            try:
                report_path, owned_report_dir = _owned_report_path(spec.report_path)
                report_before = _report_signature(report_path)
            except OSError as error:
                return _execution_error(
                    spec,
                    f"Verification report output could not be allocated: {error}",
                    started_at,
                )
            effective_argv = _rewrite_report_argv(
                spec.argv,
                spec.report_path,
                report_path,
                workspace_relative=report_resolution.relative_path,
                workspace_path=report_resolution.path,
            )
            if effective_argv == list(spec.argv):
                cleanup_error = _cleanup_owned_report(owned_report_dir)
                message = "Verification report output could not be redirected."
                if cleanup_error is not None:
                    message += f" Temporary report cleanup failed: {cleanup_error}"
                return _execution_error(
                    spec,
                    message,
                    started_at,
                )

        process: subprocess.Popen[bytes] | None = None
        process_tree: ProcessTree | None = None
        result: VerificationResult | None = None
        cleanup_errors: list[str] = []
        try:
            process = subprocess.Popen(
                effective_argv,
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
            process_tree = ProcessTree(process)

            stdout_capture = _BoundedOutput(spec.max_output_bytes)
            stderr_capture = _BoundedOutput(spec.max_output_bytes)
            assert process.stdout is not None and process.stderr is not None
            readers = (
                threading.Thread(
                    target=stdout_capture.consume,
                    args=(process.stdout,),
                    daemon=True,
                ),
                threading.Thread(
                    target=stderr_capture.consume,
                    args=(process.stderr,),
                    daemon=True,
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
                message = (
                    f"Verification output could not be captured: {capture_error}"
                )

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

            result = VerificationResult.create(
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
        except (OSError, ValueError) as error:
            prefix = (
                "Verification process could not start"
                if process is None
                else "Verification execution failed"
            )
            result = _execution_error(
                spec,
                f"{prefix}: {error}",
                started_at,
            )
        finally:
            try:
                if process_tree is not None:
                    try:
                        if process is not None and process.poll() is None:
                            process_tree.terminate()
                    except OSError as error:
                        cleanup_errors.append(f"process cleanup failed: {error}")
                    finally:
                        try:
                            process_tree.close()
                        except OSError as error:
                            cleanup_errors.append(
                                f"process handle cleanup failed: {error}"
                            )
            finally:
                cleanup_error = _cleanup_owned_report(owned_report_dir)
                if cleanup_error is not None:
                    cleanup_errors.append(
                        f"temporary report cleanup failed: {cleanup_error}"
                    )

        if cleanup_errors:
            return _execution_error(
                spec,
                "Verification evidence cleanup failed: "
                + "; ".join(cleanup_errors),
                started_at,
            )
        assert result is not None
        return result


def _validate_spec(spec: VerificationSpec) -> str | None:
    if not spec.name.strip():
        return "Verification name must not be empty."
    if not spec.argv or any(not item or "\x00" in item for item in spec.argv):
        return "Verification argv must contain non-empty strings."
    if not math.isfinite(spec.timeout_seconds) or spec.timeout_seconds <= 0:
        return "Verification timeout must be a finite positive number."
    if spec.max_output_bytes < len(_TRUNCATION_MARKER) + 2:
        return "Verification max_output_bytes is too small."
    if spec.report_path is not None and not is_pytest_junitxml_producer(
        spec.argv, spec.report_path
    ):
        return "Verification report_path requires pytest --junitxml output."
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


def _owned_report_path(report_path: str) -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix="swe-agent-verification-"))
    return directory / Path(report_path).name, directory


def _rewrite_report_argv(
    argv: tuple[str, ...],
    configured_path: str,
    owned_path: Path,
    *,
    workspace_relative: str | None = None,
    workspace_path: Path | None = None,
) -> list[str]:
    """Redirect only an exact pytest JUnit destination value."""
    owned_text = owned_path.as_posix()
    candidates = {_report_path_key(configured_path)}
    if workspace_relative:
        candidates.add(_report_path_key(workspace_relative))
    if workspace_path is not None:
        candidates.add(_report_path_key(str(workspace_path)))
    rewritten: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        matched_option = next(
            (
                option
                for option in ("--junitxml", "--junit-xml")
                if item == option or item.startswith(f"{option}=")
            ),
            None,
        )
        if matched_option is None:
            rewritten.append(item)
            index += 1
            continue

        if item == matched_option:
            rewritten.append(item)
            if index + 1 < len(argv):
                value = argv[index + 1]
                if _report_path_key(value) in candidates:
                    rewritten.append(owned_text)
                else:
                    rewritten.append(value)
                index += 2
            else:
                index += 1
            continue

        value = item[len(matched_option) + 1 :]
        if _report_path_key(value) in candidates:
            rewritten.append(f"{matched_option}={owned_text}")
        else:
            rewritten.append(item)
        index += 1
    return rewritten


def _report_path_key(value: str) -> str:
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.casefold().startswith("workspace_repo/"):
        text = text[len("workspace_repo/") :]
    return os.path.normcase(text)


def _cleanup_owned_report(directory: Path | None) -> OSError | None:
    if directory is not None:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            return None
        except OSError as error:
            return error
    return None


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
