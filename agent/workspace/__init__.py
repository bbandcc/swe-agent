"""Shared workspace path boundary for file tools and editors."""

from agent.workspace.paths import (
    PathResolution,
    WorkspacePathErrorCode,
    WorkspacePathResolver,
    configured_workspace_root,
    default_workspace_resolver,
)

__all__ = [
    "PathResolution",
    "WorkspacePathErrorCode",
    "WorkspacePathResolver",
    "configured_workspace_root",
    "default_workspace_resolver",
]
