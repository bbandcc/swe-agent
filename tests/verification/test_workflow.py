import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.developer.state import DeveloperStatus
from agent.editing import (
    CommittedEdit,
    EditErrorCode,
    EditResult,
    EditStatus,
    WorkspaceEditor,
    WorkspaceTransaction,
)
from agent.graph import AgentState, WorkflowOutcome, create_workflow_graph
from agent.runtime import BudgetErrorCode, BudgetSnapshot
from agent.runtime.durable import DurableRunResult, DurableRunStatus, run_exit_code
from agent.verification import (
    REPORT_SCHEMA,
    VerificationCase,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationRunner,
    VerificationResult,
    VerificationReport,
    VerificationSummary,
    VerificationSpec,
    VerificationStatus,
    RepairScopePolicy,
)
from agent.verification.workflow import VerificationController


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


def file_check(root: Path, *, timeout_seconds: float = 2) -> VerificationSpec:
    executable = getattr(sys, "_base_executable", sys.executable)
    (root / "test_app_check.py").write_text(
        "from pathlib import Path\n"
        "def test_app_file():\n"
        "    assert 'BROKEN' not in Path('app.py').read_text(encoding='utf-8')\n",
        encoding="utf-8",
        newline="",
    )
    return VerificationSpec(
        name="app-check",
        argv=(
            executable,
            "-m",
            "pytest",
            "-q",
            "--junitxml=report.xml",
        ),
        timeout_seconds=timeout_seconds,
        report_path="report.xml",
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


def multi_file_plan() -> ImplementationPlan:
    return ImplementationPlan(
        tasks=[
            ImplementationTask(
                file_path="workspace_repo/a.py",
                logical_task="更新 a",
                atomic_tasks=[AtomicTask(atomic_task="更新 a")],
            ),
            ImplementationTask(
                file_path="workspace_repo/b.py",
                logical_task="更新 b",
                atomic_tasks=[AtomicTask(atomic_task="更新 b")],
            ),
        ]
    )


def multi_file_developer(root: Path):
    executor = DeveloperEditExecutor(WorkspaceEditor(root))

    def propose(values):
        old = "value = 1" if not values["verification_feedback"] else "value = 2"
        new = "value = 2" if not values["verification_feedback"] else "value = 3"
        return search_replace(old, new)

    runtime = DeveloperRuntime(
        edit_executor=lambda: executor,
        load_codebase_structure=lambda: "a.py b.py",
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


class _SequenceRunner:
    def __init__(self, *statuses: VerificationCheckStatus) -> None:
        self._statuses = iter(statuses)

    def run(self, spec: VerificationSpec) -> VerificationResult:
        status = next(self._statuses)
        return VerificationResult.create(
            name=spec.name,
            argv=("pytest", "--junitxml=report.xml"),
            cwd=spec.cwd,
            status=status,
            exit_code=0 if status is VerificationCheckStatus.PASS else 1,
            stderr=(
                "failure"
                if status is not VerificationCheckStatus.PASS
                else ""
            ),
            report=VerificationReport(
                check_id=spec.name,
                report_schema=REPORT_SCHEMA,
                cases=(
                    (
                        VerificationCase(
                            check_id=spec.name,
                            case_id="target",
                            status=VerificationCaseStatus.PASS,
                        ),
                        VerificationCase(
                            check_id=spec.name,
                            case_id="legacy",
                            status=VerificationCaseStatus.FAIL,
                        ),
                    )
                    if spec.allowed_failure_case_ids
                    else (
                        VerificationCase(
                            check_id=spec.name,
                            case_id="case",
                            status=(
                                VerificationCaseStatus.PASS
                                if status is VerificationCheckStatus.PASS
                                else VerificationCaseStatus.FAIL
                            ),
                        ),
                    )
                ),
            ),
            allowed_failure_case_ids=spec.allowed_failure_case_ids,
        )


class VerificationWorkflowTests(unittest.TestCase):
    def test_last_file_repair_does_not_touch_earlier_committed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a.py", "b.py"):
                (root / name).write_text("value = 1\n", encoding="utf-8", newline="")
            runner = _SequenceRunner(
                VerificationCheckStatus.PASS,
                VerificationCheckStatus.FAIL,
                VerificationCheckStatus.PASS,
            )
            result = create_workflow_graph(
                architect=lambda _: {"implementation_plan": multi_file_plan()},
                developer=multi_file_developer(root),
                verification_specs=(VerificationSpec("check", ("unused",)),),
                verification_runner=runner,
            ).compile().invoke({})
            self.assertEqual(result["verification_status"], VerificationStatus.VERIFIED)
            self.assertEqual([edit.path for edit in result["committed_edits"]], ["a.py", "b.py", "b.py"])
            self.assertEqual((root / "a.py").read_text(), "value = 2\n")
            self.assertEqual((root / "b.py").read_text(), "value = 3\n")

    def test_committed_plan_scope_repairs_each_original_committed_file_in_plan_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a.py", "b.py"):
                (root / name).write_text("value = 1\n", encoding="utf-8", newline="")
            runner = _SequenceRunner(
                VerificationCheckStatus.PASS,
                VerificationCheckStatus.FAIL,
                VerificationCheckStatus.PASS,
            )
            result = create_workflow_graph(
                architect=lambda _: {"implementation_plan": multi_file_plan()},
                developer=multi_file_developer(root),
                verification_specs=(VerificationSpec("check", ("unused",)),),
                verification_runner=runner,
                repair_scope_policy=RepairScopePolicy.COMMITTED_PLAN_FILES,
            ).compile().invoke({})
            self.assertEqual(result["verification_status"], VerificationStatus.VERIFIED)
            self.assertEqual(
                [edit.path for edit in result["committed_edits"]],
                ["a.py", "b.py", "a.py", "b.py"],
            )
            self.assertEqual((root / "a.py").read_text(), "value = 3\n")
            self.assertEqual((root / "b.py").read_text(), "value = 3\n")
    def _committed_record(self, path: str, index: int, repair_attempt: int = 0):
        transaction = WorkspaceTransaction(
            path=path,
            existed=True,
            original_content=f"{path}:before\n",
            working_content=f"{path}:after-{repair_attempt}\n",
            base_hash="a" * 64,
            original_mode=None,
            task_ids=(f"task-{index}.step-1",),
        )
        result = EditResult(
            status=EditStatus.APPLIED,
            path=path,
            before_hash="a" * 64,
            after_hash="b" * 64,
            diff="same repair patch",
            task_ids=transaction.task_ids,
        )
        return CommittedEdit.create(
            run_id="run-1",
            task_id="task-1",
            task_index=index,
            repair_attempt=repair_attempt,
            transaction=transaction,
            result=result,
        )

    def test_repair_scope_only_uses_original_committed_plan_files_in_order(self):
        plan_with_three_files = ImplementationPlan(
            tasks=[
                ImplementationTask(
                    file_path="workspace_repo/a.py",
                    logical_task="a",
                    atomic_tasks=[AtomicTask(atomic_task="a")],
                ),
                ImplementationTask(
                    file_path="workspace_repo/b.py",
                    logical_task="b",
                    atomic_tasks=[AtomicTask(atomic_task="b")],
                ),
                ImplementationTask(
                    file_path="workspace_repo/c.py",
                    logical_task="c",
                    atomic_tasks=[AtomicTask(atomic_task="c")],
                ),
            ]
        )
        committed = (self._committed_record("a.py", 0), self._committed_record("b.py", 1))
        state = AgentState(
            implementation_plan=plan_with_three_files,
            committed_edits=committed,
            last_edit_result=EditResult(status=EditStatus.APPLIED, path="b.py"),
            developer_status=DeveloperStatus.COMPLETED,
            verification_status=VerificationStatus.REGRESSION,
        )

        last_file = VerificationController((), None, ".")
        self.assertEqual(
            [task.file_path for task in last_file.prepare_repair(state)["repair_plan"].tasks],
            ["workspace_repo/b.py"],
        )
        committed_scope = VerificationController(
            (), None, ".", repair_scope_policy=RepairScopePolicy.COMMITTED_PLAN_FILES
        )
        self.assertEqual(
            [task.file_path for task in committed_scope.prepare_repair(state)["repair_plan"].tasks],
            ["workspace_repo/a.py", "workspace_repo/b.py"],
        )

    def test_same_structured_failure_and_patch_stops_repair_early(self):
        spec = VerificationSpec(
            "check", ("python", "-m", "pytest", "--junitxml=report.xml")
        )
        report = VerificationReport(
            check_id="check",
            report_schema=REPORT_SCHEMA,
            cases=(
                VerificationCase(
                    check_id="check",
                    case_id="target",
                    status=VerificationCaseStatus.FAIL,
                ),
            ),
        )
        failing = VerificationSummary.from_result(
            VerificationResult.create(
                name="check",
                argv=spec.argv,
                cwd=".",
                status=VerificationCheckStatus.FAIL,
                exit_code=1,
                report=report,
            )
        )
        baseline = VerificationSummary.from_result(
            VerificationResult.create(
                name="check",
                argv=spec.argv,
                cwd=".",
                status=VerificationCheckStatus.PASS,
                exit_code=0,
                report=VerificationReport(
                    check_id="check",
                    report_schema=REPORT_SCHEMA,
                    cases=(
                        VerificationCase(
                            check_id="check",
                            case_id="target",
                            status=VerificationCaseStatus.PASS,
                        ),
                    ),
                ),
            )
        )
        committed = self._committed_record("app.py", 0, repair_attempt=0)
        class FailureRunner:
            def run(self, configured):
                return VerificationResult.create(
                    name=configured.name,
                    argv=("python", "-m", "pytest", "--junitxml=report.xml"),
                    cwd=".",
                    status=VerificationCheckStatus.FAIL,
                    exit_code=1,
                    report=report,
                )

        controller = VerificationController((spec,), FailureRunner(), ".")
        initial = AgentState(
            implementation_plan=plan(),
            committed_edits=(committed,),
            last_edit_result=EditResult(status=EditStatus.APPLIED, path="app.py"),
            baseline_verification=(baseline,),
            post_verification=(failing,),
            developer_status=DeveloperStatus.COMPLETED,
            verification_status=VerificationStatus.REGRESSION,
        )
        prepared = controller.prepare_repair(initial)
        repaired = initial.model_copy(
            update={
                **prepared,
                "repair_attempts": 1,
                "committed_edits": (self._committed_record("app.py", 0, repair_attempt=1),),
                "post_verification": (failing,),
                "developer_status": DeveloperStatus.COMPLETED,
            }
        )
        post = controller.run_post(repaired)
        self.assertEqual(post["verification_status"], VerificationStatus.REPAIR_EXHAUSTED)
        self.assertEqual(post["outcome"], WorkflowOutcome.FAILED)

    def test_failure_signature_ignores_diagnostic_noise(self):
        spec = VerificationSpec(
            "check", ("python", "-m", "pytest", "--junitxml=report.xml")
        )
        def summary(stdout: str):
            return VerificationSummary.from_result(
                VerificationResult.create(
                    name=spec.name,
                    argv=spec.argv,
                    cwd=".",
                    status=VerificationCheckStatus.FAIL,
                    exit_code=1,
                    stdout=stdout,
                    report=VerificationReport(
                        check_id=spec.name,
                        report_schema=REPORT_SCHEMA,
                        cases=(
                            VerificationCase(
                                check_id=spec.name,
                                case_id="target",
                                status=VerificationCaseStatus.FAIL,
                            ),
                        ),
                    ),
                )
            )
        from agent.verification.workflow import verification_failure_signature

        self.assertEqual(
            verification_failure_signature((summary("first"),)),
            verification_failure_signature((summary("second"),)),
        )

    def test_changed_failure_signature_still_allows_bounded_repair(self):
        spec = VerificationSpec(
            "check", ("python", "-m", "pytest", "--junitxml=report.xml")
        )
        def result(case_id: str):
            cases = tuple(
                VerificationCase(
                    check_id=spec.name,
                    case_id=current,
                    status=(
                        VerificationCaseStatus.FAIL
                        if current == case_id
                        else VerificationCaseStatus.PASS
                    ),
                )
                for current in ("target", "other")
            )
            return VerificationSummary.from_result(
                VerificationResult.create(
                    name=spec.name,
                    argv=spec.argv,
                    cwd=".",
                    status=VerificationCheckStatus.FAIL,
                    exit_code=1,
                    report=VerificationReport(
                        check_id=spec.name,
                        report_schema=REPORT_SCHEMA,
                        cases=cases,
                    ),
                )
            )
        baseline = VerificationSummary.from_result(
            VerificationResult.create(
                name=spec.name,
                argv=spec.argv,
                cwd=".",
                status=VerificationCheckStatus.PASS,
                exit_code=0,
                report=VerificationReport(
                    check_id=spec.name,
                    report_schema=REPORT_SCHEMA,
                    cases=tuple(
                        VerificationCase(
                            check_id=spec.name,
                            case_id=current,
                            status=VerificationCaseStatus.PASS,
                        )
                        for current in ("target", "other")
                    ),
                ),
            )
        )
        state = AgentState(
            baseline_verification=(baseline,),
            post_verification=(result("first"),),
            developer_status=DeveloperStatus.COMPLETED,
            verification_status=VerificationStatus.REGRESSION,
            repair_attempts=1,
            repair_failure_signatures=((
                ("check", "other", "fail"),
            ),),
            repair_patch_digests=("p" * 64,),
            implementation_plan=plan(),
            last_edit_result=EditResult(status=EditStatus.APPLIED, path="app.py"),
            committed_edits=(self._committed_record("app.py", 0, repair_attempt=1),),
        )
        class Runner:
            def run(self, configured):
                return VerificationResult.create(
                    name=configured.name,
                    argv=configured.argv,
                    cwd=configured.cwd,
                    status=VerificationCheckStatus.FAIL,
                    exit_code=1,
                    report=VerificationReport(
                        check_id=configured.name,
                        report_schema=REPORT_SCHEMA,
                        cases=(
                            VerificationCase(
                                check_id=configured.name,
                                case_id="target",
                                status=VerificationCaseStatus.FAIL,
                            ),
                            VerificationCase(
                                check_id=configured.name,
                                case_id="other",
                                status=VerificationCaseStatus.PASS,
                            ),
                        ),
                    ),
                )

        controller = VerificationController((spec,), Runner(), ".")
        post = controller.run_post(state)
        self.assertEqual(post["verification_status"], VerificationStatus.REGRESSION)

    def test_uncommitted_plan_file_never_enters_repair(self):
        calls = []
        runner = _SequenceRunner(
            VerificationCheckStatus.PASS,
            VerificationCheckStatus.FAIL,
        )
        result = create_workflow_graph(
            architect=lambda _: {"implementation_plan": plan()},
            developer=lambda _: (
                calls.append("developer")
                or {"developer_status": DeveloperStatus.COMPLETED}
            ),
            verification_specs=(VerificationSpec("check", ("unused",)),),
            verification_runner=runner,
            repair_scope_policy=RepairScopePolicy.COMMITTED_PLAN_FILES,
        ).compile().invoke({})
        self.assertEqual(calls, ["developer"])
        self.assertEqual(result["repair_attempts"], 0)
        self.assertEqual(result["verification_status"], VerificationStatus.REPAIR_EXHAUSTED)
        self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)
    def test_baseline_clamps_each_check_to_remaining_run_deadline(self) -> None:
        now = [100.0]

        class Runner:
            def __init__(self) -> None:
                self.specs: list[VerificationSpec] = []

            def run(self, spec: VerificationSpec) -> VerificationResult:
                self.specs.append(spec)
                now[0] = 106.0
                return VerificationResult.create(
                    name=spec.name,
                    argv=spec.argv,
                    cwd=spec.cwd,
                    status=VerificationCheckStatus.PASS,
                    exit_code=0,
                )

        runner = Runner()
        controller = VerificationController(
            (
                VerificationSpec("first", ("unused",), timeout_seconds=30),
                VerificationSpec("second", ("unused",), timeout_seconds=30),
            ),
            runner,
            ".",
            clock=lambda: now[0],
        )
        state = AgentState(
            budget=BudgetSnapshot.create(
                max_steps=1, max_cost_usd=None, deadline_at=105.0
            )
        )

        update = controller.run_baseline(state)

        self.assertEqual(len(runner.specs), 1)
        self.assertEqual(runner.specs[0].timeout_seconds, 5.0)
        self.assertEqual(
            update["runtime_error_code"], BudgetErrorCode.TIMEOUT_OVERRUN
        )
        self.assertEqual(update["outcome"], WorkflowOutcome.FAILED)

    def test_artifact_failure_cannot_be_accepted_as_verified(self) -> None:
        spec = VerificationSpec("artifact-check", ("unused",))
        report = VerificationReport(
            check_id=spec.name,
            report_schema=REPORT_SCHEMA,
            cases=(
                VerificationCase(
                    check_id=spec.name,
                    case_id="target",
                    status=VerificationCaseStatus.PASS,
                ),
            ),
        )

        class Runner:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, configured: VerificationSpec) -> VerificationResult:
                self.calls += 1
                return VerificationResult.create(
                    name=configured.name,
                    argv=configured.argv,
                    cwd=configured.cwd,
                    status=VerificationCheckStatus.PASS,
                    exit_code=0,
                    report=report,
                    artifact_error_code=(
                        "artifact_quota_exceeded" if self.calls == 2 else None
                    ),
                )

        runner = Runner()
        controller = VerificationController((spec,), runner, ".")
        baseline = controller.run_baseline(AgentState())
        baseline.update(
            {
                "developer_status": DeveloperStatus.COMPLETED,
                "outcome": WorkflowOutcome.PENDING,
            }
        )
        state = AgentState(
            **baseline,
        )
        post = controller.run_post(state)

        self.assertEqual(runner.calls, 2)
        self.assertEqual(
            post["verification_status"], VerificationStatus.EVIDENCE_INSUFFICIENT
        )
        self.assertFalse(post["acceptance"].accepted)
        self.assertEqual(post["outcome"], WorkflowOutcome.FAILED)

    def test_expired_deadline_starts_no_post_verification_process(self) -> None:
        class Runner:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, spec: VerificationSpec) -> VerificationResult:
                self.calls += 1
                raise AssertionError("expired deadline must start no subprocess")

        runner = Runner()
        controller = VerificationController(
            (VerificationSpec("check", ("unused",), timeout_seconds=30),),
            runner,
            ".",
            clock=lambda: 105.0,
        )
        state = AgentState(
            budget=BudgetSnapshot.create(
                max_steps=1, max_cost_usd=None, deadline_at=105.0
            ),
            baseline_verification=(
                VerificationResult.create(
                    name="check",
                    argv=("unused",),
                    cwd=".",
                    status=VerificationCheckStatus.PASS,
                    exit_code=0,
                ),
            ),
            developer_status=DeveloperStatus.COMPLETED,
        )

        update = controller.run_post(state)

        self.assertEqual(runner.calls, 0)
        self.assertEqual(
            update["runtime_error_code"], BudgetErrorCode.TIMEOUT_OVERRUN
        )
        self.assertEqual(
            update["post_verification"][0].status,
            VerificationCheckStatus.TIMEOUT,
        )
        self.assertEqual(update["outcome"], WorkflowOutcome.FAILED)

    def test_repair_path_uses_s1_case_sensitive_canonicalization(self) -> None:
        plan_with_case_distinct_paths = ImplementationPlan(
            tasks=[
                ImplementationTask(
                    file_path="workspace_repo/A.py",
                    logical_task="upper",
                    atomic_tasks=[AtomicTask(atomic_task="upper")],
                ),
                ImplementationTask(
                    file_path="workspace_repo/a.py",
                    logical_task="lower",
                    atomic_tasks=[AtomicTask(atomic_task="lower")],
                ),
            ]
        )
        state = AgentState(
            implementation_plan=plan_with_case_distinct_paths,
            last_edit_result=EditResult(
                status=EditStatus.APPLIED,
                path="a.py",
            ),
            developer_status=DeveloperStatus.COMPLETED,
            verification_status=VerificationStatus.REGRESSION,
        )
        controller = VerificationController((), None, ".")

        with patch(
            "agent.developer.editing.os.path.normcase",
            side_effect=lambda path: path,
        ):
            update = controller.prepare_repair(state)

        self.assertEqual(
            update["repair_plan"].tasks[0].file_path,
            "workspace_repo/a.py",
        )

    def test_no_changes_cannot_hide_post_verification_failure(self) -> None:
        cases = (
            (
                VerificationCheckStatus.FAIL,
                VerificationStatus.REGRESSION,
            ),
            (
                VerificationCheckStatus.EXECUTION_ERROR,
                VerificationStatus.VERIFICATION_ERROR,
            ),
        )
        for post_status, expected_status in cases:
            with self.subTest(post_status=post_status):
                runner = _SequenceRunner(
                    VerificationCheckStatus.PASS,
                    post_status,
                )
                result = create_workflow_graph(
                    architect=lambda _: {"implementation_plan": plan()},
                    developer=lambda _: {
                        "developer_status": DeveloperStatus.NO_CHANGES
                    },
                    verification_specs=(
                        VerificationSpec(name="check", argv=("unused",)),
                    ),
                    verification_runner=runner,
                ).compile().invoke({})

                self.assertEqual(
                    result["verification_status"], expected_status
                )
                self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)

    def test_incomplete_developer_is_failed_before_graph_end(self) -> None:
        for developer_status in (
            DeveloperStatus.PENDING,
            DeveloperStatus.RUNNING,
        ):
            with self.subTest(developer_status=developer_status):
                runner = _SequenceRunner(
                    VerificationCheckStatus.PASS,
                    VerificationCheckStatus.PASS,
                )
                result = create_workflow_graph(
                    architect=lambda _: {"implementation_plan": plan()},
                    developer=lambda _: {"developer_status": developer_status},
                    verification_specs=(
                        VerificationSpec(name="check", argv=("unused",)),
                    ),
                    verification_runner=runner,
                ).compile().invoke({})

                self.assertEqual(
                    result["verification_status"], VerificationStatus.VERIFIED
                )
                self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)

    def test_all_non_repair_end_states_have_terminal_outcome(self) -> None:
        cases = (
            (DeveloperStatus.PENDING, WorkflowOutcome.FAILED),
            (DeveloperStatus.RUNNING, WorkflowOutcome.FAILED),
            (DeveloperStatus.FAILED, WorkflowOutcome.FAILED),
            (DeveloperStatus.NO_CHANGES, WorkflowOutcome.NO_CHANGES),
            (DeveloperStatus.COMPLETED, WorkflowOutcome.COMPLETED),
        )
        for developer_status, expected_outcome in cases:
            with self.subTest(developer_status=developer_status):
                runner = _SequenceRunner(
                    VerificationCheckStatus.PASS,
                    VerificationCheckStatus.PASS,
                )
                result = create_workflow_graph(
                    architect=lambda _: {"implementation_plan": plan()},
                    developer=lambda _, status=developer_status: {
                        "developer_status": status
                    },
                    verification_specs=(
                        VerificationSpec(name="check", argv=("unused",)),
                    ),
                    verification_runner=runner,
                ).compile().invoke({})

                self.assertEqual(result["outcome"], expected_outcome)
                self.assertIsNot(result["outcome"], WorkflowOutcome.PENDING)

    def test_rejected_edit_cannot_produce_successful_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            child = developer(root, lambda _: "invalid model response")

            result = run_parent(root, child, (file_check(root),))

            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["verification_status"], VerificationStatus.VERIFIED
            )
            self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")

    def test_noop_edit_cannot_produce_successful_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            child = developer(
                root,
                lambda _: search_replace("value = 1", "value = 1"),
            )

            result = run_parent(root, child, (file_check(root),))

            self.assertEqual(result["developer_status"], DeveloperStatus.FAILED)
            self.assertEqual(
                result["verification_status"], VerificationStatus.VERIFIED
            )
            self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)

    def test_developer_failure_cannot_produce_successful_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(
                "value = 1\n", encoding="utf-8", newline=""
            )

            result = create_workflow_graph(
                architect=lambda _: {"implementation_plan": plan()},
                developer=lambda _: {
                    "developer_status": DeveloperStatus.FAILED,
                    "developer_message": "developer failed",
                },
                verification_specs=(file_check(root),),
                verification_runner=VerificationRunner(root),
            ).compile().invoke({})

            self.assertEqual(
                result["verification_status"], VerificationStatus.VERIFIED
            )
            self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)

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
                root, developer(root, propose), (file_check(root),)
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

    def test_multi_file_repair_only_replays_regressed_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.py"
            second = root / "b.py"
            first.write_bytes(b"A = 1\r\n")
            second.write_text("B = 1\n", encoding="utf-8", newline="")
            initial_plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="./workspace_repo/a.py",
                        logical_task="update A",
                        atomic_tasks=[AtomicTask(atomic_task="update A")],
                    ),
                    ImplementationTask(
                        file_path="workspace_repo/b.py",
                        logical_task="update B",
                        atomic_tasks=[AtomicTask(atomic_task="update B")],
                    ),
                ]
            )
            calls: list[str] = []

            def propose(values):
                calls.append(values["file_path"])
                if values["file_path"] == "a.py":
                    return search_replace("A = 1", "A = 2")
                if not values["verification_feedback"]:
                    return search_replace("B = 1", "B = 'BROKEN'")
                return search_replace("B = 'BROKEN'", "B = 2")

            (root / "test_two_files.py").write_text(
                "from pathlib import Path\n"
                "def test_files():\n"
                "    assert 'BROKEN' not in Path('b.py').read_text(encoding='utf-8')\n",
                encoding="utf-8",
                newline="",
            )
            check = VerificationSpec(
                name="two-files",
                argv=(
                    getattr(sys, "_base_executable", sys.executable),
                    "-m",
                    "pytest",
                    "-q",
                    "--junitxml=report.xml",
                ),
                report_path="report.xml",
            )
            result = create_workflow_graph(
                architect=lambda _: {"implementation_plan": initial_plan},
                developer=developer(root, propose),
                verification_specs=(check,),
                verification_runner=VerificationRunner(root),
            ).compile().invoke({})
            first_after_initial_edit = b"A = 2\r\n"

            self.assertEqual(result["outcome"], WorkflowOutcome.COMPLETED)
            self.assertEqual(result["repair_attempts"], 1)
            self.assertEqual(calls, ["a.py", "b.py", "b.py"])
            self.assertEqual(first.read_bytes(), first_after_initial_edit)
            self.assertEqual(second.read_text(encoding="utf-8"), "B = 2\n")
            self.assertEqual(result["implementation_plan"], initial_plan)
            self.assertEqual(len(result["repair_plan"].tasks), 1)
            self.assertEqual(
                result["repair_plan"].tasks[0].file_path,
                "workspace_repo/b.py",
            )

    def test_feedback_marks_diagnostics_untrusted_and_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")

            def propose(values):
                feedback = values["verification_feedback"]
                if not feedback:
                    return search_replace("value = 1", "value = 'BROKEN'")
                post = feedback["post_results"][0]
                self.assertEqual(feedback["diagnostic_data_trust"], "untrusted")
                self.assertTrue(post["stdout_truncated"])
                self.assertTrue(post["stderr_truncated"])
                return search_replace("value = 'BROKEN'", "value = 2")

            (root / "test_noisy.py").write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "def test_noisy():\n"
                "    print('x' * 500)\n"
                "    print('y' * 500, file=sys.stderr)\n"
                "    assert 'BROKEN' not in Path('app.py').read_text()\n",
                encoding="utf-8",
                newline="",
            )
            noisy_check = VerificationSpec(
                name="noisy-check",
                argv=(
                    getattr(sys, "_base_executable", sys.executable),
                    "-m",
                    "pytest",
                    "-q",
                    "-s",
                    "--junitxml=report.xml",
                ),
                max_output_bytes=96,
                report_path="report.xml",
            )

            result = run_parent(
                root, developer(root, propose), (noisy_check,)
            )

            self.assertEqual(result["outcome"], WorkflowOutcome.COMPLETED)

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
                root, developer(root, propose), (file_check(root),)
            )

            self.assertEqual(
                result["verification_status"],
                VerificationStatus.REPAIR_EXHAUSTED,
            )
            self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)
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
                root, developer(root, propose), (file_check(root),)
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
            self.assertEqual(result["outcome"], WorkflowOutcome.UNVERIFIED)
            self.assertEqual(result["repair_attempts"], 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_trusted_allowed_failure_reaches_public_acceptance(self) -> None:
        runner = _SequenceRunner(
            VerificationCheckStatus.FAIL,
            VerificationCheckStatus.FAIL,
        )
        result = create_workflow_graph(
            architect=lambda _: {"implementation_plan": plan()},
            developer=lambda _: {"developer_status": DeveloperStatus.COMPLETED},
            verification_specs=(
                VerificationSpec(
                    name="check",
                    argv=("unused",),
                    allowed_failure_case_ids=("legacy",),
                ),
            ),
            verification_runner=runner,
        ).compile().invoke({})

        self.assertEqual(
            result["verification_status"],
            VerificationStatus.PRE_EXISTING_FAILURE,
        )
        self.assertTrue(result["acceptance"].accepted)
        summary_result = DurableRunResult(
            DurableRunStatus.COMPLETED,
            state=result,
        )
        self.assertEqual(run_exit_code(summary_result.summary), 0)

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

            result = run_parent(root, child, (file_check(root),))

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

            result = run_parent(root, child, (file_check(root),))

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
