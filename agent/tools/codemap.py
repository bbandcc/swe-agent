"""Workspace-bound tree-sitter code inspection tools."""

from pathlib import Path

from langchain_core.tools import tool
from tree_sitter_languages import get_language, get_parser

from agent.tools.results import (
    tool_access_denied,
    tool_error,
    tool_rejection,
    tool_success,
)
from agent.workspace import current_workspace_access_policy, default_workspace_resolver

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def _read_code(file_path: Path) -> bytes:
    return file_path.read_bytes()


def _definitions(file_path: Path, display_path: str) -> str:
    language_name = _LANGUAGE_BY_SUFFIX.get(file_path.suffix.lower())
    if language_name is None:
        raise ValueError(f"Unsupported file type: {file_path.suffix or '<none>'}")
    language = get_language(language_name)
    parser = get_parser(language_name)
    code = _read_code(file_path)
    tree = parser.parse(code)
    query = language.query(
        """
        (class_definition
            name: (identifier) @name.definition.class
            body: (block
                (function_definition
                    name: (identifier) @name.definition.method
                    parameters: (parameters) @params.definition.method)?)
            @body.definition.class)

        (function_definition
            name: (identifier) @name.definition.function
            parameters: (parameters) @params.definition.function
            body: (block) @body.definition.function)
        """
    )
    captures = query.captures(tree.root_node)
    output_lines = [f"\n{display_path}:\n"]
    current_definition: dict[str, object] = {}
    in_class = False
    last_line_number = 0
    for node, tag in captures:
        current_line = node.start_point[0] + 1
        if last_line_number > 0 and current_line > last_line_number + 1:
            output_lines.append("...")
        if tag == "name.definition.class":
            in_class = True
            output_lines.append(
                f"{current_line}| class {node.text.decode('utf-8')}:"
            )
            last_line_number = current_line
        elif tag == "name.definition.method" and in_class:
            current_definition["method_name"] = node.text.decode("utf-8")
            current_definition["line"] = current_line
        elif tag == "params.definition.method" and in_class:
            line_number = int(current_definition["line"])
            output_lines.append(
                f"{line_number}|     def "
                f"{current_definition['method_name']}{node.text.decode('utf-8')}:"
            )
            last_line_number = line_number
        elif tag == "body.definition.method":
            line_number = node.start_point[0] + 1
            output_lines.append(f"{line_number}|         ...")
            last_line_number = line_number
        elif tag == "body.definition.class":
            in_class = False
        elif tag == "name.definition.function":
            current_definition["name"] = node.text.decode("utf-8")
            current_definition["line"] = current_line
        elif tag == "params.definition.function":
            line_number = int(current_definition["line"])
            output_lines.append(
                f"{line_number}| def "
                f"{current_definition['name']}{node.text.decode('utf-8')}:"
            )
            last_line_number = line_number
        elif tag == "body.definition.function":
            line_number = node.start_point[0] + 1
            output_lines.append(f"{line_number}|     ...")
            last_line_number = line_number
    return "\n".join(output_lines)


def _function_implementation(
    file_path: Path, display_path: str, function_name: str
) -> str | None:
    language_name = _LANGUAGE_BY_SUFFIX.get(file_path.suffix.lower())
    if language_name is None:
        raise ValueError(f"Unsupported file type: {file_path.suffix or '<none>'}")
    language = get_language(language_name)
    parser = get_parser(language_name)
    code = _read_code(file_path)
    tree = parser.parse(code)
    query = language.query(
        """
        (function_definition
            name: (identifier) @name.function
            parameters: (parameters) @params.function
            body: (block) @body.function)

        (class_definition
            body: (block
                (function_definition
                    name: (identifier) @name.method
                    parameters: (parameters) @params.method
                    body: (block) @body.method)))
        """
    )
    current: dict[str, object] = {}
    for node, tag in query.captures(tree.root_node):
        if tag in {"name.function", "name.method"}:
            if node.text.decode("utf-8") == function_name:
                current = {
                    "name": function_name,
                    "line": node.start_point[0] + 1,
                }
        elif tag in {"params.function", "params.method"} and current:
            current["params"] = node.text.decode("utf-8")
        elif tag in {"body.function", "body.method"} and current:
            body = code[node.start_byte : node.end_byte].decode("utf-8")
            start_line = int(current["line"])
            output = [
                f"\n{display_path}:\n",
                f"{start_line}| def {function_name}{current['params']}:",
            ]
            for index, line in enumerate(body.split("\n")):
                line_number = start_line + index + 1
                indent = (
                    "    "
                    if not line.strip()
                    else line[: len(line) - len(line.lstrip())]
                )
                output.append(f"{line_number}|{indent}{line.lstrip()}")
            return "\n".join(output)
    return None


