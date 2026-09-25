import tempfile
import unittest
from pathlib import Path
from typing import Annotated, TypedDict
from unittest.mock import patch

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
from agent.architect.runtime import default_architect_runtime
from agent.common.entities import (
    AtomicTask,
    ImplementationPlan,
    ImplementationTask,
    PlanStatus,
)
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.developer.runtime import default_developer_runtime
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import EditStatus, WorkspaceEditor
from agent.graph import create_workflow_graph
from agent.tools.read_contract import MAX_TREE_SCAN_ENTRIES
from agent.tools.write import get_files_structure
from agent.workspace import WorkspaceAccessPolicy, workspace_root_scope


class ToolLoopState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


@tool
def echo_value(value: str) -> str:
    """Return a value for the compiled ToolNode integration test."""
    return f"tool:{value}"


@tool
def architect_search(query: str) -> str:
    """Return deterministic architect research evidence."""
    return f"architect-search:{query}"


@tool
def architect_codemap(path: str) -> dict[str, object]:
    """Return deterministic architect code map evidence."""
    return {"path": path, "hash": "architect-hash", "truncated": False}


@tool
def developer_search(query: str) -> str:
    """Return deterministic developer research evidence."""
    return f"developer-search:{query}"


@tool
def developer_codemap(path: str) -> dict[str, object]:
    """Return deterministic developer code map evidence."""
    return {"path": path, "hash": "developer-hash", "truncated": False}


