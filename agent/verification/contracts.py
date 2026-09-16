"""Public contracts for deterministic repository verification."""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum


class VerificationCheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    IMPROVED = "improved"
    REGRESSION = "regression"
    PRE_EXISTING_FAILURE = "pre_existing_failure"
    UNVERIFIED = "unverified"
    VERIFICATION_ERROR = "verification_error"
    REPAIR_EXHAUSTED = "repair_exhausted"


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    """One configured argv check executed relative to the workspace."""

    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 60.0
    max_output_bytes: int = 20_000

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _sequence_tuple(self.argv, "argv"))

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
        )


def _sequence_tuple(value: object, name: str) -> tuple:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise ValueError(f"{name} must be a non-string sequence.")
    return tuple(value)
