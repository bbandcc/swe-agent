import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.architect.graph import (
    ArchitectRuntime,
    ResearchEvaluation,
    ResearchStep,
    create_architect_workflow,
)
from agent.common.entities import AtomicTask, ImplementationPlan, ImplementationTask
from agent.developer.editing import DeveloperEditExecutor
from agent.developer.graph import DeveloperRuntime, create_developer_workflow
from agent.editing import WorkspaceEditor
from agent.runtime import (
    BudgetErrorCode,
    BudgetSnapshot,
    DurableBudgetBoundary,
    ModelCallResult,
    UsageMeasurement,
    UsageStatus,
)
from agent.graph import create_workflow_graph
from agent.outcome import WorkflowOutcome
from agent.verification import (
    VerificationCheckStatus,
    VerificationResult,
    VerificationSpec,
)


def measured(value):
    return ModelCallResult(
        value=value,
        usage=UsageMeasurement(UsageStatus.PARTIAL, 1, 1, 2, None, None),
        response_digest="d" * 64,
    )


def ready_plan() -> ImplementationPlan:
    return ImplementationPlan(
        tasks=[
            ImplementationTask(
                file_path="app.py",
                logical_task="change value",
                atomic_tasks=[AtomicTask(atomic_task="change", additional_context="")],
            )
        ]
    )


