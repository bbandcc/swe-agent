"""Public contracts for deterministic repository verification."""

import hashlib
import json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


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
        )


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
