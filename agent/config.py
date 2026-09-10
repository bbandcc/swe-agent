"""Small environment-backed defaults shared by agent runtimes."""

import os

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


def anthropic_model_name() -> str:
    configured = os.environ.get("ANTHROPIC_MODEL", "").strip()
    return configured or DEFAULT_ANTHROPIC_MODEL
