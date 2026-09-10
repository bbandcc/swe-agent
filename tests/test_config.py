import os
import unittest
from unittest.mock import patch

from agent.config import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
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

    def test_builds_deepseek_anthropic_client_from_environment(self) -> None:
        environment = {
            "AGENT_MODEL_PROVIDER": "deepseek",
            "AGENT_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_API_KEY": "test-deepseek-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            model = build_chat_model(max_tokens=16, temperature=0)

        self.assertEqual(model.model, "deepseek-v4-flash")
        self.assertEqual(model.anthropic_api_url, DEFAULT_DEEPSEEK_BASE_URL)
        self.assertEqual(
            model.anthropic_api_key.get_secret_value(), "test-deepseek-key"
        )

    def test_retains_explicit_anthropic_provider(self) -> None:
        environment = {
            "AGENT_MODEL_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = model_settings()

        self.assertEqual(settings.provider, "anthropic")
        self.assertEqual(settings.model, DEFAULT_ANTHROPIC_MODEL)
        self.assertIsNone(settings.base_url)
        self.assertEqual(
            settings.api_key.get_secret_value(), "test-anthropic-key"
        )

    def test_rejects_unknown_provider(self) -> None:
        with patch.dict(
            os.environ, {"AGENT_MODEL_PROVIDER": "unknown"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "AGENT_MODEL_PROVIDER"):
                model_settings()


if __name__ == "__main__":
    unittest.main()
