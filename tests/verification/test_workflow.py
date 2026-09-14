import sys
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.developer.state import DeveloperStatus
from agent.editing import EditErrorCode, WorkspaceEditor
from agent.graph import create_workflow_graph
from agent.verification import (
    VerificationCheckStatus,
    VerificationRunner,
    VerificationSpec,
    VerificationStatus,
)


def search_replace(old_text: str, new_text: str) -> str:
    return (
        f"<<<<<<< SEARCH\n{old_text}\n=======\n"
        f"{new_text}\n>>>>>>> REPLACE"
    )


def plan() -> ImplementationPlan:
    return ImplementationPlan(
        tasks=[
            ImplementationTask(
                file_path="workspace_repo/app.py",
                logical_task="更新值",
                atomic_tasks=[AtomicTask(atomic_task="更新 app.py")],
            )
        ]
    )


def file_check(*, timeout_seconds: float = 2) -> VerificationSpec:
    executable = getattr(sys, "_base_executable", sys.executable)
    return VerificationSpec(
        name="app-check",
        argv=(
            executable,
            "-c",
            (
                "from pathlib import Path; import sys; "
                "text=Path('app.py').read_text(encoding='utf-8'); "
                "sys.exit(1 if 'BROKEN' in text else 0)"
            ),
        ),
        timeout_seconds=timeout_seconds,
    )


def developer(root: Path, propose):
    executor = DeveloperEditExecutor(WorkspaceEditor(root))
    runtime = DeveloperRuntime(
        edit_executor=lambda: executor,
        load_codebase_structure=lambda: "app.py",
        research_atomic_task=lambda _: AIMessage(content="ready"),
        propose_existing_file_edit=propose,
        propose_new_file=lambda _: "value = 1\n",
    )
    return create_developer_workflow(runtime, research_tools=[])


def run_parent(root: Path, child_developer, specs):
    return create_workflow_graph(
        architect=lambda _: {"implementation_plan": plan()},
        developer=child_developer,
        verification_specs=specs,
        verification_runner=VerificationRunner(root),
    ).compile().invoke({})


class VerificationWorkflowTests(unittest.TestCase):
    def test_repairs_one_regression_with_structured_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")

            def propose(values):
                feedback = values["verification_feedback"]
                if not feedback:
                    return search_replace("value = 1", "value = 'BROKEN'")
                self.assertEqual(values["file_content"], "value = 'BROKEN'\n")
                self.assertEqual(feedback["attempt"], 1)
                self.assertEqual(feedback["status"], "regression")
                self.assertEqual(
                    feedback["post_results"][0]["status"], "fail"
                )
                return search_replace("value = 'BROKEN'", "value = 2")

            result = run_parent(
                root, developer(root, propose), (file_check(),)
            )

            self.assertEqual(
                result["verification_status"], VerificationStatus.VERIFIED
            )
            self.assertEqual(result["repair_attempts"], 1)
            self.assertEqual(
                result["baseline_verification"][0].status,
                VerificationCheckStatus.PASS,
            )
            self.assertEqual(
                result["post_verification"][0].status,
                VerificationCheckStatus.PASS,
            )
            self.assertEqual(
                result["developer_status"], DeveloperStatus.COMPLETED
            )
            self.assertEqual(
                result["last_edit_result"].task_ids,
                ("repair-1.task-1.step-1",),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_stops_after_two_failed_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            replacements = iter(
                [
                    ("value = 1", "value = 'BROKEN-0'"),
                    ("value = 'BROKEN-0'", "value = 'BROKEN-1'"),
                    ("value = 'BROKEN-1'", "value = 'BROKEN-2'"),
                ]
            )
            calls = 0

            def propose(_):
                nonlocal calls
                calls += 1
                old, new = next(replacements)
                return search_replace(old, new)

            result = run_parent(
                root, developer(root, propose), (file_check(),)
            )

            self.assertEqual(
                result["verification_status"],
                VerificationStatus.REPAIR_EXHAUSTED,
            )
            self.assertEqual(result["repair_attempts"], 2)
            self.assertEqual(calls, 3)
            self.assertEqual(
                result["last_edit_result"].task_ids,
                ("repair-2.task-1.step-1",),
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"), "value = 'BROKEN-2'\n"
            )

    def test_repair_cannot_bypass_workspace_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")

            def propose(values):
                if not values["verification_feedback"]:
                    return search_replace("value = 1", "value = 'BROKEN'")
                return search_replace("missing text", "value = 2")

            result = run_parent(
                root, developer(root, propose), (file_check(),)
            )

            self.assertEqual(result["repair_attempts"], 1)
            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["last_edit_result"].error_code,
                EditErrorCode.MATCH_NOT_FOUND,
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"), "value = 'BROKEN'\n"
            )

    def test_no_configured_checks_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            child = developer(
                root,
                lambda _: search_replace("value = 1", "value = 2"),
            )

            result = run_parent(root, child, ())

            self.assertEqual(
                result["verification_status"], VerificationStatus.UNVERIFIED
            )
            self.assertEqual(result["repair_attempts"], 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_baseline_failure_that_becomes_pass_is_improved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text(
                "value = 'BROKEN'\n", encoding="utf-8", newline=""
            )
            child = developer(
                root,
                lambda _: search_replace("value = 'BROKEN'", "value = 2"),
            )

            result = run_parent(root, child, (file_check(),))

            self.assertEqual(
                result["verification_status"], VerificationStatus.IMPROVED
            )
            self.assertEqual(result["repair_attempts"], 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_matching_baseline_failure_does_not_trigger_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text(
                "value = 1\nBROKEN = True\n",
                encoding="utf-8",
                newline="",
            )
            child = developer(
                root,
                lambda _: search_replace("value = 1", "value = 2"),
            )

            result = run_parent(root, child, (file_check(),))

            self.assertEqual(
                result["verification_status"],
                VerificationStatus.PRE_EXISTING_FAILURE,
            )
            self.assertEqual(result["repair_attempts"], 0)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "value = 2\nBROKEN = True\n",
            )

    def test_baseline_execution_problems_block_edit_and_repair(self) -> None:
        executable = getattr(sys, "_base_executable", sys.executable)
        cases = (
            VerificationSpec(
                name="slow",
                argv=(executable, "-c", "import time; time.sleep(5)"),
                timeout_seconds=0.05,
            ),
            VerificationSpec(
                name="missing",
                argv=("command-that-does-not-exist-s2-workflow",),
            ),
        )
        for spec in cases:
            with (
                self.subTest(status=spec.name),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                target = root / "app.py"
                target.write_text(
                    "value = 1\n", encoding="utf-8", newline=""
                )
                child = developer(
                    root, lambda _: self.fail("Developer must not run")
                )

                result = run_parent(root, child, (spec,))

                self.assertEqual(
                    result["verification_status"],
                    VerificationStatus.VERIFICATION_ERROR,
                )
                self.assertEqual(result["repair_attempts"], 0)
                self.assertEqual(
                    result["developer_status"], DeveloperStatus.PENDING
                )
                self.assertEqual(
                    target.read_text(encoding="utf-8"), "value = 1\n"
                )


if __name__ == "__main__":
    unittest.main()
