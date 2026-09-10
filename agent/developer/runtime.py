"""Injectable model and workspace dependencies for the Developer graph."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser

from agent.config import anthropic_model_name
from agent.developer.editing import DeveloperEditExecutor
from agent.editing import WorkspaceEditor
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools
from agent.tools.write import get_files_structure
from helpers.prompts import markdown_to_prompt_template

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
    return prompt | ChatAnthropic(model=anthropic_model_name()).bind_tools(
        search_tools + codemap_tools
    )


@lru_cache(maxsize=1)
def _edit_runnable():
    prompt = markdown_to_prompt_template(
        "agent/developer/prompts/create_diff_prompt.md"
    )
    return (
        prompt
        | ChatAnthropic(model=anthropic_model_name())
        | StrOutputParser()
    )


@lru_cache(maxsize=1)
def _create_runnable():
    prompt = markdown_to_prompt_template(
        "agent/developer/prompts/implement_new_file.md"
    )
    return (
        prompt
        | ChatAnthropic(model=anthropic_model_name())
        | StrOutputParser()
    )


def default_developer_runtime() -> DeveloperRuntime:
    return DeveloperRuntime(
        edit_executor=lambda: DeveloperEditExecutor(
            WorkspaceEditor("./workspace_repo")
        ),
        load_codebase_structure=_load_codebase_structure,
        research_atomic_task=lambda values: _research_runnable().invoke(values),
        propose_existing_file_edit=lambda values: _edit_runnable().invoke(values),
        propose_new_file=lambda values: _create_runnable().invoke(values),
    )


def _load_codebase_structure() -> str:
    result = get_files_structure.invoke({"directory": "."})
    return str(result.get("content", result))
