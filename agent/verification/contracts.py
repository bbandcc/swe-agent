"""Public contracts for deterministic repository verification."""

import hashlib
import json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path

from agent.artifacts import ArtifactRef
from agent.runtime.revision import WorkspaceRevision


class VerificationCheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"


class VerificationCaseStatus(str, Enum):
    """Stable status values emitted by the supported JUnit report format."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"


REPORT_SCHEMA = "junit-xml-v1"
_PYTEST_JUNITXML_OPTIONS = ("--junitxml", "--junit-xml")


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    IMPROVED = "improved"
    REGRESSION = "regression"
    PRE_EXISTING_FAILURE = "pre_existing_failure"
    UNVERIFIED = "unverified"
    VERIFICATION_ERROR = "verification_error"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    REPAIR_EXHAUSTED = "repair_exhausted"


class VerificationRecoveryPolicy(str, Enum):
    """Trusted policy for an interrupted verification command.

    The current runtime only supports stopping when a command has started but
    its result was not checkpointed.  A rerun policy remains an explicit
    value so a future isolated execution seam cannot be confused with the
    conservative default.
    """

    STOP_ON_UNKNOWN = "stop_on_unknown"
    RERUN_ISOLATED = "rerun_isolated"


class RepairScopePolicy(str, Enum):
    """Trusted boundary for files eligible in a regression repair."""

    LAST_FILE = "last_file"
    COMMITTED_PLAN_FILES = "committed_plan_files"


@dataclass(frozen=True, slots=True)
class VerificationCase:
    """One case from the trusted structured verification report."""

    check_id: str
    case_id: str
    status: VerificationCaseStatus
    report_schema: str = REPORT_SCHEMA

    def __post_init__(self) -> None:
        for field_name in ("check_id", "case_id", "report_schema"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Verification case {field_name} must be non-empty.")
        if self.report_schema != REPORT_SCHEMA:
            raise ValueError("Unsupported verification report schema.")
        if not isinstance(self.status, VerificationCaseStatus):
            try:
                object.__setattr__(
                    self, "status", VerificationCaseStatus(self.status)
                )
            except (TypeError, ValueError) as error:
                raise ValueError("Verification case status is invalid.") from error


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Normalized, identity-safe evidence from one configured check."""

    check_id: str
    report_schema: str
    cases: tuple[VerificationCase, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.check_id, str) or not self.check_id.strip():
            raise ValueError("Verification report check_id must be non-empty.")
        if self.report_schema != REPORT_SCHEMA:
            raise ValueError("Unsupported verification report schema.")
        cases = _sequence_tuple(self.cases, "cases")
        if not cases:
            raise ValueError("Verification report must contain at least one case.")
        seen: set[str] = set()
        for case in cases:
            if not isinstance(case, VerificationCase):
                raise ValueError("Verification report cases must be VerificationCase values.")
            if case.check_id != self.check_id:
                raise ValueError("Verification case check_id does not match report.")
            if case.case_id in seen:
                raise ValueError("Verification report contains duplicate case_id values.")
            seen.add(case.case_id)
        object.__setattr__(self, "cases", cases)

    @property
    def case_map(self) -> dict[str, VerificationCase]:
        return {case.case_id: case for case in self.cases}


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    """One configured argv check executed relative to the workspace."""

    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 60.0
    max_output_bytes: int = 20_000
    report_path: str | None = None
    allowed_failure_case_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        timeout = self.timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError(
                "Verification timeout must be a finite positive number."
            )
        if self.report_path is not None and (
            not isinstance(self.report_path, str)
            or not self.report_path.strip()
            or "\x00" in self.report_path
        ):
            raise ValueError("Verification report_path must be a valid path.")
        object.__setattr__(
            self,
            "allowed_failure_case_ids",
            _string_tuple(self.allowed_failure_case_ids, "allowed_failure_case_ids"),
        )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Bounded evidence returned by one configured verification check."""

    name: str
    argv: tuple[str, ...]
    cwd: str
    status: VerificationCheckStatus
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    failure_id: str | None = None
    message: str = ""
    report: VerificationReport | None = None
    allowed_failure_case_ids: tuple[str, ...] = ()
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    stdout_artifact: ArtifactRef | None = None
    stderr_artifact: ArtifactRef | None = None
    artifact_error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _sequence_tuple(self.argv, "argv"))
        object.__setattr__(
            self,
            "allowed_failure_case_ids",
            _string_tuple(self.allowed_failure_case_ids, "allowed_failure_case_ids"),
        )
        if self.report is not None:
            if not isinstance(self.report, VerificationReport):
                raise ValueError("report must be VerificationReport or None.")
            if self.report.check_id != self.name:
                raise ValueError("Verification report check_id must match result name.")
        for name in ("stdout_artifact", "stderr_artifact"):
            artifact = getattr(self, name)
            if artifact is not None and not isinstance(artifact, ArtifactRef):
                raise ValueError(f"{name} must be ArtifactRef or None.")
        for name in ("stdout_digest", "stderr_digest"):
            digest = getattr(self, name)
            if digest is not None and (
                not isinstance(digest, str) or not digest.strip()
            ):
                raise ValueError(f"{name} must be a string or None.")

    @property
    def artifact_refs(self) -> tuple[ArtifactRef, ...]:
        return tuple(
            reference
            for reference in (self.stdout_artifact, self.stderr_artifact)
            if reference is not None
        )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        argv: tuple[str, ...],
        cwd: str,
        status: VerificationCheckStatus,
        exit_code: int | None,
        stdout: str = "",
        stderr: str = "",
        duration_seconds: float = 0.0,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        stdout_digest: str | None = None,
        stderr_digest: str | None = None,
        message: str = "",
        report: VerificationReport | None = None,
        allowed_failure_case_ids: tuple[str, ...] = (),
        stdout_artifact: ArtifactRef | None = None,
        stderr_artifact: ArtifactRef | None = None,
        artifact_error_code: str | None = None,
    ) -> "VerificationResult":
        failure_id = None
        if status is not VerificationCheckStatus.PASS:
            evidence = json.dumps(
                {
                    "argv": argv,
                    "cwd": cwd,
                    "exit_code": exit_code,
                    "name": name,
                    "status": status.value,
                    "stderr_digest": stderr_digest
                    or hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
                    "stdout_digest": stdout_digest
                    or hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            failure_id = hashlib.sha256(evidence).hexdigest()
        return cls(
            name=name,
            argv=argv,
            cwd=cwd,
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            failure_id=failure_id,
            message=message,
            report=report,
            allowed_failure_case_ids=allowed_failure_case_ids,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            stdout_artifact=stdout_artifact,
            stderr_artifact=stderr_artifact,
            artifact_error_code=artifact_error_code,
        )


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    """Checkpoint-safe verification evidence with bounded diagnostics only."""

    name: str
    argv: tuple[str, ...]
    cwd: str
    status: VerificationCheckStatus
    exit_code: int | None
    stdout_preview: str = ""
    stderr_preview: str = ""
    duration_seconds: float = 0.0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    failure_id: str | None = None
    message: str = ""
    report: VerificationReport | None = None
    allowed_failure_case_ids: tuple[str, ...] = ()
    stdout_artifact: ArtifactRef | None = None
    stderr_artifact: ArtifactRef | None = None
    artifact_error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _sequence_tuple(self.argv, "argv"))
        object.__setattr__(
            self,
            "allowed_failure_case_ids",
            _string_tuple(self.allowed_failure_case_ids, "allowed_failure_case_ids"),
        )
        if not isinstance(self.status, VerificationCheckStatus):
            try:
                object.__setattr__(
                    self, "status", VerificationCheckStatus(self.status)
                )
            except (TypeError, ValueError) as error:
                raise ValueError("Verification summary status is invalid.") from error
        if self.report is not None:
            if not isinstance(self.report, VerificationReport):
                raise ValueError("report must be VerificationReport or None.")
            if self.report.check_id != self.name:
                raise ValueError("Verification report check_id must match result name.")
        for name in ("stdout_artifact", "stderr_artifact"):
            artifact = getattr(self, name)
            if artifact is not None and not isinstance(artifact, ArtifactRef):
                raise ValueError(f"{name} must be ArtifactRef or None.")

    @property
    def stdout(self) -> str:
        """Compatibility view used by the existing repair feedback renderer."""
        return self.stdout_preview

    @property
    def stderr(self) -> str:
        return self.stderr_preview

    @property
    def artifact_refs(self) -> tuple[ArtifactRef, ...]:
        """Return verification artifact references without exposing contents."""
        return tuple(
            reference
            for reference in (self.stdout_artifact, self.stderr_artifact)
            if reference is not None
        )

    @classmethod
    def from_result(cls, result: VerificationResult) -> "VerificationSummary":
        return cls(
            name=result.name,
            argv=result.argv,
            cwd=result.cwd,
            status=result.status,
            exit_code=result.exit_code,
            stdout_preview=result.stdout,
            stderr_preview=result.stderr,
            duration_seconds=result.duration_seconds,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            stdout_digest=result.stdout_digest,
            stderr_digest=result.stderr_digest,
            failure_id=result.failure_id,
            message=result.message,
            report=result.report,
            allowed_failure_case_ids=result.allowed_failure_case_ids,
            stdout_artifact=result.stdout_artifact,
            stderr_artifact=result.stderr_artifact,
            artifact_error_code=result.artifact_error_code,
        )


VERIFICATION_ATTEMPT_SCHEMA_VERSION = 1


class VerificationAttemptStatus(str, Enum):
    STARTED = "started"
    RESULT_RECORDED = "result_recorded"


def verification_spec_digest(spec: VerificationSpec) -> str:
    """Return the trusted, secret-free identity of one configured check."""
    payload = {
        "name": spec.name,
        "argv": list(spec.argv),
        "cwd": spec.cwd,
        "timeout_seconds": _canonical_number_text(spec.timeout_seconds),
        "max_output_bytes": spec.max_output_bytes,
        "report_path": spec.report_path,
        "allowed_failure_case_ids": list(spec.allowed_failure_case_ids),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_number_text(value: int | float) -> str:
    normalized = Decimal(str(value)).normalize()
    return "0" if normalized == 0 else format(normalized, "f")


@dataclass(frozen=True, slots=True)
class VerificationAttempt:
    """Checkpoint-safe boundary around one externally executed check."""

    schema_version: int
    attempt_id: str
    run_id: str
    task_id: str
    phase: str
    repair_attempt: int
    spec_index: int
    spec_name: str
    attempt_ordinal: int
    spec_digest: str
    workspace_revision: WorkspaceRevision
    status: VerificationAttemptStatus
    result: VerificationSummary | None = None

    def __post_init__(self) -> None:
        if self.schema_version != VERIFICATION_ATTEMPT_SCHEMA_VERSION:
            raise ValueError("Unsupported verification attempt schema version.")
        for name in ("attempt_id", "run_id", "task_id", "phase", "spec_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Verification attempt {name} must be non-empty.")
        if self.phase not in {"baseline", "post"}:
            raise ValueError("Verification attempt phase is invalid.")
        for name in ("repair_attempt", "spec_index", "attempt_ordinal"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Verification attempt {name} is invalid.")
        if (
            not isinstance(self.spec_digest, str)
            or len(self.spec_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.spec_digest)
        ):
            raise ValueError("Verification attempt spec_digest is invalid.")
        if not isinstance(self.workspace_revision, WorkspaceRevision):
            raise ValueError("Verification attempt workspace revision is invalid.")
        if not isinstance(self.status, VerificationAttemptStatus):
            try:
                object.__setattr__(self, "status", VerificationAttemptStatus(self.status))
            except (TypeError, ValueError) as error:
                raise ValueError("Verification attempt status is invalid.") from error
        if self.status is VerificationAttemptStatus.STARTED and self.result is not None:
            raise ValueError("Started verification attempts cannot have a result.")
        if self.status is VerificationAttemptStatus.RESULT_RECORDED and not isinstance(
            self.result, VerificationSummary
        ):
            raise ValueError("Recorded verification attempts require a result.")
        if self.result is not None and self.result.name != self.spec_name:
            raise ValueError("Verification attempt result does not match spec.")
        if self.attempt_id != self._stable_attempt_id():
            raise ValueError("Verification attempt identity is invalid.")

    @classmethod
    def started(
        cls,
        *,
        run_id: str,
        task_id: str,
        phase: str,
        repair_attempt: int,
        spec_index: int,
        spec: VerificationSpec,
        attempt_ordinal: int,
        workspace_revision: WorkspaceRevision,
    ) -> "VerificationAttempt":
        spec_digest = verification_spec_digest(spec)
        seed = {
            "schema_version": VERIFICATION_ATTEMPT_SCHEMA_VERSION,
            "run_id": run_id,
            "task_id": task_id,
            "phase": phase,
            "repair_attempt": repair_attempt,
            "spec_index": spec_index,
            "spec_name": spec.name,
            "attempt_ordinal": attempt_ordinal,
            "spec_digest": spec_digest,
        }
        attempt_id = hashlib.sha256(
            json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return cls(
            schema_version=VERIFICATION_ATTEMPT_SCHEMA_VERSION,
            attempt_id=attempt_id,
            run_id=run_id,
            task_id=task_id,
            phase=phase,
            repair_attempt=repair_attempt,
            spec_index=spec_index,
            spec_name=spec.name,
            attempt_ordinal=attempt_ordinal,
            spec_digest=spec_digest,
            workspace_revision=workspace_revision,
            status=VerificationAttemptStatus.STARTED,
        )

    def with_result(self, result: VerificationSummary) -> "VerificationAttempt":
        return VerificationAttempt(
            schema_version=self.schema_version,
            attempt_id=self.attempt_id,
            run_id=self.run_id,
            task_id=self.task_id,
            phase=self.phase,
            repair_attempt=self.repair_attempt,
            spec_index=self.spec_index,
            spec_name=self.spec_name,
            attempt_ordinal=self.attempt_ordinal,
            spec_digest=self.spec_digest,
            workspace_revision=self.workspace_revision,
            status=VerificationAttemptStatus.RESULT_RECORDED,
            result=result,
        )

    def _stable_attempt_id(self) -> str:
        seed = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "phase": self.phase,
            "repair_attempt": self.repair_attempt,
            "spec_index": self.spec_index,
            "spec_name": self.spec_name,
            "attempt_ordinal": self.attempt_ordinal,
            "spec_digest": self.spec_digest,
        }
        return hashlib.sha256(
            json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()


def is_pytest_command(argv: Sequence[str]) -> bool:
    """Recognize only direct pytest or ``python -m pytest`` producers."""
    if not argv:
        return False
    executable = Path(str(argv[0])).name.lower()
    if executable in {"pytest", "pytest.exe", "py.test", "py.test.exe"}:
        return True
    return (
        len(argv) >= 3
        and str(argv[1]) == "-m"
        and str(argv[2]).lower() == "pytest"
    )


def is_pytest_junitxml_producer(
    argv: Sequence[str], report_path: str
) -> bool:
    """Require an explicit pytest ``--junitxml`` output matching the spec."""
    if not is_pytest_command(argv):
        return False
    configured = _report_path_key(report_path)
    if not configured:
        return False
    return any(
        _report_path_key(output) == configured
        for output in _pytest_junitxml_outputs(argv)
    )


def has_pytest_junitxml_option(argv: Sequence[str]) -> bool:
    """Return whether argv explicitly requests a pytest JUnit XML output."""
    return is_pytest_command(argv) and bool(_pytest_junitxml_outputs(argv))


def _pytest_junitxml_outputs(argv: Sequence[str]) -> tuple[str, ...]:
    outputs: list[str] = []
    index = 0
    while index < len(argv):
        item = str(argv[index])
        for option in _PYTEST_JUNITXML_OPTIONS:
            prefix = f"{option}="
            if item.startswith(prefix):
                outputs.append(item[len(prefix) :])
                break
            if item == option and index + 1 < len(argv):
                outputs.append(str(argv[index + 1]))
                index += 1
                break
        index += 1
    return tuple(outputs)


def _report_path_key(value: str) -> str:
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.casefold().startswith("workspace_repo/"):
        text = text[len("workspace_repo/") :]
    return os.path.normcase(text)


def _sequence_tuple(value: object, name: str) -> tuple:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise ValueError(f"{name} must be a non-string sequence.")
    return tuple(value)


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    values = _sequence_tuple(value, name)
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError(f"{name} must contain non-empty strings.")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicate values.")
    return tuple(values)
