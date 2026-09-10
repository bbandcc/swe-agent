"""Perform one minimal real Anthropic request using production defaults."""

import os
from pathlib import Path

from langchain_anthropic import ChatAnthropic

from agent.config import anthropic_model_name


def _load_local_environment() -> None:
    for filename in (".env", ".env.local"):
        path = Path(filename)
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> int:
    _load_local_environment()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("SKIPPED: ANTHROPIC_API_KEY is not configured.")
        return 2
    model_name = anthropic_model_name()
    response = ChatAnthropic(
        model=model_name,
        max_tokens=16,
        temperature=0,
    ).invoke("Reply with exactly: SMOKE_OK")
    content = str(response.content).strip()
    if "SMOKE_OK" not in content:
        print(f"FAILED: {model_name} returned an unexpected response.")
        return 1
    print(f"PASSED: {model_name} completed a real API request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
