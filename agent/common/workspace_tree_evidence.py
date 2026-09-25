"""Render bounded workspace tree results for model-facing context."""

import json
from collections.abc import Mapping


def render_workspace_tree_evidence(result: Mapping[str, object]) -> str:
    """Keep tree completeness and structured failures visible without cursors."""
    lines = ["[UNTRUSTED WORKSPACE EVIDENCE]", "kind: workspace_tree"]
    is_success = result.get("ok") is True
    content = result.get("content")
    has_truncation_state = isinstance(result.get("truncated"), bool)

    if is_success and isinstance(content, str) and has_truncation_state:
        truncated = result["truncated"] is True
        status = "INCOMPLETE (TRUNCATED)" if truncated else "COMPLETE"
        lines.extend(
            [f"status: {status}", f"truncated: {str(truncated).lower()}"]
        )
        path = result.get("path")
        if isinstance(path, str):
            lines.append(f"path: {_json_text(path)}")
        content_hash = result.get("content_hash")
        if isinstance(content_hash, str):
            lines.append(f"content_hash: {content_hash}")
    else:
        error_code = result.get("error_code")
        message = result.get("message")
        error_code_text = (
            error_code if isinstance(error_code, str) else "invalid_tree_result"
        )
        message_text = (
            message
            if isinstance(message, str)
            else "Workspace tree evidence is unavailable."
        )
        lines.extend(
            [
                "status: FAILED",
                f"error_code: {_json_text(error_code_text)}",
                f"message: {_json_text(message_text)}",
            ]
        )

    for key in ("range", "warnings", "limits", "stale"):
        if key in result:
            lines.append(f"{key}: {_json_text(result[key])}")

    if is_success and isinstance(content, str) and has_truncation_state:
        continuation = result.get("continuation")
        available = isinstance(continuation, str) and bool(continuation)
        lines.append(f"continuation_available: {str(available).lower()}")
        lines.extend(["tree:", content])
    return "\n".join(lines)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = ["render_workspace_tree_evidence"]
