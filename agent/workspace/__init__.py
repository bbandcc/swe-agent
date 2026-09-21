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
    current_workspace_access_policy,
    workspace_access_scope,
    workspace_root_scope,
)
from agent.workspace.policy import (
    AccessDecision,
    WorkspaceAccessErrorCode,
    WorkspaceAccessPolicy,
    configured_workspace_access_policy,
    parse_configured_paths,
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
    "current_workspace_access_policy",
    "workspace_access_scope",
    "workspace_root_scope",
    "AccessDecision",
    "WorkspaceAccessErrorCode",
    "WorkspaceAccessPolicy",
    "configured_workspace_access_policy",
    "parse_configured_paths",
]
