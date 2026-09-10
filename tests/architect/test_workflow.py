import unittest

from langchain_core.messages import AIMessage, HumanMessage

from agent.architect.graph import (
    ArchitectRuntime,
    ResearchEvaluation,
    ResearchStep,
    create_architect_workflow,
)
from agent.common.entities import ImplementationPlan, PlanStatus


class ArchitectWorkflowTests(unittest.TestCase):
    def test_invalid_research_returns_to_planning_before_conducting(self) -> None:
        planned_steps = iter(
            [
                ResearchStep(reasoning="already checked", hypothesis="duplicate"),
                ResearchStep(reasoning="new evidence", hypothesis="fresh"),
            ]
        )
        evaluations = iter(
            [
                ResearchEvaluation(reasoning="duplicate", is_valid=False),
                ResearchEvaluation(reasoning="useful", is_valid=True),
            ]
        )
        runtime = ArchitectRuntime(
            plan_next_step=lambda _: next(planned_steps),
            check_research_step=lambda _: next(evaluations),
            conduct_research=lambda _: AIMessage(content="research complete"),
            extract_implementation_plan=lambda _: ImplementationPlan(
                status=PlanStatus.NO_CHANGES,
                no_change_reason="No implementation is required for this route test.",
                tasks=[],
            ),
            load_codebase_structure=lambda: "app.py",
        )

        events = list(
            create_architect_workflow(runtime, research_tools=[]).stream(
                {
                    "implementation_research_scratchpad": [
                        HumanMessage(content="inspect the project")
                    ]
                },
                stream_mode="updates",
            )
        )
        visited = [next(iter(event)) for event in events]

        self.assertEqual(
            visited[:5],
            [
                "come_up_with_research_next_step",
                "check_research_step",
                "come_up_with_research_next_step",
                "check_research_step",
                "conduct_research",
            ],
        )


if __name__ == "__main__":
    unittest.main()
