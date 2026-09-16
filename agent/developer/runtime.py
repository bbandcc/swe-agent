"""Injectable model and workspace dependencies for the Developer graph."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.output_parsers import StrOutputParser

from agent.config import build_chat_model
from agent.config import ModelSettings
from agent.developer.editing import DeveloperEditExecutor
from agent.editing import WorkspaceEditor
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools
from agent.tools.write import get_files_structure
from agent.workspace import configured_workspace_root
from helpers.prompts import markdown_to_prompt_template
from agent.runtime.calls import capture_model_failure, capture_model_result
from agent.runtime.config import TokenPricing

RunnableInput = dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeveloperRuntime:
    edit_executor: Callable[[], DeveloperEditExecutor]
    load_codebase_structure: Callable[[], str]
    research_atomic_task: Callable[[RunnableInput], Any]
    propose_existing_file_edit: Callable[[RunnableInput], str]
    propose_new_file: Callable[[RunnableInput], str]


@lru_cache(maxsize=1)
def _research_runnable():
    prompt = markdown_to_prompt_template(
        "agent/developer/prompts/get_clear_implementation_plan.md"
    )
    return prompt | build_chat_model().bind_tools(
        search_tools + codemap_tools
    )


@lru_cache(maxsize=1)
def _edit_runnable():
    prompt = markdown_to_prompt_template(
        "agent/developer/prompts/create_diff_prompt.md"
    )
    return (
        prompt
        | build_chat_model()
        | StrOutputParser()
    )


@lru_cache(maxsize=1)
def _create_runnable():
    prompt = markdown_to_prompt_template(
        "agent/developer/prompts/implement_new_file.md"
    )
    return (
        prompt
        | build_chat_model()
        | StrOutputParser()
    )


def default_developer_runtime() -> DeveloperRuntime:
    return DeveloperRuntime(
        edit_executor=lambda: DeveloperEditExecutor(
            WorkspaceEditor(configured_workspace_root())
        ),
        load_codebase_structure=_load_codebase_structure,
        research_atomic_task=lambda values: _research_runnable().invoke(values),
        propose_existing_file_edit=lambda values: _edit_runnable().invoke(values),
        propose_new_file=lambda values: _create_runnable().invoke(values),
    )


def durable_developer_runtime(
    settings: ModelSettings,
    max_output_tokens: int,
    pricing: TokenPricing | None,
    *,
    workspace_root,
) -> DeveloperRuntime:
    """Build explicit Developer calls with raw usage accounting."""
    model = build_chat_model(settings, max_output_tokens=max_output_tokens)
    research = markdown_to_prompt_template(
        "agent/developer/prompts/get_clear_implementation_plan.md"
    ) | model.bind_tools(search_tools + codemap_tools)
    edit = markdown_to_prompt_template(
        "agent/developer/prompts/create_diff_prompt.md"
    ) | model
    create = markdown_to_prompt_template(
        "agent/developer/prompts/implement_new_file.md"
    ) | model
    parser = StrOutputParser()

    def invoke_message(runnable, values):
        raw = runnable.invoke(values)
        return capture_model_result(raw, raw, pricing)

    def invoke_text(runnable, values):
        raw = runnable.invoke(values)
        try:
            parsed = parser.invoke(raw)
        except Exception as error:
            return capture_model_failure(raw, pricing, error)
        return capture_model_result(parsed, raw, pricing)

    return DeveloperRuntime(
        edit_executor=lambda: DeveloperEditExecutor(WorkspaceEditor(workspace_root)),
        load_codebase_structure=_load_codebase_structure,
        research_atomic_task=lambda values: invoke_message(research, values),
        propose_existing_file_edit=lambda values: invoke_text(edit, values),
        propose_new_file=lambda values: invoke_text(create, values),
    )


def _load_codebase_structure() -> str:
    result = get_files_structure.invoke({"directory": "."})
    return str(result.get("content", result))
