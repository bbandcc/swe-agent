"""Run small real-API checks against the configured production model."""

import os
import re
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel

from agent.config import build_chat_model, model_settings


class SmokeResult(BaseModel):
    status: Literal["SMOKE_OK"]


@tool
def smoke_probe(value: str) -> str:
    """Return a diagnostic value supplied by the model."""
    return value


def _load_local_environment() -> None:
    inherited = set(os.environ)
    for filename in (".env", ".env.local"):
        path = Path(filename)
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key = key.strip()
            if key not in inherited:
                os.environ[key] = value.strip().strip("'\"")


def main() -> int:
    _load_local_environment()
    settings = model_settings()
    if settings.api_key is None:
        key_name = (
            "DEEPSEEK_API_KEY"
            if settings.provider == "deepseek"
            else "ANTHROPIC_API_KEY"
        )
        print(f"SKIPPED: {key_name} is not configured.")
        return 2

    stage = "plain-response"
    try:
        model = build_chat_model(max_tokens=512, temperature=0)
        response = model.invoke("Reply with exactly: SMOKE_OK")
        if "SMOKE_OK" not in str(response.content):
            raise RuntimeError("plain response did not contain SMOKE_OK")

        stage = "tool-call"
        tool_response = model.bind_tools(
            [smoke_probe], tool_choice="smoke_probe"
        ).invoke("Call smoke_probe with value SMOKE_OK.")
        calls = getattr(tool_response, "tool_calls", [])
        if not calls or calls[0].get("name") != "smoke_probe":
            raise RuntimeError("forced tool call was not returned")

        stage = "structured-output"
        structured = model.with_structured_output(SmokeResult).invoke(
            "Return status SMOKE_OK."
        )
        if structured is None or structured.status != "SMOKE_OK":
            raise RuntimeError("structured output did not validate")
    except Exception as error:
        status = getattr(error, "status_code", "unknown")
        detail = _safe_error_detail(error)
        print(
            f"FAILED at {stage}: {settings.provider}/{settings.model} "
            f"raised {type(error).__name__} (status={status}). {detail}"
        )
        return 1

    print(
        f"PASSED: {settings.provider}/{settings.model} completed plain, "
        "tool-call, and structured-output requests."
    )
    return 0


def _safe_error_detail(error: Exception) -> str:
    text = str(getattr(error, "body", "") or error)
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)


if __name__ == "__main__":
    raise SystemExit(main())
