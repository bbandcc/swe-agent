import tempfile
import sqlite3
import json
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.common.entities import (
    AtomicTask,
    ImplementationPlan,
    ImplementationTask,
    PlanStatus,
)
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.developer.state import DeveloperErrorCode, DeveloperStatus
from agent.editing import EditErrorCode, EditStatus, WorkspaceEditor
from agent.developer.editing import DeveloperEditExecutor
from agent.workspace import WorkspaceAccessPolicy, workspace_root_scope
from agent.tools.codemap import get_raw_file_content
from agent.runtime import BudgetSnapshot, DurableBudgetBoundary
from agent.runtime.checkpointing import checkpoint_serializer


def search_replace_block(old_text: str, new_text: str) -> str:
    return (
        f"<<<<<<< SEARCH\n{old_text}\n=======\n"
        f"{new_text}\n>>>>>>> REPLACE"
    )


def implementation_plan(path: str = "./workspace_repo/app.py") -> ImplementationPlan:
    return ImplementationPlan(
        tasks=[
            ImplementationTask(
                file_path=path,
                logical_task="更新返回值",
                atomic_tasks=[
                    AtomicTask(atomic_task="把返回值从 41 改为 42")
                ],
            )
        ]
    )


class DeveloperWorkflowTests(unittest.TestCase):
    def raw_view_fixture(self, root):
        """Three trusted proposals exercise raw reads between real stages."""
        target = root / "app.py"
        target.write_bytes(b"value = 1\n")
        seen = []

        def research(values):
            messages = values["atomic_implementation_research"]
            if not messages:
                return AIMessage(content="", tool_calls=[{
                    "name": "get_raw_file_content", "args": {"file_path": "app.py"},
                    "id": "raw-" + values["development_task"],
                    "type": "tool_call",
                }])
            evidence = "\n".join(str(m.content) for m in messages)
            self.assertIn("[UNTRUSTED EVIDENCE]", evidence)
            self.assertNotIn("INCOMPLETE TOOL EXCHANGE", evidence)
            self.assertIn('"view": "working"', evidence)
            self.assertIn('"source_hash":', evidence)
            expected = values["file_content"]
            self.assertIn(json.dumps(expected)[1:-1], evidence)
            self.assertEqual(target.read_bytes(), b"value = 1\n")
            seen.append(expected)
            return AIMessage(content="ready")

        def propose(values):
            old = values["file_content"].rstrip("\n")
            value = int(old[-1])
            return search_replace_block(old, f"value = {value + 1}")

        runtime = DeveloperRuntime(
            edit_executor=lambda: DeveloperEditExecutor(WorkspaceEditor(root)),
            load_codebase_structure=lambda: "app.py",
            research_atomic_task=research,
            propose_existing_file_edit=propose,
            propose_new_file=lambda _: self.fail("unexpected create"),
        )
        plan = ImplementationPlan(tasks=[ImplementationTask(
            file_path="app.py", logical_task="three stages",
            atomic_tasks=[AtomicTask(atomic_task=str(n)) for n in range(3)],
        )])
        return runtime, plan, seen

    def test_real_developer_toolnode_reads_latest_stage_and_renders_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, plan, seen = self.raw_view_fixture(root)
            with workspace_root_scope(root):
                result = create_developer_workflow(
                    runtime, research_tools=[get_raw_file_content]
                ).invoke({"implementation_plan": plan})
            self.assertEqual(seen, ["value = 1\n", "value = 2\n", "value = 3\n"])
            self.assertEqual(result["developer_status"], DeveloperStatus.COMPLETED)
            self.assertEqual((root / "app.py").read_bytes(), b"value = 4\n")

    def test_working_raw_survives_sqlite_close_reopen_with_shared_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "workspace"
            root.mkdir()
            runtime, plan, seen = self.raw_view_fixture(root)
            config = {"configurable": {"thread_id": "working-raw"}}
            developer = create_developer_workflow(
                runtime, research_tools=[get_raw_file_content],
                budget_boundary=DurableBudgetBoundary("raw-run", clock=lambda: 100.0),
                run_id="raw-run", task_id="raw-task",
            )
            with workspace_root_scope(root):
                with sqlite3.connect(parent / "checkpoint.sqlite", check_same_thread=False) as connection:
                    saver = SqliteSaver(connection, serde=checkpoint_serializer())
                    first = developer.builder.compile(
                        checkpointer=saver, interrupt_after=["stage_diff_for_task"]
                    )
                    first.invoke({
                        "implementation_plan": plan,
                        "budget": BudgetSnapshot.create(max_steps=30, max_cost_usd=None, deadline_at=200.0),
                    }, config, durability="sync")
                    before = first.get_state(config).values
                    self.assertEqual(before["current_file_transaction"].working_content, "value = 2\n")
                    self.assertEqual((root / "app.py").read_bytes(), b"value = 1\n")
                    used = before["budget"].steps_used
                # sqlite3 context commits but does not close; explicitly close
                # before constructing the fresh connection and compiled graph.
                connection.close()
                with sqlite3.connect(parent / "checkpoint.sqlite", check_same_thread=False) as reopened:
                    saver = SqliteSaver(reopened, serde=checkpoint_serializer())
                    resumed = developer.builder.compile(checkpointer=saver)
                    after = resumed.invoke(None, config, durability="sync")
                    self.assertEqual(after["developer_status"], DeveloperStatus.COMPLETED)
                    self.assertGreater(after["budget"].steps_used, used)
                    self.assertEqual(after["budget"].steps_used, 12)
                    self.assertEqual(after["budget"].active, ())
                    self.assertIsNone(after["pending_write"])
                reopened.close()
            self.assertEqual(seen, ["value = 1\n", "value = 2\n", "value = 3\n"])
            self.assertEqual((root / "app.py").read_bytes(), b"value = 4\n")

    def test_protected_plan_target_is_rejected_before_model_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = DeveloperEditExecutor(
                WorkspaceEditor(root, access_policy=WorkspaceAccessPolicy())
            )
            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: self.fail("workspace was scanned"),
                research_atomic_task=lambda _: self.fail("research was called"),
                propose_existing_file_edit=lambda _: self.fail("edit was called"),
                propose_new_file=lambda _: self.fail("create was called"),
            )
            result = create_developer_workflow(
                runtime, research_tools=[]
            ).invoke({"implementation_plan": implementation_plan(".git/config")})

            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["last_edit_result"].error_code,
                EditErrorCode.WRITE_DENIED,
            )
            self.assertEqual(result["current_task_idx"], 0)

    def test_advances_only_after_edit_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text(
                "def answer():\n    return 41\n", encoding="utf-8", newline=""
            )
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: AIMessage(content="ready"),
                propose_existing_file_edit=lambda _: search_replace_block(
                    "    return 41", "    return 42"
                ),
                propose_new_file=lambda _: self.fail("new-file model was called"),
            )

            graph = create_developer_workflow(runtime, research_tools=[])
            result = graph.invoke({"implementation_plan": implementation_plan()})

            self.assertEqual(result["current_task_idx"], 1)
            self.assertEqual(result["last_edit_result"].status, EditStatus.APPLIED)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "def answer():\n    return 42\n",
            )

    def test_does_not_advance_rejected_or_noop_edit(self) -> None:
        cases = (
            (
                search_replace_block("missing", "new"),
                EditStatus.REJECTED,
                EditErrorCode.MATCH_NOT_FOUND,
            ),
            (
                search_replace_block("value = 1", "value = 1"),
                EditStatus.NOOP,
                None,
            ),
        )
        for model_output, expected_status, expected_error in cases:
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "app.py"
                target.write_text("value = 1\n", encoding="utf-8", newline="")
                executor = DeveloperEditExecutor(WorkspaceEditor(root))
                runtime = DeveloperRuntime(
                    edit_executor=lambda: executor,
                    load_codebase_structure=lambda: "app.py",
                    research_atomic_task=lambda _: AIMessage(content="ready"),
                    propose_existing_file_edit=lambda _: model_output,
                    propose_new_file=lambda _: self.fail("new-file model was called"),
                )

                result = create_developer_workflow(
                    runtime, research_tools=[]
                ).invoke({"implementation_plan": implementation_plan()})

                self.assertEqual(result["current_task_idx"], 0)
                self.assertEqual(
                    result["last_edit_result"].status, expected_status
                )
                self.assertEqual(
                    result["last_edit_result"].error_code, expected_error
                )
                self.assertEqual(
                    target.read_text(encoding="utf-8"), "value = 1\n"
                )

    def test_rejects_implicit_empty_plan_before_calling_dependencies(self) -> None:
        runtime = DeveloperRuntime(
            edit_executor=lambda: self.fail("workspace was accessed"),
            load_codebase_structure=lambda: self.fail("workspace was scanned"),
            research_atomic_task=lambda _: self.fail("research model was called"),
            propose_existing_file_edit=lambda _: self.fail("edit model was called"),
            propose_new_file=lambda _: self.fail("new-file model was called"),
        )

        result = create_developer_workflow(runtime, research_tools=[]).invoke(
            {"implementation_plan": ImplementationPlan(tasks=[])}
        )

        self.assertEqual(result["current_task_idx"], 0)
        self.assertEqual(
            result["developer_status"], DeveloperStatus.FAILED
        )
        self.assertEqual(
            result["developer_error_code"], DeveloperErrorCode.INVALID_PLAN
        )

    def test_rejects_duplicate_canonical_file_tasks_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="./workspace_repo/app.py",
                        logical_task="第一次修改",
                        atomic_tasks=[AtomicTask(atomic_task="改为 2")],
                    ),
                    ImplementationTask(
                        file_path="workspace_repo/app.py",
                        logical_task="重复修改",
                        atomic_tasks=[AtomicTask(atomic_task="改为 3")],
                    ),
                ]
            )
            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: self.fail("workspace was scanned"),
                research_atomic_task=lambda _: self.fail("research model was called"),
                propose_existing_file_edit=lambda _: self.fail("edit model was called"),
                propose_new_file=lambda _: self.fail("new-file model was called"),
            )

            result = create_developer_workflow(runtime, research_tools=[]).invoke(
                {"implementation_plan": plan}
            )

            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["developer_error_code"], DeveloperErrorCode.INVALID_PLAN
            )
            self.assertIsNone(result["last_edit_result"])
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")

    def test_accepts_explicit_no_change_plan_without_calling_dependencies(self) -> None:
        runtime = DeveloperRuntime(
            edit_executor=lambda: self.fail("workspace was accessed"),
            load_codebase_structure=lambda: self.fail("workspace was scanned"),
            research_atomic_task=lambda _: self.fail("research model was called"),
            propose_existing_file_edit=lambda _: self.fail("edit model was called"),
            propose_new_file=lambda _: self.fail("new-file model was called"),
        )

        result = create_developer_workflow(runtime, research_tools=[]).invoke(
            {
                "implementation_plan": ImplementationPlan(
                    status=PlanStatus.NO_CHANGES,
                    no_change_reason="Requested behavior is already present.",
                    tasks=[],
                )
            }
        )

        self.assertEqual(result["developer_status"], DeveloperStatus.NO_CHANGES)
        self.assertIsNone(result["developer_error_code"])
        self.assertIsNone(result["last_edit_result"])

    def test_rejects_escaping_plan_path_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: self.fail("workspace was scanned"),
                research_atomic_task=lambda _: self.fail("research model was called"),
                propose_existing_file_edit=lambda _: self.fail("edit model was called"),
                propose_new_file=lambda _: self.fail("new-file model was called"),
            )

            result = create_developer_workflow(
                runtime, research_tools=[]
            ).invoke(
                {
                    "implementation_plan": implementation_plan(
                        "./workspace_repo/../secret.txt"
                    )
                }
            )

            self.assertEqual(result["current_task_idx"], 0)
            self.assertEqual(
                result["last_edit_result"].error_code,
                EditErrorCode.PATH_INVALID,
            )

    def test_creates_missing_file_through_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "empty workspace",
                research_atomic_task=lambda _: AIMessage(content="ready"),
                propose_existing_file_edit=lambda _: self.fail("edit model was called"),
                propose_new_file=lambda _: "value = 42\n",
            )

            result = create_developer_workflow(
                runtime, research_tools=[]
            ).invoke(
                {
                    "implementation_plan": implementation_plan(
                        "./workspace_repo/new.py"
                    )
                }
            )

            self.assertEqual(result["current_task_idx"], 1)
            self.assertEqual(result["last_edit_result"].status, EditStatus.APPLIED)
            self.assertEqual(
                (root / "new.py").read_text(encoding="utf-8"), "value = 42\n"
            )

    def test_stages_same_file_edits_in_memory_and_commits_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="./workspace_repo/app.py",
                        logical_task="连续更新",
                        atomic_tasks=[
                            AtomicTask(atomic_task="第一次更新"),
                            AtomicTask(atomic_task="第二次更新"),
                        ],
                    )
                ]
            )

            def propose(values):
                if values["task"] == "第一次更新":
                    self.assertEqual(values["file_content"], "value = 1\n")
                    return search_replace_block("value = 1", "value = 2")
                self.assertEqual(values["file_content"], "value = 2\n")
                self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")
                return search_replace_block("value = 2", "value = 3")

            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: AIMessage(content="ready"),
                propose_existing_file_edit=propose,
                propose_new_file=lambda _: self.fail("new-file model was called"),
            )

            result = create_developer_workflow(runtime, research_tools=[]).invoke(
                {"implementation_plan": plan}
            )

            self.assertEqual(result["current_task_idx"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 3\n")
            self.assertEqual(
                result["last_edit_result"].task_ids,
                ("task-1.step-1", "task-1.step-2"),
            )

    def test_does_not_advance_when_final_transaction_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            original = "value = 1\n"
            target.write_text(original, encoding="utf-8", newline="")
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="workspace_repo/app.py",
                        logical_task="抵消连续更新",
                        atomic_tasks=[
                            AtomicTask(atomic_task="第一次更新"),
                            AtomicTask(atomic_task="第二次更新"),
                            AtomicTask(atomic_task="抵消更新"),
                        ],
                    )
                ]
            )

            def propose(values):
                replacements = {
                    "第一次更新": ("value = 1", "value = 2"),
                    "第二次更新": ("value = 2", "value = 3"),
                    "抵消更新": ("value = 3", "value = 1"),
                }
                old_text, new_text = replacements[values["task"]]
                return search_replace_block(old_text, new_text)

            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: AIMessage(content="ready"),
                propose_existing_file_edit=propose,
                propose_new_file=lambda _: self.fail("new-file model was called"),
            )

            result = create_developer_workflow(runtime, research_tools=[]).invoke(
                {"implementation_plan": plan}
            )

            self.assertEqual(result["current_task_idx"], 0)
            self.assertEqual(result["last_edit_result"].status, EditStatus.NOOP)
            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["last_edit_result"].task_ids,
                ("task-1.step-1", "task-1.step-2", "task-1.step-3"),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_rejects_entire_same_file_transaction_when_later_edit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="./workspace_repo/app.py",
                        logical_task="连续更新",
                        atomic_tasks=[
                            AtomicTask(atomic_task="第一次更新"),
                            AtomicTask(atomic_task="第二次失败"),
                        ],
                    )
                ]
            )

            def propose(values):
                if values["task"] == "第一次更新":
                    return search_replace_block("value = 1", "value = 2")
                self.assertEqual(values["file_content"], "value = 2\n")
                return search_replace_block("missing", "value = 3")

            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: AIMessage(content="ready"),
                propose_existing_file_edit=propose,
                propose_new_file=lambda _: self.fail("new-file model was called"),
            )

            result = create_developer_workflow(runtime, research_tools=[]).invoke(
                {"implementation_plan": plan}
            )

            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["last_edit_result"].error_code,
                EditErrorCode.MATCH_NOT_FOUND,
            )
            self.assertEqual(
                result["last_edit_result"].task_ids,
                ("task-1.step-1", "task-1.step-2"),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")


if __name__ == "__main__":
    unittest.main()
