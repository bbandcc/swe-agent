"""Public S3 runtime configuration, identity, and durable-run contracts."""

from typing import Any

from agent.runtime.config import (
    DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
    ModelRetryPolicy,
    RunConfig,
    RunConfigError,
    RunConfigErrorCode,
    TokenPricing,
    load_run_config,
)
from agent.runtime.semantics import semantic_config_digest
from agent.runtime.identity import (
    CheckpointLookup,
    PreflightErrorCode,
    PreflightResult,
    PreflightStatus,
    PreflightWarning,
    PreflightWarningCode,
    ResumeRequest,
    RunCheckpoint,
    RunIdentity,
    StartRequest,
    WorkspaceIdentity,
    preflight_resume,
    preflight_start,
)
from agent.runtime.revision import (
    AgentCodeRevision,
    AgentRevisionReason,
    AgentRevisionStatus,
    detect_agent_code_revision,
)
from agent.runtime.budget import (
    BudgetController,
    BudgetDecision,
    BudgetErrorCode,
    BudgetSnapshot,
    CallKind,
    CallReservation,
    CallStatus,
    RequestIdentityScope,
    UsageRecord,
    UsageStatus,
)
from agent.runtime.calls import (
    ModelCallResult,
    UsageMeasurement,
    capture_model_exception,
    capture_model_result,
    capture_model_failure,
    classify_model_exception,
    measure_usage,
)
from agent.runtime.boundary import (
    DurableBudgetBoundary,
    DurableBudgetState,
    DurableCallResult,
)

__all__ = [
    "RunConfig",
    "DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS",
    "ModelRetryPolicy",
    "RunConfigError",
    "RunConfigErrorCode",
    "TokenPricing",
    "load_run_config",
    "semantic_config_digest",
    "AgentCodeRevision",
    "AgentRevisionReason",
    "AgentRevisionStatus",
    "CheckpointLookup",
    "PreflightErrorCode",
    "PreflightResult",
    "PreflightStatus",
    "PreflightWarning",
    "PreflightWarningCode",
    "ResumeRequest",
    "RunCheckpoint",
    "RunIdentity",
    "StartRequest",
    "WorkspaceIdentity",
    "detect_agent_code_revision",
    "preflight_resume",
    "preflight_start",
    "BudgetController",
    "BudgetDecision",
    "BudgetErrorCode",
    "BudgetSnapshot",
    "CallKind",
    "CallReservation",
    "CallStatus",
    "RequestIdentityScope",
    "UsageRecord",
    "UsageStatus",
    "ModelCallResult",
    "UsageMeasurement",
    "capture_model_exception",
    "capture_model_result",
    "capture_model_failure",
    "classify_model_exception",
    "measure_usage",
    "DurableBudgetBoundary",
    "DurableBudgetState",
    "DurableCallResult",
    "DurableRunResult",
    "DurableRunStatus",
    "RunSummary",
    "run_exit_code",
    "GraphCheckpointLookup",
    "create_durable_workflow",
    "durable_recursion_limit",
    "resume_run",
    "start_run",
]


def __getattr__(name: str) -> Any:
    """Load the durable runtime lazily to keep graph module imports acyclic."""
    if name in {
        "DurableRunResult",
        "DurableRunStatus",
        "RunSummary",
        "run_exit_code",
        "GraphCheckpointLookup",
        "create_durable_workflow",
        "durable_recursion_limit",
        "resume_run",
        "start_run",
    }:
        from agent.runtime import durable

        return getattr(durable, name)
    raise AttributeError(name)
