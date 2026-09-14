import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

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