def _resolve_file(file_path: str):
    return default_workspace_resolver().resolve_file(file_path)


def _read_denial(resolution):
    policy = current_workspace_access_policy()
    if policy is None or not resolution.ok:
        return None
    decision = policy.check_read(resolution.relative_path or resolution.requested_path)
    if decision.allowed:
        return None
    return tool_access_denied(decision.error_code.value, decision.message)


@tool(parse_docstring=True)
def get_code_definitions(file_path: str) -> dict[str, object]:
    """Extract function and class signatures from one source file.

    Args:
        file_path: Workspace file, relative or absolute within the workspace.
    """
    resolution = _resolve_file(file_path)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    denied = _read_denial(resolution)
    if denied is not None:
        return denied
    try:
        content = _definitions(
            resolution.path, resolution.relative_path or file_path
        )
    except ValueError as error:
        return tool_error(file_path, "unsupported_file_type", str(error))
    except UnicodeDecodeError:
        return tool_error(file_path, "encoding_error", "The file is not valid UTF-8.")
    except OSError as error:
        return tool_error(file_path, "read_failed", f"Could not read file: {error}")
    return tool_success(resolution.relative_path or file_path, content)


@tool(parse_docstring=True)
def get_function_implementation(
    file_path: str, function_name: str
) -> dict[str, object]:
    """Extract one function or method implementation from a source file.

    Args:
        file_path: Workspace file, relative or absolute within the workspace.
        function_name: Function or method name to find.
    """
    resolution = _resolve_file(file_path)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    denied = _read_denial(resolution)
    if denied is not None:
        return denied
    try:
        content = _function_implementation(
            resolution.path,
            resolution.relative_path or file_path,
            function_name,
        )
    except ValueError as error:
        return tool_error(file_path, "unsupported_file_type", str(error))
    except UnicodeDecodeError:
        return tool_error(file_path, "encoding_error", "The file is not valid UTF-8.")
    except OSError as error:
        return tool_error(file_path, "read_failed", f"Could not read file: {error}")
    if content is None:
        return tool_error(
            file_path,
            "definition_not_found",
            f"Function or method {function_name!r} was not found.",
        )
    return tool_success(resolution.relative_path or file_path, content)


@tool(parse_docstring=True)
def get_code_definitions_multi(file_paths: list[str]) -> dict[str, object]:
    """Extract definitions from several workspace files.

    Args:
        file_paths: Workspace files to inspect.
    """
    resolutions = [_resolve_file(file_path) for file_path in file_paths]
    for resolution in resolutions:
        if not resolution.ok:
            return tool_rejection(resolution)
        denied = _read_denial(resolution)
        if denied is not None:
            return denied
    contents: list[str] = []
    for resolution in resolutions:
        assert resolution.path is not None
        try:
            contents.append(
                _definitions(
                    resolution.path,
                    resolution.relative_path or resolution.requested_path,
                )
            )
        except ValueError as error:
            return tool_error(
                resolution.requested_path, "unsupported_file_type", str(error)
            )
        except UnicodeDecodeError:
            return tool_error(
                resolution.requested_path,
                "encoding_error",
                "The file is not valid UTF-8.",
            )
        except OSError as error:
            return tool_error(
                resolution.requested_path,
                "read_failed",
                f"Could not read file: {error}",
            )
    return tool_success("<multiple>", "\n".join(contents))


@tool(parse_docstring=True)
def get_raw_file_content(file_path: str) -> dict[str, object]:
    """Read one UTF-8 text file from the workspace.

    Args:
        file_path: Workspace file, relative or absolute within the workspace.
    """
    resolution = _resolve_file(file_path)
    if not resolution.ok or resolution.path is None:
        return tool_rejection(resolution)
    denied = _read_denial(resolution)
    if denied is not None:
        return denied
    try:
        content = resolution.path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return tool_error(file_path, "encoding_error", "The file is not valid UTF-8.")
    except OSError as error:
        return tool_error(file_path, "read_failed", f"Could not read file: {error}")
    return tool_success(resolution.relative_path or file_path, content)


codemap_tools = [
    get_code_definitions,
    get_function_implementation,
    get_code_definitions_multi,
    get_raw_file_content,
]
codemap_tools_map = {workspace_tool.name: workspace_tool for workspace_tool in codemap_tools}
