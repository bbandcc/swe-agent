"""Small structured result helpers shared by workspace read tools."""

from agent.workspace import PathResolution


def tool_success(
    path: str,
    content: str,
    *,
    warnings: list[str] | None = None,
    skipped_files: list[str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {"ok": True, "path": path, "content": content}
    if warnings is not None:
        result["warnings"] = warnings
    if skipped_files is not None:
        result["skipped_files"] = skipped_files
    return result


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


def tool_access_denied(error_code: str, message: str) -> dict[str, object]:
    """Return a policy denial without echoing the protected path."""
    return {
        "ok": False,
        "path": None,
        "error_code": error_code,
        "message": message,
    }
