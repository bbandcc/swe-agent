import tempfile
import unittest
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.constants import END, START
from langgraph.graph import StateGraph, add_messages
from langgraph.prebuilt import ToolNode

from agent.architect.graph import (
    ArchitectRuntime,
    ResearchEvaluation,
    ResearchStep,
    create_architect_workflow,
)
from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import EditStatus, WorkspaceEditor
from agent.graph import create_workflow_graph


class ToolLoopState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


@tool
def echo_value(value: str) -> str:
    """Return a value for the compiled ToolNode integration test."""
    return f"tool:{value}"


class GraphIntegrationTests(unittest.TestCase):
    def test_fake_model_children_propagate_final_parent_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="workspace_repo/app.py",
                        logical_task="更新值",
                        atomic_tasks=[AtomicTask(atomic_task="改为 2")],
                    )
                ]
            )
            architect = create_architect_workflow(
                ArchitectRuntime(
                    plan_next_step=lambda _: ResearchStep(
                        reasoning="inspect", hypothesis="find target"
                    ),
                    check_research_step=lambda _: ResearchEvaluation(
                        reasoning="useful", is_valid=True
                    ),
                    conduct_research=lambda _: AIMessage(content="ready"),
                    extract_implementation_plan=lambda _: plan,
                    load_codebase_structure=lambda: "app.py",
                ),
                research_tools=[],
            )
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            developer = create_developer_workflow(
                DeveloperRuntime(
                    edit_executor=lambda: executor,
                    load_codebase_structure=lambda: "app.py",
                    research_atomic_task=lambda _: AIMessage(content="ready"),
                    propose_existing_file_edit=lambda _: (
                        "<<<<<<< SEARCH\nvalue = 1\n=======\n"
                        "value = 2\n>>>>>>> REPLACE"
                    ),
                    propose_new_file=lambda _: self.fail("new-file model was called"),
                ),
                research_tools=[],
            )

            result = create_workflow_graph(
                architect=architect, developer=developer
            ).compile().invoke(
                {
                    "implementation_research_scratchpad": [
                        HumanMessage(content="update app")
                    ]
                }
            )

            self.assertEqual(result["implementation_plan"], plan)
            self.assertEqual(result["developer_status"], DeveloperStatus.COMPLETED)
            self.assertIsNone(result["developer_error_code"])
            self.assertEqual(result["developer_message"], "Implementation completed.")
            self.assertEqual(result["last_edit_result"].status, EditStatus.APPLIED)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_fake_model_children_propagate_invalid_plan_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            task = ImplementationTask(
                file_path="workspace_repo/app.py",
                logical_task="更新值",
                atomic_tasks=[AtomicTask(atomic_task="改为 2")],
            )
            plan = ImplementationPlan(
                tasks=[
                    task,
                    task.model_copy(update={"file_path": "./workspace_repo/app.py"}),
                ]
            )
            architect = create_architect_workflow(
                ArchitectRuntime(
                    plan_next_step=lambda _: ResearchStep(
                        reasoning="inspect", hypothesis="find target"
                    ),
                    check_research_step=lambda _: ResearchEvaluation(
                        reasoning="useful", is_valid=True
                    ),
                    conduct_research=lambda _: AIMessage(content="ready"),
                    extract_implementation_plan=lambda _: plan,
                    load_codebase_structure=lambda: "app.py",
                ),
                research_tools=[],
            )
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            developer = create_developer_workflow(
                DeveloperRuntime(
                    edit_executor=lambda: executor,
                    load_codebase_structure=lambda: self.fail("workspace was scanned"),
                    research_atomic_task=lambda _: self.fail("model was called"),
                    propose_existing_file_edit=lambda _: self.fail("model was called"),
                    propose_new_file=lambda _: self.fail("model was called"),
                ),
                research_tools=[],
            )

            result = create_workflow_graph(
                architect=architect, developer=developer
            ).compile().invoke(
                {
                    "implementation_research_scratchpad": [
                        HumanMessage(content="update app")
                    ]
                }
            )

            self.assertEqual(result["implementation_plan"], plan)
            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["developer_error_code"], DeveloperErrorCode.INVALID_PLAN
            )
            self.assertIn("duplicate", result["developer_message"].lower())
            self.assertIsNone(result["last_edit_result"])
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")

    def test_compiled_tool_node_returns_tool_message_to_model(self) -> None:
        observed_tool_message: list[ToolMessage] = []

        def fake_model(state: ToolLoopState) -> dict[str, list[AIMessage]]:
            last = state["messages"][-1]
            if isinstance(last, ToolMessage):
                observed_tool_message.append(last)
                return {"messages": [AIMessage(content=f"done:{last.content}")]}
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "echo_value",
                                "args": {"value": "42"},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }

        def route(state: ToolLoopState) -> str:
            return "tools" if state["messages"][-1].tool_calls else END

        workflow = StateGraph(ToolLoopState)
        workflow.add_node("model", fake_model)
        workflow.add_node("tools", ToolNode([echo_value]))
        workflow.add_edge(START, "model")
        workflow.add_conditional_edges("model", route, {"tools": "tools", END: END})
        workflow.add_edge("tools", "model")

        result = workflow.compile().invoke(
            {"messages": [HumanMessage(content="call the tool")]}
        )

        self.assertEqual(len(observed_tool_message), 1)
        self.assertEqual(observed_tool_message[0].tool_call_id, "call-1")
        self.assertEqual(observed_tool_message[0].content, "tool:42")
        self.assertEqual(result["messages"][-1].content, "done:tool:42")
