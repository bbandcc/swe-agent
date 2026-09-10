"""Shared plan contracts passed from Architect to Developer."""

from enum import Enum

from pydantic import BaseModel, Field


class PlanStatus(str, Enum):
    READY = "ready"
    NO_CHANGES = "no_changes"


class AtomicTask(BaseModel):
    atomic_task: str = Field(
        description=(
            "One concrete edit step within the implementation task's file"
        )
    )
    additional_context: str = Field(
        "",
        description="Research context needed to complete this atomic task",
    )


class ImplementationTask(BaseModel):
    file_path: str = Field(description="File affected by this implementation task")
    logical_task: str = Field(
        description="Behavior the file-level task must achieve"
    )
    atomic_tasks: list[AtomicTask] = Field(
        description="Ordered edit proposals for this file"
    )


class ImplementationPlan(BaseModel):
    status: PlanStatus = Field(
        PlanStatus.READY,
        description="Whether the plan contains work or explicitly has no changes",
    )
    no_change_reason: str = Field(
        "", description="Evidence-backed reason for an explicit no-change outcome"
    )
    tasks: list[ImplementationTask] = Field(
        description="Ordered file-level tasks needed to implement the plan"
    )
