"""Run configured verification commands inside one workspace boundary."""

import hashlib
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from agent.artifacts import (
    DEFAULT_ARTIFACT_QUOTA_BYTES,
    ArtifactErrorCode,
    ArtifactRef,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactWriter,
)
from agent.runtime.secrets import KnownSecretFilter
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

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        runtime_root: str | Path | None = None,
        artifact_store: ArtifactStore | None = None,
        secret_filter: KnownSecretFilter | None = None,
        artifact_quota_bytes: int = DEFAULT_ARTIFACT_QUOTA_BYTES,
    ) -> None:
        self._resolver = WorkspacePathResolver(workspace_root)
        self._secret_filter = secret_filter
        self._artifact_store = artifact_store or (
            ArtifactStore(runtime_root, max_bytes=artifact_quota_bytes)
            if runtime_root is not None
            else None
        )

    def run(self, spec: VerificationSpec) -> VerificationResult:
        started_at = time.monotonic()
        validation_error = _validate_spec(spec)
        if validation_error is not None:
            return self._safe_result(
                _execution_error(spec, validation_error, started_at)
            )

        resolution = self._resolver.resolve_directory(spec.cwd)
        if not resolution.ok or resolution.path is None:
            return self._safe_result(
                _execution_error(
                    spec,
                    resolution.message
                    or "The verification cwd is outside the workspace.",
                    started_at,
                )
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
                return self._safe_result(
                    _execution_error(
                        spec,
                        report_resolution.message
                        or "The verification report path is outside the workspace.",
                        started_at,
                    )
                )
            assert report_resolution.path is not None
            try:
                report_path, owned_report_dir = _owned_report_path(spec.report_path)
                report_before = _report_signature(report_path)
            except OSError as error:
                return self._safe_result(
                    _execution_error(
                        spec,
                        f"Verification report output could not be allocated: {error}",
                        started_at,
                    )
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
                return self._safe_result(
                    _execution_error(
                        spec,
                        message,
                        started_at,
                    )
                )

        process: subprocess.Popen[bytes] | None = None
        process_tree: ProcessTree | None = None
        result: VerificationResult | None = None
        stdout_capture: _BoundedOutput | None = None
        stderr_capture: _BoundedOutput | None = None
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

            stdout_capture = _BoundedOutput(
                spec.max_output_bytes,
                artifact_store=self._artifact_store,
                artifact_kind="verification_stdout",
                secret_filter=self._secret_filter,
            )
            stderr_capture = _BoundedOutput(
                spec.max_output_bytes,
                artifact_store=self._artifact_store,
                artifact_kind="verification_stderr",
                secret_filter=self._secret_filter,
            )
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

            stdout_result = stdout_capture.result()
            stderr_result = stderr_capture.result()
            stdout = stdout_result.preview
            stderr = stderr_result.preview
            stdout_truncated = stdout_result.truncated
            stderr_truncated = stderr_result.truncated
            stdout_digest = stdout_result.digest
            stderr_digest = stderr_result.digest
            capture_error = stdout_capture.error or stderr_capture.error
            if capture_error is not None:
                status = VerificationCheckStatus.EXECUTION_ERROR
                message = (
                    f"Verification output could not be captured: {capture_error}"
                )
            artifact_errors = tuple(
                item
                for item in (
                    stdout_result.artifact_error,
                    stderr_result.artifact_error,
                )
                if item is not None
            )
            if artifact_errors:
                message = _append_message(
                    message,
                    "Verification artifact evidence is incomplete: "
                    + "; ".join(error.message for error in artifact_errors),
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
                stdout_artifact=stdout_result.artifact,
                stderr_artifact=stderr_result.artifact,
                artifact_error_code=(
                    artifact_errors[0].code.value if artifact_errors else None
                ),
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
            for capture in (stdout_capture, stderr_capture):
                if capture is not None:
                    capture.abort()
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
            return self._safe_result(
                _execution_error(
                    spec,
                    "Verification evidence cleanup failed: "
                    + "; ".join(cleanup_errors),
                    started_at,
                )
            )
        assert result is not None
        return self._safe_result(result)

    def _safe_result(self, result: VerificationResult) -> VerificationResult:
        if self._secret_filter is None:
            return result
        sanitized = self._secret_filter.sanitize(result).value
        if not isinstance(sanitized, VerificationResult):
            raise TypeError("Secret filter must preserve VerificationResult type.")
        return sanitized


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


@dataclass(frozen=True, slots=True)
class _ArtifactFailure:
    code: ArtifactErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class _CaptureResult:
    preview: str
    truncated: bool
    digest: str
    artifact: ArtifactRef | None
    artifact_error: _ArtifactFailure | None


class _BoundedOutput:
    def __init__(
        self,
        limit: int,
        *,
        artifact_store: ArtifactStore | None = None,
        artifact_kind: str | None = None,
        secret_filter: KnownSecretFilter | None = None,
    ) -> None:
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
        self._secret_stream = (
            secret_filter.stream() if secret_filter is not None else None
        )
        self._artifact_writer: ArtifactWriter | None = None
        self._artifact: ArtifactRef | None = None
        self._artifact_error: _ArtifactFailure | None = None
        if artifact_store is not None and artifact_kind is not None:
            try:
                self._artifact_writer = artifact_store.open_stream(
                    kind=artifact_kind,
                    media_type="text/plain; charset=utf-8",
                )
            except ArtifactStoreError as error:
                self._artifact_error = _ArtifactFailure(error.code, str(error))

    def consume(self, stream: BinaryIO) -> None:
        try:
            try:
                while chunk := stream.read(8192):
                    self._digest.update(chunk)
                    if self._secret_stream is not None:
                        chunk = self._secret_stream.feed(chunk)
                    self._consume_chunk(chunk)
                if self._secret_stream is not None:
                    self._consume_chunk(self._secret_stream.finish())
            except (OSError, ValueError) as error:
                self.error = f"{type(error).__name__}: {error}"
        finally:
            stream.close()
            if self._artifact_writer is not None:
                published = self._artifact_writer.finalize(
                    redacted=(
                        self._secret_stream.redacted
                        if self._secret_stream is not None
                        else False
                    ),
                    truncated=False,
                )
                self._artifact = published.ref
                if published.error_code is not None and self._artifact_error is None:
                    try:
                        code = ArtifactErrorCode(published.error_code)
                    except ValueError:
                        code = ArtifactErrorCode.WRITE_FAILED
                    self._artifact_error = _ArtifactFailure(code, published.message)

    def result(self) -> _CaptureResult:
        bounded = (
            self._head + _TRUNCATION_MARKER + self._tail
            if self._truncated
            else bytes(self._buffer)
        )
        return _CaptureResult(
            preview=bounded.decode("utf-8", errors="replace"),
            truncated=self._truncated,
            digest=self._digest.hexdigest(),
            artifact=self._artifact,
            artifact_error=self._artifact_error,
        )

    def abort(self) -> None:
        if self._artifact_writer is not None and self._artifact is None:
            published = self._artifact_writer.abort()
            if published.error_code is not None and self._artifact_error is None:
                try:
                    code = ArtifactErrorCode(published.error_code)
                except ValueError:
                    code = ArtifactErrorCode.CLEANUP_FAILED
                self._artifact_error = _ArtifactFailure(code, published.message)

    def _consume_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._append(chunk)
        if self._artifact_writer is not None:
            self._artifact_writer.write(chunk)

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
