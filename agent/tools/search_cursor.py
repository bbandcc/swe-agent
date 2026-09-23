"""Query-bound and evidence-checked search continuation cursors."""

from agent.tools.read_contract import (
    MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES,
    content_version,
    decode_cursor,
    stable_digest,
)
from agent.workspace import (
    WorkspaceAccessPolicy,
    WorkspacePathResolver,
    has_single_regular_file_link,
)


def search_cursor_version(values: dict[str, object]) -> str:
    return stable_digest(
        {
            "schema": 1,
            "directory": values.get("directory"),
            "search_term": values.get("search_term"),
            "context": values.get("context"),
            "next_candidate_index": values.get("next_candidate_index"),
            "next_line": values.get("next_line"),
            "next_file_path": values.get("next_file_path"),
            "next_file_offset": values.get("next_file_offset"),
            "next_line_number": values.get("next_line_number"),
            "line_open": values.get("line_open"),
            "line_reported": values.get("line_reported"),
            "file_evidence": values.get("file_evidence"),
            "directory_evidence": values.get("directory_evidence"),
        }
    )


def _cursor_evidence_is_current(
    resolver: WorkspacePathResolver,
    access_policy: WorkspaceAccessPolicy | None,
    file_evidence: list[list[str]],
    directory_evidence: list[list[str]],
) -> bool:
    for path, expected in file_evidence:
        if access_policy is not None and not access_policy.check_read(path).allowed:
            return False
        resolution = resolver.resolve_file(path)
        if (
            not resolution.ok
            or resolution.path is None
            or resolution.relative_path != path
            or not has_single_regular_file_link(resolution.path)
        ):
            return False
        try:
            if content_version(resolution.path) != expected:
                return False
        except OSError:
            return False
    for path, expected in directory_evidence:
        if access_policy is not None and not access_policy.check_read(path).allowed:
            return False
        resolution = resolver.resolve_directory(path)
        if (
            not resolution.ok
            or resolution.path is None
            or (resolution.relative_path or ".") != path
        ):
            return False
        try:
            if content_version(resolution.path) != expected:
                return False
        except OSError:
            return False
    return True


def parse_search_cursor(
    cursor: str,
    *,
    directory: str,
    search_term: str,
    context: int,
    resolver: WorkspacePathResolver,
    access_policy: WorkspaceAccessPolicy | None,
) -> tuple[dict[str, object] | None, str | None]:
    token = decode_cursor(cursor, "workspace_search")
    if token is None:
        return None, "invalid_cursor"
    if any(
        token.get(key) != expected
        for key, expected in (
            ("directory", directory),
            ("search_term", search_term),
            ("context", context),
        )
    ):
        return None, "stale_cursor"
    next_index = token.get("next_candidate_index")
    next_line = token.get("next_line")
    next_file_path = token.get("next_file_path")
    next_file_offset = token.get("next_file_offset")
    next_line_number = token.get("next_line_number")
    line_open = token.get("line_open")
    line_reported = token.get("line_reported")
    file_evidence = token.get("file_evidence")
    directory_evidence = token.get("directory_evidence")
    if (
        isinstance(next_index, bool)
        or not isinstance(next_index, int)
        or next_index < 0
        or isinstance(next_line, bool)
        or not isinstance(next_line, int)
        or not 0 <= next_line <= MAX_SEARCH_BYTES
        or (next_file_path is not None and not isinstance(next_file_path, str))
        or (
            next_file_offset is not None
            and (
                isinstance(next_file_offset, bool)
                or not isinstance(next_file_offset, int)
                or next_file_offset < 0
            )
        )
        or isinstance(next_line_number, bool)
        or not isinstance(next_line_number, int)
        or next_line_number < 1
        or not isinstance(line_open, bool)
        or not isinstance(line_reported, bool)
        or (next_file_offset is None and (line_open or line_reported or next_line_number != 1))
        or not isinstance(file_evidence, list)
        or len(file_evidence) > MAX_SEARCH_FILES
        or not isinstance(directory_evidence, list)
        or len(directory_evidence) > MAX_SEARCH_FILES + 1
        or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
            for item in (*file_evidence, *directory_evidence)
        )
    ):
        return None, "invalid_cursor"
    if next_file_offset is not None and (
        not isinstance(next_file_path, str)
        or not any(item[0] == next_file_path for item in file_evidence)
    ):
        return None, "invalid_cursor"
    if token.get("search_version") != search_cursor_version(token):
        return None, "invalid_cursor"
    if not _cursor_evidence_is_current(
        resolver,
        access_policy,
        file_evidence,
        directory_evidence,
    ):
        return None, "stale_cursor"
    if next_file_offset is not None:
        resolution = resolver.resolve_file(next_file_path)
        if (
            not resolution.ok
            or resolution.path is None
            or not has_single_regular_file_link(resolution.path)
        ):
            return None, "stale_cursor"
        try:
            if next_file_offset > resolution.path.stat(follow_symlinks=False).st_size:
                return None, "stale_cursor"
        except OSError:
            return None, "stale_cursor"
    return token, None
