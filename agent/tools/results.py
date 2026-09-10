"""Small structured result helpers shared by workspace read tools."""

from agent.workspace import PathResolution


def tool_success(path: str, content: str) -> dict[str, object]:
    return {"ok": True, "path": path, "content": content}


def tool_rejection(resolution: PathResolution) -> dict[str, object]:
    return {
        "ok": False,
        "path": resolution.requested_path,
        "error_code": (
            resolution.error_code.value if resolution.error_code else "path_invalid"
        ),
        "message": resolution.message,
    }


def tool_error(path: str, error_code: str, message: str) -> dict[str, object]:
    return {
        "ok": False,
        "path": path,
        "error_code": error_code,
        "message": message,
    }
