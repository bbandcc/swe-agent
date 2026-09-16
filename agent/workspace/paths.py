"""Resolve untrusted paths against one configured workspace boundary."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from collections.abc import Iterator

_RUN_WORKSPACE_ROOT: ContextVar[Path | None] = ContextVar(
    "swe_agent_run_workspace_root", default=None
)


class WorkspacePathErrorCode(str, Enum):
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_INVALID = "workspace_invalid"
    PATH_INVALID = "path_invalid"
    PATH_NOT_FOUND = "path_not_found"
    EXPECTED_FILE = "expected_file"
    EXPECTED_DIRECTORY = "expected_directory"


class WorkspaceRootErrorCode(str, Enum):
    NOT_FOUND = "not_found"
    INVALID = "invalid"


class WorkspaceRootError(ValueError):
    """Failure to establish a canonical, link-free directory root."""

    def __init__(self, code: WorkspaceRootErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PathResolution:
    requested_path: str
    relative_path: str | None = None
    path: Path | None = None
    error_code: WorkspacePathErrorCode | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None and self.error_code is None


class WorkspacePathResolver:
    """Resolve files and directories without allowing link-based escapes."""

    def __init__(self, root: str | Path) -> None:
        self._configured_root = Path(root)

    def resolve_file(
        self,
        requested_path: str,
        *,
        must_exist: bool = True,
        allow_absolute: bool = True,
    ) -> PathResolution:
        return self._resolve(
            requested_path,
            expected_kind="file",
            must_exist=must_exist,
            allow_absolute=allow_absolute,
        )

    def resolve_directory(
        self,
        requested_path: str,
        *,
        must_exist: bool = True,
        allow_absolute: bool = True,
    ) -> PathResolution:
        return self._resolve(
            requested_path,
            expected_kind="directory",
            must_exist=must_exist,
            allow_absolute=allow_absolute,
        )

    def _resolve(
        self,
        requested_path: str,
        *,
        expected_kind: str,
        must_exist: bool,
        allow_absolute: bool,
    ) -> PathResolution:
        root = self._resolve_root(requested_path)
        if isinstance(root, PathResolution):
            return root

        relative = self._workspace_relative_path(
            requested_path, root, allow_absolute=allow_absolute
        )
        if relative is None:
            return self._error(
                requested_path,
                WorkspacePathErrorCode.PATH_INVALID,
                "The path must stay inside the configured workspace.",
            )

        current = root
        for part in relative.parts:
            current = current / part
            if _is_link_or_junction(current):
                return self._error(
                    requested_path,
                    WorkspacePathErrorCode.PATH_INVALID,
                    "Symbolic links and junctions are not valid workspace paths.",
                )

        candidate = root.joinpath(relative)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return self._error(
                requested_path,
                WorkspacePathErrorCode.PATH_INVALID,
                "The workspace path could not be resolved safely.",
            )
        if not resolved.is_relative_to(root):
            return self._error(
                requested_path,
                WorkspacePathErrorCode.PATH_INVALID,
                "The path must stay inside the configured workspace.",
            )
        if must_exist and not resolved.exists():
            return self._error(
                requested_path,
                WorkspacePathErrorCode.PATH_NOT_FOUND,
                "The workspace path does not exist.",
            )
        if resolved.exists():
            if expected_kind == "file" and not resolved.is_file():
                return self._error(
                    requested_path,
                    WorkspacePathErrorCode.EXPECTED_FILE,
                    "The workspace path is not a regular file.",
                )
            if expected_kind == "directory" and not resolved.is_dir():
                return self._error(
                    requested_path,
                    WorkspacePathErrorCode.EXPECTED_DIRECTORY,
                    "The workspace path is not a directory.",
                )

        relative_string = relative.as_posix() or "."
        return PathResolution(
            requested_path=requested_path,
            relative_path=relative_string,
            path=resolved,
        )

    def _resolve_root(self, requested_path: str) -> Path | PathResolution:
        try:
            return canonicalize_root_path(self._configured_root)
        except WorkspaceRootError as error:
            error_code = (
                WorkspacePathErrorCode.WORKSPACE_NOT_FOUND
                if error.code is WorkspaceRootErrorCode.NOT_FOUND
                else WorkspacePathErrorCode.WORKSPACE_INVALID
            )
            return self._error(
                requested_path,
                error_code,
                str(error),
            )

    def _workspace_relative_path(
        self, requested_path: str, root: Path, *, allow_absolute: bool
    ) -> Path | None:
        if not requested_path or "\x00" in requested_path:
            return None
        normalized = requested_path.replace("\\", "/")
        posix = PurePosixPath(normalized)
        windows = PureWindowsPath(normalized)
        requested = Path(requested_path)
        if requested.is_absolute() or windows.is_absolute() or windows.drive:
            if not allow_absolute:
                return None
            try:
                absolute = requested.absolute()
                return absolute.relative_to(root)
            except (OSError, ValueError):
                return None
        if ".." in posix.parts:
            return None

        parts = list(posix.parts)
        if parts and parts[0] == "workspace_repo":
            parts.pop(0)
        return Path(*parts) if parts else Path()

    @staticmethod
    def _error(
        requested_path: str,
        error_code: WorkspacePathErrorCode,
        message: str,
    ) -> PathResolution:
        return PathResolution(
            requested_path=requested_path,
            error_code=error_code,
            message=message,
        )


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )
    except OSError:
        return True


def canonicalize_root_path(
    root: str | Path,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve an absolute directory whose root entry is not a link."""
    if isinstance(root, str) and not root.strip():
        raise WorkspaceRootError(
            WorkspaceRootErrorCode.INVALID,
            "The configured root must be a non-empty path.",
        )
    try:
        configured = Path(root).expanduser().absolute()
    except (OSError, RuntimeError, TypeError) as error:
        raise WorkspaceRootError(
            WorkspaceRootErrorCode.INVALID,
            "The configured root path is invalid.",
        ) from error
    if _is_link_or_junction(configured):
        raise WorkspaceRootError(
            WorkspaceRootErrorCode.INVALID,
            "The configured root itself must not be a symlink or junction.",
        )
    if must_exist and not configured.exists():
        raise WorkspaceRootError(
            WorkspaceRootErrorCode.NOT_FOUND,
            "The configured workspace root does not exist.",
        )
    if configured.exists() and not configured.is_dir():
        raise WorkspaceRootError(
            WorkspaceRootErrorCode.INVALID,
            "The configured root must be a directory.",
        )
    try:
        return configured.resolve(strict=must_exist)
    except (OSError, RuntimeError) as error:
        raise WorkspaceRootError(
            WorkspaceRootErrorCode.INVALID,
            "The configured root could not be resolved safely.",
        ) from error


def canonical_path_key(path: str | Path) -> str:
    """Return the S1 cross-platform comparison key for a canonical path."""
    return os.path.normcase(str(path))


def default_workspace_resolver() -> WorkspacePathResolver:
    return WorkspacePathResolver(configured_workspace_root())


def configured_workspace_root() -> Path:
    """Return the single workspace root used by default read and write paths."""
    runtime_root = _RUN_WORKSPACE_ROOT.get()
    if runtime_root is not None:
        return runtime_root
    return Path(os.environ.get("SWE_AGENT_WORKSPACE", "./workspace_repo"))


@contextmanager
def workspace_root_scope(root: str | Path) -> Iterator[None]:
    """Bind default tools to one explicit run workspace without global mutation."""
    token = _RUN_WORKSPACE_ROOT.set(Path(root))
    try:
        yield
    finally:
        _RUN_WORKSPACE_ROOT.reset(token)
