from enum import Enum
from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from langgraph.graph.message import Messages
from pydantic import BaseModel, Field

from agent.common.entities import ImplementationPlan
from agent.editing import EditResult, WorkspaceSnapshot, WorkspaceTransaction


class DeveloperStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    NO_CHANGES = "no_changes"
    FAILED = "failed"


class DeveloperErrorCode(str, Enum):
    INVALID_PLAN = "invalid_plan"
    INVALID_STATE = "invalid_state"


def add_messages_with_clear(left: Messages, right: Messages) -> Messages:
    if right is None or not right:
        return []
    return add_messages(left, right)


class SoftwareDeveloperState(BaseModel):
    implementation_plan: ImplementationPlan | None = Field(
        None, description="The implementation plan to be executed"
    )
    current_task_idx: int = Field(0, description="Current logical task index")
    current_atomic_task_idx: int = Field(0, description="Current atomic task index")
    atomic_implementation_research: Annotated[
        list[AnyMessage], add_messages_with_clear
    ] = Field(default_factory=list)
    codebase_structure: str | None = Field(
        None, description="The codebase structure"
    )
    current_file_snapshot: WorkspaceSnapshot | None = Field(
        None, description="Validated workspace file state used to build an edit"
    )
    current_file_transaction: WorkspaceTransaction | None = Field(
        None, description="In-memory transaction for the current file"
    )
    current_file_content: str | None = Field(
        None, description="Current UTF-8 content supplied to the model"
    )
    last_edit_result: EditResult | None = Field(
        None, description="Structured outcome of the most recent edit attempt"
    )
    developer_status: DeveloperStatus = Field(DeveloperStatus.PENDING)
    developer_error_code: DeveloperErrorCode | None = Field(None)
    developer_message: str = Field("")