class BudgetedGraphTests(unittest.TestCase):
    def test_architect_accounts_each_model_call_and_stops_at_n_plus_one(self) -> None:
        runtime = ArchitectRuntime(
            plan_next_step=lambda _: measured(ResearchStep(reasoning="r", hypothesis="h")),
            check_research_step=lambda _: measured(ResearchEvaluation(is_valid=True, reasoning="ok")),
            conduct_research=lambda _: measured(AIMessage(content="enough")),
            extract_implementation_plan=lambda _: measured(ready_plan()),
            load_codebase_structure=lambda: "app.py",
        )
        boundary = DurableBudgetBoundary("run", clock=lambda: 100.0)

        accepted = create_architect_workflow(
            runtime, research_tools=[], budget_boundary=boundary
        ).invoke(
            {
                "implementation_research_scratchpad": [HumanMessage(content="task")],
                "budget": BudgetSnapshot.create(
                    max_steps=4, max_cost_usd=None, deadline_at=200.0
                ),
            }
        )
        denied = create_architect_workflow(
            runtime, research_tools=[], budget_boundary=boundary
        ).invoke(
            {
                "implementation_research_scratchpad": [HumanMessage(content="task")],
                "budget": BudgetSnapshot.create(
                    max_steps=3, max_cost_usd=None, deadline_at=200.0
                ),
            }
        )

        self.assertEqual(accepted["budget"].steps_used, 4)
        self.assertEqual(accepted["implementation_plan"], ready_plan())
        self.assertEqual(denied["budget"].steps_used, 3)
        self.assertEqual(
            denied["runtime_error_code"], BudgetErrorCode.MAX_STEPS_EXCEEDED
        )

    def test_toolnode_batch_capacity_failure_executes_zero_tools(self) -> None:
        executed: list[str] = []

        @tool
        def inspect_file(path: str) -> str:
            """Inspect one test file."""
            executed.append(path)
            return path

        runtime = ArchitectRuntime(
            plan_next_step=lambda _: measured(ResearchStep(reasoning="r", hypothesis="h")),
            check_research_step=lambda _: measured(ResearchEvaluation(is_valid=True, reasoning="ok")),
            conduct_research=lambda _: measured(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "inspect_file", "args": {"path": "a.py"}, "id": "t1"},
                        {"name": "inspect_file", "args": {"path": "b.py"}, "id": "t2"},
                    ],
                )
            ),
            extract_implementation_plan=lambda _: self.fail("plan extraction should not run"),
            load_codebase_structure=lambda: "app.py",
        )

        result = create_architect_workflow(
            runtime,
            research_tools=[inspect_file],
            budget_boundary=DurableBudgetBoundary("run", clock=lambda: 100.0),
        ).invoke(
            {
                "implementation_research_scratchpad": [HumanMessage(content="task")],
                "budget": BudgetSnapshot.create(
                    max_steps=4, max_cost_usd=None, deadline_at=200.0
                ),
            }
        )

        self.assertEqual(executed, [])
        self.assertEqual(result["budget"].steps_used, 3)
        self.assertEqual(result["runtime_error_code"], BudgetErrorCode.MAX_STEPS_EXCEEDED)

    def test_each_model_requested_tool_call_consumes_one_step(self) -> None:
        executed: list[str] = []

        @tool
        def inspect_file(path: str) -> str:
            """Inspect one test file."""
            executed.append(path)
            return path

        research = iter(
            (
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "inspect_file", "args": {"path": "a.py"}, "id": "t1"},
                        {"name": "inspect_file", "args": {"path": "b.py"}, "id": "t2"},
                    ],
                ),
                AIMessage(content="enough"),
            )
        )
        runtime = ArchitectRuntime(
            plan_next_step=lambda _: measured(ResearchStep(reasoning="r", hypothesis="h")),
            check_research_step=lambda _: measured(ResearchEvaluation(is_valid=True, reasoning="ok")),
            conduct_research=lambda _: measured(next(research)),
            extract_implementation_plan=lambda _: measured(ready_plan()),
            load_codebase_structure=lambda: "app.py",
        )

        result = create_architect_workflow(
            runtime,
            research_tools=[inspect_file],
            budget_boundary=DurableBudgetBoundary("run", clock=lambda: 100.0),
        ).invoke(
            {
                "implementation_research_scratchpad": [HumanMessage(content="task")],
                "budget": BudgetSnapshot.create(
                    max_steps=7, max_cost_usd=None, deadline_at=200.0
                ),
            }
        )

        self.assertCountEqual(executed, ["a.py", "b.py"])
        self.assertEqual(result["budget"].steps_used, 7)

    def test_parser_failure_settles_returned_model_usage_before_stopping(self) -> None:
        failed = ModelCallResult(
            value=None,
            usage=UsageMeasurement(UsageStatus.PARTIAL, 3, 1, 4, None, None),
            response_digest="f" * 64,
            error="invalid structured output",
        )
        runtime = ArchitectRuntime(
            plan_next_step=lambda _: failed,
            check_research_step=lambda _: self.fail("must stop after parse failure"),
            conduct_research=lambda _: self.fail("must stop after parse failure"),
            extract_implementation_plan=lambda _: self.fail("must stop after parse failure"),
            load_codebase_structure=lambda: "app.py",
        )

        result = create_architect_workflow(
            runtime,
            research_tools=[],
            budget_boundary=DurableBudgetBoundary("run", clock=lambda: 100.0),
        ).invoke(
            {
                "implementation_research_scratchpad": [HumanMessage(content="task")],
                "budget": BudgetSnapshot.create(
                    max_steps=4, max_cost_usd=None, deadline_at=200.0
                ),
            }
        )

        self.assertEqual(result["budget"].steps_used, 1)
        self.assertEqual(result["budget"].usage[0].input_tokens, 3)
        self.assertEqual(result["runtime_error_code"], BudgetErrorCode.MODEL_OUTPUT_INVALID)

    def test_model_deadline_overrun_settles_usage_and_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            now = [100.0]
            executor = DeveloperEditExecutor(WorkspaceEditor(root))

            def propose(_):
                now[0] = 201.0
                return measured(
                    "<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE"
                )

            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: measured(AIMessage(content="ready")),
                propose_existing_file_edit=propose,
                propose_new_file=lambda _: self.fail("unexpected create"),
            )

            developer = create_developer_workflow(
                runtime,
                research_tools=[],
                budget_boundary=DurableBudgetBoundary(
                    "run", clock=lambda: now[0]
                ),
            )

            class Runner:
                def __init__(self) -> None:
                    self.calls = 0

                def run(self, spec):
                    self.calls += 1
                    return VerificationResult.create(
                        name=spec.name,
                        argv=spec.argv,
                        cwd=spec.cwd,
                        status=VerificationCheckStatus.PASS,
                        exit_code=0,
                    )

            runner = Runner()
            result = create_workflow_graph(
                architect=lambda _: {"implementation_plan": ready_plan()},
                developer=developer,
                verification_specs=(VerificationSpec("unit", ("unused",)),),
                verification_runner=runner,
                workspace_root=root,
                durable_runtime=True,
                clock=lambda: now[0],
            ).compile().invoke(
                {
                    "budget": BudgetSnapshot.create(
                        max_steps=4, max_cost_usd=None, deadline_at=200.0
                    ),
                }
            )

            self.assertEqual(
                result["runtime_error_code"], BudgetErrorCode.TIMEOUT_OVERRUN
            )
            self.assertEqual(result["budget"].steps_used, 2)
            self.assertEqual(len(result["budget"].usage), 2)
            self.assertEqual(runner.calls, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")

    def test_tool_deadline_overrun_settles_batch_and_stops_next_model(self) -> None:
        now = [100.0]
        executed: list[str] = []

        @tool
        def inspect_file(path: str) -> str:
            """Inspect one test file and cross the absolute deadline."""
            executed.append(path)
            now[0] = 201.0
            return path

        runtime = ArchitectRuntime(
            plan_next_step=lambda _: measured(
                ResearchStep(reasoning="r", hypothesis="h")
            ),
            check_research_step=lambda _: measured(
                ResearchEvaluation(is_valid=True, reasoning="ok")
            ),
            conduct_research=lambda _: measured(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "inspect_file",
                            "args": {"path": "app.py"},
                            "id": "tool-1",
                        }
                    ],
                )
            ),
            extract_implementation_plan=lambda _: self.fail(
                "deadline overrun must stop the next model"
            ),
            load_codebase_structure=lambda: "app.py",
        )

        result = create_architect_workflow(
            runtime,
            research_tools=[inspect_file],
            budget_boundary=DurableBudgetBoundary(
                "run", clock=lambda: now[0]
            ),
        ).invoke(
            {
                "implementation_research_scratchpad": [HumanMessage(content="task")],
                "budget": BudgetSnapshot.create(
                    max_steps=6, max_cost_usd=None, deadline_at=200.0
                ),
            }
        )

        self.assertEqual(executed, ["app.py"])
        self.assertEqual(
            result["runtime_error_code"], BudgetErrorCode.TIMEOUT_OVERRUN
        )
        self.assertEqual(result["budget"].steps_used, 4)
        self.assertEqual(len(result["budget"].usage), 4)

    def test_parent_seals_budget_failure_before_developer(self) -> None:
        boundary = DurableBudgetBoundary("run", clock=lambda: 100.0)
        architect = create_architect_workflow(
            ArchitectRuntime(
                plan_next_step=lambda _: measured(
                    ResearchStep(reasoning="r", hypothesis="h")
                ),
                check_research_step=lambda _: self.fail("max step must stop here"),
                conduct_research=lambda _: self.fail("max step must stop here"),
                extract_implementation_plan=lambda _: self.fail("max step must stop here"),
                load_codebase_structure=lambda: "app.py",
            ),
            research_tools=[],
            budget_boundary=boundary,
        )

        result = create_workflow_graph(
            architect=architect,
            developer=lambda _: self.fail("Developer must not run"),
            durable_runtime=True,
        ).compile().invoke(
            {
                "implementation_research_scratchpad": [HumanMessage(content="task")],
                "budget": BudgetSnapshot.create(
                    max_steps=1, max_cost_usd=None, deadline_at=200.0
                ),
            }
        )

        self.assertEqual(result["outcome"], WorkflowOutcome.FAILED)
        self.assertEqual(result["runtime_error_code"], BudgetErrorCode.MAX_STEPS_EXCEEDED)

    def test_developer_uses_incoming_budget_without_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            executor = DeveloperEditExecutor(WorkspaceEditor(root))
            runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: measured(AIMessage(content="ready")),
                propose_existing_file_edit=lambda _: measured(
                    "<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE"
                ),
                propose_new_file=lambda _: self.fail("unexpected create"),
            )
            controller = DurableBudgetBoundary("run", clock=lambda: 100.0)
            initial = BudgetSnapshot.create(
                max_steps=6, max_cost_usd=None, deadline_at=200.0
            )
            first = controller.controller.reserve_model(
                initial, run_id="run", request_digest="a" * 64, now=100.0
            )
            carried = controller.controller.settle(
                first.snapshot,
                (
                    UsageMeasurement(
                        UsageStatus.PARTIAL, 1, 1, 2, None, None
                    ).for_call(first.reservations[0].call_id),
                ),
            )

            result = create_developer_workflow(
                runtime, research_tools=[], budget_boundary=controller
            ).invoke({"implementation_plan": ready_plan(), "budget": carried})

            self.assertEqual(result["budget"].steps_used, 3)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "value = 2\n")

    def test_parent_architect_developer_repair_share_one_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            boundary = DurableBudgetBoundary("run", clock=lambda: 100.0)
            architect_runtime = ArchitectRuntime(
                plan_next_step=lambda _: measured(ResearchStep(reasoning="r", hypothesis="h")),
                check_research_step=lambda _: measured(ResearchEvaluation(is_valid=True, reasoning="ok")),
                conduct_research=lambda _: measured(AIMessage(content="enough")),
                extract_implementation_plan=lambda _: measured(ready_plan()),
                load_codebase_structure=lambda: "app.py",
            )
            executor = DeveloperEditExecutor(WorkspaceEditor(root))

            def propose(values):
                before = values["file_content"]
                after = "value = 0\n" if before == "value = 1\n" else "value = 2\n"
                return measured(
                    f"<<<<<<< SEARCH\n{before}=======\n{after}>>>>>>> REPLACE"
                )

            developer_runtime = DeveloperRuntime(
                edit_executor=lambda: executor,
                load_codebase_structure=lambda: "app.py",
                research_atomic_task=lambda _: measured(AIMessage(content="ready")),
                propose_existing_file_edit=propose,
                propose_new_file=lambda _: self.fail("unexpected create"),
            )

            class Runner:
                def __init__(self):
                    self.statuses = iter(
                        (
                            VerificationCheckStatus.PASS,
                            VerificationCheckStatus.FAIL,
                            VerificationCheckStatus.PASS,
                        )
                    )

                def run(self, spec):
                    status = next(self.statuses)
                    return VerificationResult.create(
                        name=spec.name,
                        argv=spec.argv,
                        cwd=spec.cwd,
                        status=status,
                        exit_code=0 if status is VerificationCheckStatus.PASS else 1,
                        stderr="regression" if status is VerificationCheckStatus.FAIL else "",
                    )

            architect = create_architect_workflow(
                architect_runtime, research_tools=[], budget_boundary=boundary
            )
            developer = create_developer_workflow(
                developer_runtime, research_tools=[], budget_boundary=boundary
            )
            spec = VerificationSpec("unit", ("python", "-m", "unittest"))

            result = create_workflow_graph(
                architect=architect,
                developer=developer,
                verification_specs=(spec,),
                verification_runner=Runner(),
                workspace_root=root,
                durable_runtime=True,
                clock=lambda: 100.0,
            ).compile().invoke(
                {
                    "implementation_research_scratchpad": [HumanMessage(content="task")],
                    "budget": BudgetSnapshot.create(
                        max_steps=8, max_cost_usd=None, deadline_at=200.0
                    ),
                }
            )

            self.assertEqual(result["budget"].steps_used, 8)
            self.assertEqual(result["repair_attempts"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")


if __name__ == "__main__":
    unittest.main()
