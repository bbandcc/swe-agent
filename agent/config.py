"""Environment-backed model configuration shared by agent runtimes."""

import os
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


def model_settings() -> ModelSettings:
    provider = os.environ.get("AGENT_MODEL_PROVIDER", "deepseek").strip().lower()
    configured_model = os.environ.get("AGENT_MODEL", "").strip()

    if provider == "deepseek":
        return ModelSettings(
            provider="deepseek",
            model=configured_model or DEFAULT_DEEPSEEK_MODEL,
            base_url=(
                os.environ.get("AGENT_MODEL_BASE_URL", "").strip()
                or DEFAULT_DEEPSEEK_BASE_URL
            ),
            api_key=_secret_from_environment("DEEPSEEK_API_KEY"),
        )
    if provider == "anthropic":
        return ModelSettings(
            provider="anthropic",
            model=configured_model or DEFAULT_ANTHROPIC_MODEL,
            base_url=(
                os.environ.get("AGENT_MODEL_BASE_URL", "").strip() or None
            ),
            api_key=_secret_from_environment("ANTHROPIC_API_KEY"),
        )
    raise ValueError(
        "AGENT_MODEL_PROVIDER must be either 'deepseek' or 'anthropic'"
    )


def build_chat_model(**options: Any) -> BaseChatModel:
    settings = model_settings()
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


def _secret_from_environment(name: str) -> SecretStr | None:
    value = os.environ.get(name, "").strip()
    return SecretStr(value) if value else None