class GraphIntegrationTests(unittest.TestCase):
    @staticmethod
    def _architect_tree_graph(loader, observed, access_policy):
        def plan(values):
            observed.append(values["codebase_structure"])
            return ResearchStep(reasoning="inspect", hypothesis="find target")

        runtime = ArchitectRuntime(
            plan_next_step=plan,
            check_research_step=lambda _: ResearchEvaluation(
                reasoning="useful", is_valid=True
            ),
            conduct_research=lambda _: AIMessage(content="research complete"),
            extract_implementation_plan=lambda _: ImplementationPlan(
                status=PlanStatus.NO_CHANGES,
                no_change_reason="No implementation is required.",
                tasks=[],
            ),
            load_codebase_structure=loader,
        )
        return create_architect_workflow(
            runtime, research_tools=[], access_policy=access_policy
        ).invoke(
            {"implementation_research_scratchpad": [HumanMessage(content="inspect")]}
        )

    def test_architect_model_input_preserves_truncated_production_tree_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(130):
                (root / f"module-{index:03}.py").write_text(
                    f"value_{index} = {index}\n", encoding="utf-8"
                )
            policy = WorkspaceAccessPolicy()
            loader = default_architect_runtime().load_codebase_structure
            observed: list[str] = []
            with workspace_root_scope(root, access_policy=policy):
                raw_result = get_files_structure.invoke({"directory": "."})
                self._architect_tree_graph(loader, observed, policy)

        self.assertTrue(raw_result["truncated"])
        self.assertIsInstance(raw_result["continuation"], str)
        self.assertIn("UNTRUSTED WORKSPACE EVIDENCE", observed[0])
        self.assertIn("status: INCOMPLETE (TRUNCATED)", observed[0])
        self.assertIn("range:", observed[0])
        self.assertIn("warnings:", observed[0])
        self.assertIn("limits:", observed[0])
        self.assertIn("continuation_available: true", observed[0])
        self.assertNotIn(raw_result["continuation"], observed[0])

    def test_architect_model_input_reports_tree_scan_cap_as_incomplete(self) -> None:
        class ProtectedEntry:
            name = "private"

            def is_dir(self, *, follow_symlinks: bool = True) -> bool:
                raise AssertionError("protected path metadata must not be inspected")

        class GuardedScandir:
            def __init__(self) -> None:
                self.consumed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.consumed >= MAX_TREE_SCAN_ENTRIES + 1:
                    raise AssertionError("tree traversal consumed an unbounded suffix")
                self.consumed += 1
                return ProtectedEntry()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = WorkspaceAccessPolicy(hidden_paths=("private",))
            observed: list[str] = []

            scanners: list[GuardedScandir] = []

            def create_scanner(*_args):
                scanner = GuardedScandir()
                scanners.append(scanner)
                return scanner

            with (
                workspace_root_scope(root, access_policy=policy),
                patch("agent.tools.write.os.scandir", side_effect=create_scanner),
            ):
                self._architect_tree_graph(
                    default_architect_runtime().load_codebase_structure,
                    observed,
                    policy,
                )

        self.assertTrue(scanners)
        self.assertTrue(
            all(scanner.consumed == MAX_TREE_SCAN_ENTRIES + 1 for scanner in scanners)
        )
        self.assertIn("status: INCOMPLETE (TRUNCATED)", observed[0])
        self.assertIn("tree_scan_entry_limit_reached", observed[0])
        self.assertIn("range:", observed[0])
        self.assertIn("max_scanned_entries", observed[0])
        self.assertIn("continuation_available: false", observed[0])
        self.assertNotIn("private", observed[0])

    def test_developer_model_input_preserves_truncated_production_tree_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            for index in range(130):
                (root / f"module-{index:03}.py").write_text(
                    f"value_{index} = {index}\n", encoding="utf-8"
                )
            policy = WorkspaceAccessPolicy()
            observed: list[str] = []
            loader = default_developer_runtime().load_codebase_structure
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="workspace_repo/app.py",
                        logical_task="update value",
                        atomic_tasks=[AtomicTask(atomic_task="set value to two")],
                    )
                ]
            )
            executor = DeveloperEditExecutor(
                WorkspaceEditor(root, access_policy=policy)
            )
            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=loader,
                research_atomic_task=lambda values: (
                    observed.append(values["codebase_structure"])
                    or AIMessage(content="ready")
                ),
                propose_existing_file_edit=lambda _: (
                    "<<<<<<< SEARCH\nvalue = 1\n=======\n"
                    "value = 2\n>>>>>>> REPLACE"
                ),
                propose_new_file=lambda _: self.fail("new-file model was called"),
            )

            with workspace_root_scope(root, access_policy=policy):
                result = create_developer_workflow(
                    runtime, research_tools=[], access_policy=policy
                ).invoke({"implementation_plan": plan})

        self.assertEqual(result["developer_status"], DeveloperStatus.COMPLETED)
        self.assertIn("UNTRUSTED WORKSPACE EVIDENCE", observed[0])
        self.assertIn("status: INCOMPLETE (TRUNCATED)", observed[0])
        self.assertIn("continuation_available: true", observed[0])

    def test_complete_small_tree_is_not_marked_incomplete_and_hides_policy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public.py").write_text("value = 1\n", encoding="utf-8")
            hidden = root / "private-oracle"
            hidden.mkdir()
            (hidden / "expected.txt").write_text(
                "TREE-POLICY-CANARY", encoding="utf-8"
            )
            policy = WorkspaceAccessPolicy(hidden_paths=("private-oracle",))
            observed: list[str] = []

            with workspace_root_scope(root, access_policy=policy):
                self._architect_tree_graph(
                    default_architect_runtime().load_codebase_structure,
                    observed,
                    policy,
                )

        self.assertIn("UNTRUSTED WORKSPACE EVIDENCE", observed[0])
        self.assertIn("status: COMPLETE", observed[0])
        self.assertIn("truncated: false", observed[0])
        self.assertNotIn("INCOMPLETE", observed[0])
        self.assertNotIn("private-oracle", observed[0])
        self.assertNotIn("expected.txt", observed[0])
        self.assertNotIn("TREE-POLICY-CANARY", observed[0])

    def test_failed_tree_scan_is_explicit_in_architect_model_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            policy = WorkspaceAccessPolicy()
            observed: list[str] = []
            with (
                patch(
                    "agent.tools.write._bounded_tree_entries",
                    side_effect=OSError("injected scan failure"),
                ),
                workspace_root_scope(root, access_policy=policy),
            ):
                self._architect_tree_graph(
                    default_architect_runtime().load_codebase_structure,
                    observed,
                    policy,
                )

        self.assertIn("UNTRUSTED WORKSPACE EVIDENCE", observed[0])
        self.assertIn("status: FAILED", observed[0])
        self.assertIn('error_code: "scan_failed"', observed[0])
        self.assertNotIn("status: COMPLETE", observed[0])

    def test_compiled_architect_renders_all_tool_results_before_next_model(self) -> None:
        observed: list[list[AnyMessage]] = []
        calls = 0

        def conduct(values):
            nonlocal calls
            observed.append(values["implementation_research_scratchpad"])
            calls += 1
            if calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "architect_search",
                            "args": {"query": "alpha"},
                            "id": "architect-call-a",
                            "type": "tool_call",
                        },
                        {
                            "name": "architect_codemap",
                            "args": {"path": "app.py"},
                            "id": "architect-call-b",
                            "type": "tool_call",
                        },
                    ],
                )
            return AIMessage(content="research complete")

        runtime = ArchitectRuntime(
            plan_next_step=lambda _: ResearchStep(
                reasoning="inspect", hypothesis="find target"
            ),
            check_research_step=lambda _: ResearchEvaluation(
                reasoning="useful", is_valid=True
            ),
            conduct_research=conduct,
            extract_implementation_plan=lambda _: ImplementationPlan(
                status=PlanStatus.NO_CHANGES,
                no_change_reason="already complete",
                tasks=[],
            ),
            load_codebase_structure=lambda: "app.py",
        )

        result = create_architect_workflow(
            runtime,
            research_tools=[architect_search, architect_codemap],
        ).invoke(
            {"implementation_research_scratchpad": [HumanMessage(content="inspect app")]}
        )

        self.assertEqual(result["implementation_plan"].status, PlanStatus.NO_CHANGES)
        self.assertGreaterEqual(len(observed), 2)
        context = "\n".join(str(message.content) for message in observed[1])
        self.assertIn("architect-call-a", context)
        self.assertIn("architect-call-b", context)
        self.assertIn("architect-search:alpha", context)
        self.assertIn("architect-hash", context)

    def test_compiled_developer_renders_all_tool_results_before_next_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            observed: list[list[AnyMessage]] = []
            calls = 0
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="workspace_repo/app.py",
                        logical_task="更新值",
                        atomic_tasks=[AtomicTask(atomic_task="改为 2")],
                    )
                ]
            )

            def research(values):
                nonlocal calls
                observed.append(values["atomic_implementation_research"])
                calls += 1
                if calls == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "developer_search",
                                "args": {"query": "alpha"},
                                "id": "developer-call-a",
                                "type": "tool_call",
                            },
                            {
                                "name": "developer_codemap",
                                "args": {"path": "app.py"},
                                "id": "developer-call-b",
                                "type": "tool_call",
                            },
                        ],
                    )
                return AIMessage(content="research complete")

            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            developer = create_developer_workflow(
                DeveloperRuntime(
                    edit_executor=lambda: executor,
                    load_codebase_structure=lambda: "app.py",
                    research_atomic_task=research,
                    propose_existing_file_edit=lambda _: (
                        "<<<<<<< SEARCH\nvalue = 1\n=======\n"
                        "value = 2\n>>>>>>> REPLACE"
                    ),
                    propose_new_file=lambda _: self.fail("new-file model was called"),
                ),
                research_tools=[developer_search, developer_codemap],
            )

            result = developer.invoke({"implementation_plan": plan})

            self.assertEqual(result["developer_status"], DeveloperStatus.COMPLETED)
            self.assertGreaterEqual(len(observed), 2)
            context = "\n".join(str(message.content) for message in observed[1])
            self.assertIn("developer-call-a", context)
            self.assertIn("developer-call-b", context)
            self.assertIn("developer-search:alpha", context)
            self.assertIn("developer-hash", context)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

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
