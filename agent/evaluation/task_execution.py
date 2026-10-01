"""One-task checkout, sandbox execution, patch collection, and cleanup seam."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from agent.evaluation.frozen_environment import (
    FrozenEnvironmentCleanupError,
    FrozenEnvironmentError,
    stage_frozen_python_312,
)
from agent.evaluation.python_toolchain import PythonToolchainError, stage_python_312
from agent.evaluation.task_workspace import (
    PatchCollection,
    TaskWorkspaceErrorCode,
    TaskWorkspaceFailure,
    prepare_task_workspace,
)
from agent.evaluation.windows_sandbox import (
    SandboxCommand,
    SandboxErrorCode,
    SandboxResult,
    SandboxStatus,
    run_in_windows_appcontainer,
)
from agent.workspace.policy import WorkspaceAccessPolicy


class TaskExecutionStatus(str, Enum):
    COMMAND_COMPLETED = "command_completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class TaskExecutionErrorCode(str, Enum):
    WORKSPACE_PREPARATION_FAILED = "workspace_preparation_failed"
    SANDBOX_FAILED = "sandbox_failed"
    PATCH_COLLECTION_FAILED = "patch_collection_failed"
    TIMEOUT = "timeout"
    CLEANUP_FAILED = "cleanup_failed"
    TOOLCHAIN_INVALID = "toolchain_invalid"
    FROZEN_ENVIRONMENT_INVALID = "frozen_environment_invalid"


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    status: TaskExecutionStatus
    error_code: TaskExecutionErrorCode | None
    task_id: str | None
    revision: str | None
    sandbox_result: SandboxResult | None = None
    patch: PatchCollection | None = None
    workspace_failure: TaskWorkspaceFailure | None = None
    cleanup_complete: bool = False


def execute_isolated_task(
    *,
    repository_root: str | Path,
    task: Mapping[str, Any],
    runtime_root: str | Path,
    access_policy: WorkspaceAccessPolicy,
    argv: tuple[str, ...],
    cwd: str = ".",
    timeout_seconds: float = 30.0,
    memory_limit_bytes: int = 512 * 1024 * 1024,
    cpu_rate_percent: int = 50,
    cpu_time_seconds: float = 300.0,
    output_limit_bytes: int = 64 * 1024,
    python_toolchain_root: str | Path | None = None,
    uv_executable: str | Path | None = None,
) -> TaskExecutionResult:
    """Execute one trusted argv in a pinned disposable workspace.

    Preparation failure prevents command dispatch. ``COMMAND_COMPLETED`` means
    only that the trusted command exited successfully; task acceptance remains
    a separate evaluator decision. A patch is available only after a successful
    sandbox result and successful workspace cleanup.
    """
    prepared = prepare_task_workspace(
        repository_root=repository_root,
        task=task,
        runtime_root=runtime_root,
        access_policy=access_policy,
    )
    task_id = task.get("task_id") if isinstance(task, Mapping) else None
    target = task.get("target") if isinstance(task, Mapping) else None
    revision = target.get("revision") if isinstance(target, Mapping) else None
    if not prepared.ready:
        code = (
            TaskExecutionErrorCode.CLEANUP_FAILED
            if prepared.failure is not None
            and prepared.failure.code is TaskWorkspaceErrorCode.CLEANUP_FAILED
            else TaskExecutionErrorCode.WORKSPACE_PREPARATION_FAILED
        )
        return TaskExecutionResult(
            TaskExecutionStatus.FAILED,
            code,
            task_id if isinstance(task_id, str) else None,
            revision if isinstance(revision, str) else None,
            workspace_failure=prepared.failure,
            cleanup_complete=code is not TaskExecutionErrorCode.CLEANUP_FAILED,
        )

    workspace = prepared.workspace
    sandbox_result: SandboxResult | None = None
    patch: PatchCollection | None = None
    try:
        toolchain_root = None
        if isinstance(task, Mapping) and "environment" in task:
            if (
                not argv
                or argv[0] != "python"
                or python_toolchain_root is None
                or uv_executable is None
            ):
                raise FrozenEnvironmentError(
                    "Frozen task execution requires explicit Python and uv toolchains."
                )
            executable = stage_frozen_python_312(
                task=task,
                workspace_root=workspace.root,
                workspace_revision=workspace.revision,
                repository_root=repository_root,
                runtime_root=runtime_root,
                python_toolchain_root=python_toolchain_root,
                uv_executable=uv_executable,
                destination=workspace.root.parent / "toolchain-python312",
            )
            toolchain_root = executable.parent
            command_argv = (str(executable), "-B", "-S", *argv[1:])
        elif python_toolchain_root is not None:
            if not argv or argv[0] != "python":
                raise PythonToolchainError("Python toolchain requires the literal python command selector.")
            try:
                source = Path(python_toolchain_root).resolve(strict=True)
                repository = Path(repository_root).resolve(strict=True)
                runtime = Path(runtime_root).resolve(strict=True)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise PythonToolchainError("Trusted Python toolchain path is invalid.") from error
            if any(source == root or source.is_relative_to(root) for root in (repository, runtime)):
                raise PythonToolchainError("Trusted Python cannot originate inside task or runtime storage.")
            executable = stage_python_312(
                python_toolchain_root, workspace.root.parent / "toolchain-python312"
            )
            toolchain_root = executable.parent
            command_argv = (str(executable), "-B", "-S", *argv[1:])
        else:
            command_argv = argv
        sandbox_result = run_in_windows_appcontainer(
            SandboxCommand(
                workspace_root=workspace.root,
                argv=command_argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                memory_limit_bytes=memory_limit_bytes,
                cpu_rate_percent=cpu_rate_percent,
                cpu_time_seconds=cpu_time_seconds,
                output_limit_bytes=output_limit_bytes,
                python_toolchain_root=toolchain_root,
            )
        )
        if sandbox_result.status is SandboxStatus.COMPLETED:
            patch = workspace.collect_patch()
        cleanup_failure = workspace.cleanup()
    except PythonToolchainError as error:
        cleanup_failure = workspace.cleanup()
        frozen_cleanup_failed = isinstance(error, FrozenEnvironmentCleanupError)
        error_code = (
            TaskExecutionErrorCode.FROZEN_ENVIRONMENT_INVALID
            if isinstance(error, FrozenEnvironmentError)
            else TaskExecutionErrorCode.TOOLCHAIN_INVALID
        )
        if frozen_cleanup_failed and cleanup_failure is None:
            cleanup_failure = TaskWorkspaceFailure(
                TaskWorkspaceErrorCode.CLEANUP_FAILED,
                "Frozen Python environment cleanup failed.",
            )
        return TaskExecutionResult(
            TaskExecutionStatus.FAILED,
            TaskExecutionErrorCode.CLEANUP_FAILED
            if cleanup_failure or frozen_cleanup_failed
            else error_code,
            workspace.task_id,
            workspace.revision,
            workspace_failure=cleanup_failure,
            cleanup_complete=cleanup_failure is None and not frozen_cleanup_failed,
        )
    except BaseException:
        workspace.cleanup()
        raise

    if cleanup_failure is not None or not sandbox_result.cleanup_complete:
        return TaskExecutionResult(
            TaskExecutionStatus.FAILED,
            TaskExecutionErrorCode.CLEANUP_FAILED,
            workspace.task_id,
            workspace.revision,
            sandbox_result=sandbox_result,
            workspace_failure=cleanup_failure,
            cleanup_complete=False,
        )
    if sandbox_result.status is SandboxStatus.TIMED_OUT:
        return TaskExecutionResult(
            TaskExecutionStatus.TIMED_OUT,
            TaskExecutionErrorCode.TIMEOUT,
            workspace.task_id,
            workspace.revision,
            sandbox_result=sandbox_result,
            cleanup_complete=True,
        )
    if sandbox_result.status is not SandboxStatus.COMPLETED:
        return TaskExecutionResult(
            TaskExecutionStatus.FAILED,
            TaskExecutionErrorCode.SANDBOX_FAILED,
            workspace.task_id,
            workspace.revision,
            sandbox_result=sandbox_result,
            cleanup_complete=True,
        )
    if patch is None or not patch.valid:
        return TaskExecutionResult(
            TaskExecutionStatus.FAILED,
            TaskExecutionErrorCode.PATCH_COLLECTION_FAILED,
            workspace.task_id,
            workspace.revision,
            sandbox_result=sandbox_result,
            patch=patch,
            cleanup_complete=True,
        )
    return TaskExecutionResult(
        TaskExecutionStatus.COMMAND_COMPLETED,
        None,
        workspace.task_id,
        workspace.revision,
        sandbox_result=sandbox_result,
        patch=patch,
        cleanup_complete=True,
    )
