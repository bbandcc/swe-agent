"""Injectable and lazily constructed dependencies for the Architect graph."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.output_parsers import JsonOutputParser

from agent.architect.models import ResearchEvaluation, ResearchStep
from agent.common.workspace_tree_evidence import render_workspace_tree_evidence
from agent.common.entities import ImplementationPlan
from agent.config import build_chat_model
from agent.config import ModelSettings
from agent.runtime.calls import (
    ModelCallResult,
    capture_model_exception,
    capture_model_failure,
    capture_model_result,
    classify_model_exception,
)
from agent.runtime.config import (
    DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
    ModelRetryPolicy,
    TokenPricing,
)
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
    *,
    model_request_timeout_seconds: float = DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
    model_retry_policy: ModelRetryPolicy | None = None,
) -> ArchitectRuntime:
    """Build explicit model calls that retain raw usage before parsing."""
    plan_prompt = markdown_to_prompt_template(
        "agent/architect/prompts/plan_next_step_prompt.md"
    )
    check_prompt = markdown_to_prompt_template(
        "agent/architect/prompts/check_research_already_explored.md"
    )
    research_prompt = markdown_to_prompt_template(
        "agent/architect/prompts/conduct_research_plan_prompt.md"
    )
    extract_prompt = markdown_to_prompt_template(
        "agent/architect/prompts/extract_implementation_plan.md"
    )
    parser = JsonOutputParser(pydantic_object=ImplementationPlan)
    retry_policy = model_retry_policy or ModelRetryPolicy()

    def model_for(values):
        timeout = values.get(
            "_model_request_timeout_seconds", model_request_timeout_seconds
        )
        return build_chat_model(
            settings,
            max_output_tokens=max_output_tokens,
            request_timeout_seconds=timeout,
            max_retries=retry_policy.max_attempts - 1,
        )

    def invoke_model(call):
        try:
            return call()
        except Exception as error:
            error_code = classify_model_exception(error)
            if error_code is None:
                raise
            return capture_model_exception(error_code)

    def invoke_structured(prompt, schema, values):
        runnable = prompt | model_for(values).with_structured_output(
            schema, include_raw=True
        )
        result = invoke_model(lambda: runnable.invoke(values))
        if isinstance(result, ModelCallResult):
            return result
        if result.get("parsing_error") is not None or result.get("parsed") is None:
            return capture_model_failure(
                result["raw"],
                pricing,
                result.get("parsing_error") or "Structured model output was empty.",
            )
        return capture_model_result(result["parsed"], result["raw"], pricing)

    def invoke_extract(values):
        runnable = extract_prompt | model_for(values)
        raw = invoke_model(lambda: runnable.invoke(values))
        if isinstance(raw, ModelCallResult):
            return raw
        try:
            parsed = ImplementationPlan(**parser.invoke(raw))
        except Exception as error:
            return capture_model_failure(raw, pricing, error)
        return capture_model_result(parsed, raw, pricing)

    def invoke_research(values):
        runnable = research_prompt | model_for(values).bind_tools(
            search_tools + codemap_tools
        )
        raw = invoke_model(lambda: runnable.invoke(values))
        if isinstance(raw, ModelCallResult):
            return raw
        return capture_model_result(raw, raw, pricing)

    return ArchitectRuntime(
        plan_next_step=lambda values: invoke_structured(
            plan_prompt, ResearchStep, values
        ),
        check_research_step=lambda values: invoke_structured(
            check_prompt, ResearchEvaluation, values
        ),
        conduct_research=invoke_research,
        extract_implementation_plan=invoke_extract,
        load_codebase_structure=_load_codebase_structure,
    )


def _load_codebase_structure() -> str:
    result = get_files_structure.invoke({"directory": "."})
    return render_workspace_tree_evidence(result)
