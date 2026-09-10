"""Shared workspace path boundary for file tools and editors."""

from agent.workspace.paths import (
    PathResolution,
    WorkspacePathErrorCode,
    WorkspacePathResolver,
    default_workspace_resolver,
)

__all__ = [
    "PathResolution",
    "WorkspacePathErrorCode",
    "WorkspacePathResolver",
    "default_workspace_resolver",
]
