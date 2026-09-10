import os
import unittest
from unittest.mock import patch

from agent.config import DEFAULT_ANTHROPIC_MODEL, anthropic_model_name


class ModelConfigurationTests(unittest.TestCase):
    def test_uses_supported_default_and_allows_environment_override(self) -> None:
        self.assertEqual(DEFAULT_ANTHROPIC_MODEL, "claude-sonnet-4-6")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(anthropic_model_name(), DEFAULT_ANTHROPIC_MODEL)
        with patch.dict(
            os.environ, {"ANTHROPIC_MODEL": "claude-custom"}, clear=True
        ):
            self.assertEqual(anthropic_model_name(), "claude-custom")


if __name__ == "__main__":
    unittest.main()
