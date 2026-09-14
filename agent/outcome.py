"""Top-level workflow outcome independent from diagnostic check status."""

from enum import Enum


class WorkflowOutcome(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_CHANGES = "no_changes"
    UNVERIFIED = "unverified"
