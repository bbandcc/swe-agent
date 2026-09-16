"""Shared workspace path boundary for file tools and editors."""

from agent.workspace.paths import (
    PathResolution,
    WorkspacePathErrorCode,
    WorkspacePathResolver,
    WorkspaceRootError,
    WorkspaceRootErrorCode,
    canonical_path_key,
    canonicalize_root_path,
    configured_workspace_root,
    default_workspace_resolver,
    workspace_root_scope,
)

__all__ = [
    "PathResolution",
    "WorkspacePathErrorCode",
    "WorkspacePathResolver",
    "WorkspaceRootError",
    "WorkspaceRootErrorCode",
    "canonical_path_key",
    "canonicalize_root_path",
    "configured_workspace_root",
    "default_workspace_resolver",
    "workspace_root_scope",
]
