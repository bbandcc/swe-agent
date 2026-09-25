"""Injectable model and workspace dependencies for the Developer graph."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.output_parsers import StrOutputParser

from agent.common.workspace_tree_evidence import render_workspace_tree_evidence
from agent.config import build_chat_model
from agent.config import ModelSettings
from agent.developer.editing import DeveloperEditExecutor
from agent.editing import WorkspaceEditor
from agent.tools.codemap import codemap_tools
from agent.tools.search import search_tools
from agent.tools.write import get_files_structure
from agent.workspace import configured_workspace_root
from agent.workspace import WorkspaceAccessPolicy
from helpers.prompts import markdown_to_prompt_template
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
from agent.runtime.secrets import KnownSecretFilter

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
    model_request_timeout_seconds: float = DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
    model_retry_policy: ModelRetryPolicy | None = None,
    secret_filter: KnownSecretFilter | None = None,
    access_policy: WorkspaceAccessPolicy | None = None,
) -> DeveloperRuntime:
    """Build explicit Developer calls with raw usage accounting."""
    research_prompt = markdown_to_prompt_template(
        "agent/developer/prompts/get_clear_implementation_plan.md"
    )
    edit_prompt = markdown_to_prompt_template(
        "agent/developer/prompts/create_diff_prompt.md"
    )
    create_prompt = markdown_to_prompt_template(
        "agent/developer/prompts/implement_new_file.md"
    )
    parser = StrOutputParser()
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

    def invoke_message(prompt, values, *, tools: bool = False):
        model = model_for(values)
        runnable = prompt | (
            model.bind_tools(search_tools + codemap_tools) if tools else model
        )
        raw = invoke_model(lambda: runnable.invoke(values))
        if isinstance(raw, ModelCallResult):
            return raw
        return capture_model_result(raw, raw, pricing)

    def invoke_text(prompt, values):
        raw = invoke_model(lambda: (prompt | model_for(values)).invoke(values))
        if isinstance(raw, ModelCallResult):
            return raw
        try:
            parsed = parser.invoke(raw)
        except Exception as error:
            return capture_model_failure(raw, pricing, error)
        return capture_model_result(parsed, raw, pricing)

    return DeveloperRuntime(
        edit_executor=lambda: DeveloperEditExecutor(
            WorkspaceEditor(
                workspace_root,
                secret_filter=secret_filter,
                access_policy=access_policy,
            )
        ),
        load_codebase_structure=_load_codebase_structure,
        research_atomic_task=lambda values: invoke_message(
            research_prompt, values, tools=True
        ),
        propose_existing_file_edit=lambda values: invoke_text(edit_prompt, values),
        propose_new_file=lambda values: invoke_text(create_prompt, values),
    )


def _load_codebase_structure() -> str:
    result = get_files_structure.invoke({"directory": "."})
    return render_workspace_tree_evidence(result)
