"""Environment-backed model configuration shared by agent runtimes."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from pydantic import SecretStr

ModelProvider = Literal["deepseek", "anthropic"]

DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True, slots=True)
class ModelSettings:
    provider: ModelProvider
    model: str
    base_url: str | None
    api_key: SecretStr | None


def model_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelSettings:
    source = os.environ if environ is None else environ
    provider = source.get("AGENT_MODEL_PROVIDER", "deepseek").strip().lower()
    configured_model = source.get("AGENT_MODEL", "").strip()

    if provider == "deepseek":
        return ModelSettings(
            provider="deepseek",
            model=configured_model or DEFAULT_DEEPSEEK_MODEL,
            base_url=(
                source.get("AGENT_MODEL_BASE_URL", "").strip()
                or DEFAULT_DEEPSEEK_BASE_URL
            ),
            api_key=_secret_from_environment(source, "DEEPSEEK_API_KEY"),
        )
    if provider == "anthropic":
        return ModelSettings(
            provider="anthropic",
            model=configured_model or DEFAULT_ANTHROPIC_MODEL,
            base_url=(
                source.get("AGENT_MODEL_BASE_URL", "").strip() or None
            ),
            api_key=_secret_from_environment(source, "ANTHROPIC_API_KEY"),
        )
    raise ValueError(
        "AGENT_MODEL_PROVIDER must be either 'deepseek' or 'anthropic'"
    )


def build_chat_model(
    settings: ModelSettings | None = None,
    *,
    max_output_tokens: int | None = None,
    **options: Any,
) -> BaseChatModel:
    """Build an existing provider from explicit or legacy environment config."""
    explicit_settings = settings is not None
    if explicit_settings and options:
        names = ", ".join(sorted(options))
        raise ValueError(
            "Explicit ModelSettings only accept max_output_tokens because "
            f"other options are not bound by the semantic RunConfig: {names}"
        )
    if explicit_settings and max_output_tokens is None:
        raise ValueError(
            "Explicit ModelSettings require max_output_tokens from RunConfig."
        )
    settings = model_settings() if settings is None else settings
    if settings.provider not in {"deepseek", "anthropic"}:
        raise ValueError("Model provider must be deepseek or anthropic")
    if max_output_tokens is not None:
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if "max_tokens" in options:
            raise ValueError(
                "max_tokens and max_output_tokens cannot both be provided"
            )
        options["max_tokens"] = max_output_tokens
    connection: dict[str, Any] = {
        "model": settings.model,
        "base_url": settings.base_url,
    }
    if settings.api_key is not None:
        connection["api_key"] = settings.api_key
    if settings.provider == "deepseek" and "extra_body" not in options:
        connection["extra_body"] = {"thinking": {"type": "disabled"}}
    connection.update(options)
    if settings.provider == "deepseek":
        return ChatDeepSeek(**connection)
    return ChatAnthropic(**connection)


def _secret_from_environment(
    environ: Mapping[str, str], name: str
) -> SecretStr | None:
    value = environ.get(name, "").strip()
    return SecretStr(value) if value else None
