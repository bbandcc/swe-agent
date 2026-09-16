"""Injectable and lazily constructed dependencies for the Architect graph."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.output_parsers import JsonOutputParser

from agent.architect.models import ResearchEvaluation, ResearchStep
from agent.common.entities import ImplementationPlan
from agent.config import build_chat_model
from agent.config import ModelSettings
from agent.runtime.calls import capture_model_failure, capture_model_result
from agent.runtime.config import TokenPricing
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools
from agent.tools.write import get_files_structure
from helpers.prompts import markdown_to_prompt_template

RunnableInput = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArchitectRuntime:
    plan_next_step: Callable[[RunnableInput], ResearchStep]
    check_research_step: Callable[[RunnableInput], ResearchEvaluation]
    conduct_research: Callable[[RunnableInput], Any]
    extract_implementation_plan: Callable[[RunnableInput], ImplementationPlan]
    load_codebase_structure: Callable[[], str]


@lru_cache(maxsize=1)
def _plan_next_step_runnable():
    prompt = markdown_to_prompt_template(
        "agent/architect/prompts/plan_next_step_prompt.md"
    )
    return prompt | build_chat_model().with_structured_output(ResearchStep)


@lru_cache(maxsize=1)
def _check_research_runnable():
    prompt = markdown_to_prompt_template(
        "agent/architect/prompts/check_research_already_explored.md"
    )
    return prompt | build_chat_model().with_structured_output(ResearchEvaluation)


@lru_cache(maxsize=1)
def _conduct_research_runnable():
    prompt = markdown_to_prompt_template(
        "agent/architect/prompts/conduct_research_plan_prompt.md"
    )
    return prompt | build_chat_model().bind_tools(
        search_tools + codemap_tools
    )


@lru_cache(maxsize=1)
def _extract_implementation_runnable():
    prompt = markdown_to_prompt_template(
        "agent/architect/prompts/extract_implementation_plan.md"
    )
    return (
        prompt
        | build_chat_model()
        | JsonOutputParser(pydantic_object=ImplementationPlan)
    )


def default_architect_runtime() -> ArchitectRuntime:
    return ArchitectRuntime(
        plan_next_step=lambda values: _plan_next_step_runnable().invoke(values),
        check_research_step=lambda values: _check_research_runnable().invoke(
            values
        ),
        conduct_research=lambda values: _conduct_research_runnable().invoke(
            values
        ),
        extract_implementation_plan=lambda values: ImplementationPlan(
            **_extract_implementation_runnable().invoke(values)
        ),
        load_codebase_structure=_load_codebase_structure,
    )


def durable_architect_runtime(
    settings: ModelSettings,
    max_output_tokens: int,
    pricing: TokenPricing | None,
) -> ArchitectRuntime:
    """Build explicit model calls that retain raw usage before parsing."""
    model = build_chat_model(settings, max_output_tokens=max_output_tokens)
    plan = markdown_to_prompt_template(
        "agent/architect/prompts/plan_next_step_prompt.md"
    ) | model.with_structured_output(ResearchStep, include_raw=True)
    check = markdown_to_prompt_template(
        "agent/architect/prompts/check_research_already_explored.md"
    ) | model.with_structured_output(ResearchEvaluation, include_raw=True)
    research = markdown_to_prompt_template(
        "agent/architect/prompts/conduct_research_plan_prompt.md"
    ) | model.bind_tools(search_tools + codemap_tools)
    extract = markdown_to_prompt_template(
        "agent/architect/prompts/extract_implementation_plan.md"
    ) | model
    parser = JsonOutputParser(pydantic_object=ImplementationPlan)

    def invoke_structured(runnable, values):
        result = runnable.invoke(values)
        if result.get("parsing_error") is not None or result.get("parsed") is None:
            return capture_model_failure(
                result["raw"],
                pricing,
                result.get("parsing_error") or "Structured model output was empty.",
            )
        return capture_model_result(result["parsed"], result["raw"], pricing)

    def invoke_extract(values):
        raw = extract.invoke(values)
        try:
            parsed = ImplementationPlan(**parser.invoke(raw))
        except Exception as error:
            return capture_model_failure(raw, pricing, error)
        return capture_model_result(parsed, raw, pricing)

    def invoke_research(values):
        raw = research.invoke(values)
        return capture_model_result(raw, raw, pricing)

    return ArchitectRuntime(
        plan_next_step=lambda values: invoke_structured(plan, values),
        check_research_step=lambda values: invoke_structured(check, values),
        conduct_research=invoke_research,
        extract_implementation_plan=invoke_extract,
        load_codebase_structure=_load_codebase_structure,
    )


def _load_codebase_structure() -> str:
    result = get_files_structure.invoke({"directory": "."})
    return str(result.get("content", result))
