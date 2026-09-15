import os
import unittest
from unittest.mock import patch

from langchain_anthropic import ChatAnthropic
from langchain_deepseek import ChatDeepSeek
from pydantic import SecretStr

from agent.config import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    ModelSettings,
    build_chat_model,
    model_settings,
)


class ModelConfigurationTests(unittest.TestCase):
    def test_uses_deepseek_flash_as_production_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = model_settings()

        self.assertEqual(settings.provider, "deepseek")
        self.assertEqual(settings.model, DEFAULT_DEEPSEEK_MODEL)
        self.assertEqual(settings.base_url, DEFAULT_DEEPSEEK_BASE_URL)
        self.assertIsNone(settings.api_key)

    def test_builds_deepseek_client_from_environment(self) -> None:
        environment = {
            "AGENT_MODEL_PROVIDER": "deepseek",
            "AGENT_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_API_KEY": "test-deepseek-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            model = build_chat_model(max_tokens=16, temperature=0)

        self.assertIsInstance(model, ChatDeepSeek)
        self.assertEqual(model.model, "deepseek-v4-flash")
        self.assertEqual(model.openai_api_base, DEFAULT_DEEPSEEK_BASE_URL)
        self.assertEqual(model.extra_body, {"thinking": {"type": "disabled"}})
        self.assertEqual(
            model.openai_api_key.get_secret_value(), "test-deepseek-key"
        )

    def test_retains_explicit_anthropic_provider(self) -> None:
        environment = {
            "AGENT_MODEL_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = model_settings()
            model = build_chat_model(max_tokens=16, temperature=0)

        self.assertEqual(settings.provider, "anthropic")
        self.assertEqual(settings.model, DEFAULT_ANTHROPIC_MODEL)
        self.assertIsNone(settings.base_url)
        self.assertIsInstance(model, ChatAnthropic)
        self.assertEqual(
            settings.api_key.get_secret_value(), "test-anthropic-key"
        )

    def test_rejects_unknown_provider(self) -> None:
        with patch.dict(
            os.environ, {"AGENT_MODEL_PROVIDER": "unknown"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "AGENT_MODEL_PROVIDER"):
                model_settings()

    def test_builds_from_explicit_settings_without_reading_environment(self) -> None:
        settings = ModelSettings(
            provider="deepseek",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key=SecretStr("explicit-key"),
        )

        with patch(
            "agent.config.model_settings",
            side_effect=AssertionError("environment must not be read"),
        ):
            model = build_chat_model(
                settings,
                max_output_tokens=321,
            )

        self.assertEqual(model.model, "deepseek-v4-flash")
        self.assertEqual(model.max_tokens, 321)
        self.assertEqual(
            model.openai_api_key.get_secret_value(), "explicit-key"
        )

    def test_explicit_settings_reject_unbound_model_options(self) -> None:
        settings = ModelSettings(
            provider="anthropic",
            model="claude-sonnet-4-6",
            base_url=None,
            api_key=None,
        )
        options = {
            "model": "other-model",
            "base_url": "https://example.invalid",
            "api_key": SecretStr("override-key"),
            "temperature": 0,
            "extra_body": {"thinking": {"type": "enabled"}},
            "max_tokens": 256,
            "streaming": True,
        }

        for name, value in options.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "semantic RunConfig"):
                    build_chat_model(
                        settings,
                        max_output_tokens=128,
                        **{name: value},
                    )

    def test_builds_explicit_anthropic_with_output_limit(self) -> None:
        settings = ModelSettings(
            provider="anthropic",
            model="claude-sonnet-4-6",
            base_url=None,
            api_key=SecretStr("explicit-anthropic-key"),
        )

        model = build_chat_model(
            settings,
            max_output_tokens=654,
        )

        self.assertIsInstance(model, ChatAnthropic)
        self.assertEqual(model.max_tokens, 654)

    def test_rejects_invalid_explicit_output_limit(self) -> None:
        settings = ModelSettings(
            provider="deepseek",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key=None,
        )
        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    build_chat_model(settings, max_output_tokens=value)

    def test_rejects_unknown_explicit_provider(self) -> None:
        settings = ModelSettings(
            provider="unsupported",  # type: ignore[arg-type]
            model="model",
            base_url=None,
            api_key=None,
        )

        with self.assertRaisesRegex(ValueError, "provider"):
            build_chat_model(settings, max_output_tokens=16)


if __name__ == "__main__":
    unittest.main()
