"""Pinned, per-task source snapshots and bounded patch evidence."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from agent.workspace.paths import (
    WorkspaceRootError,
    canonical_workspace_relative_path,
    canonical_roots_overlap,
    canonicalize_root_path,
)
from agent.workspace.policy import WorkspaceAccessPolicy


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_TREE_ENTRIES = 100_000
_MAX_WORKSPACE_BYTES = 512 * 1024 * 1024
_MAX_PATCH_FILE_BYTES = 16 * 1024 * 1024
_MAX_PATCH_TOTAL_BYTES = 256 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30.0


class TaskWorkspaceErrorCode(str, Enum):
    INVALID_TASK = "invalid_task"
    REVISION_NOT_FOUND = "revision_not_found"
    ARCHIVE_FAILED = "archive_failed"
    UNSUPPORTED_ENTRY = "unsupported_entry"
    SIZE_LIMIT = "size_limit"
    WRITE_DENIED = "write_denied"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True, slots=True)
class TaskWorkspaceFailure:
    code: TaskWorkspaceErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class PatchEntry:
    path: str
    operation: str
    before_sha256: str | None
    after_sha256: str
    patch: bytes
    patch_sha256: str
    patch_size: int


@dataclass(frozen=True, slots=True)
class PatchCollection:
    valid: bool
    entries: tuple[PatchEntry, ...] = ()
    failure: TaskWorkspaceFailure | None = None


@dataclass(frozen=True, slots=True)
class TaskWorkspaceResult:
    workspace: "TaskWorkspace | None"
    failure: TaskWorkspaceFailure | None = None

    @property
    def ready(self) -> bool:
        return self.workspace is not None and self.failure is None


@dataclass(slots=True)
class TaskWorkspace:
    """A disposable tree materialized from one immutable Git revision.

    The task root intentionally has no ``.git`` directory or indirection. The
    trusted host retains the pinned revision and baseline index for patch
    collection; the command sandbox receives access only to ``root``.
    """

    task_id: str
    revision: str
    root: Path
    policy: WorkspaceAccessPolicy
    _container_root: Path = field(repr=False)
    _runtime_root: Path = field(repr=False)
    _baseline_content_root: Path = field(repr=False)
    _baseline: dict[str, tuple[int, str, str]] = field(repr=False)
    _scope: dict[str, str] = field(repr=False)
    _cleaned: bool = field(default=False, repr=False)

    def collect_patch(self) -> PatchCollection:
        if self._cleaned or not self.root.is_dir():
            return _patch_failure(TaskWorkspaceErrorCode.CLEANUP_FAILED)
        files, failure = _scan_workspace(self.root)
        if failure is not None:
            return PatchCollection(False, failure=failure)
        changed_paths = set(files)
        changed_paths.update(set(self._baseline) - set(files))
        entries: list[PatchEntry] = []
        total_patch_bytes = 0

        for relative in sorted(changed_paths):
            before = self._baseline.get(relative)
            after_path = files.get(relative)
            if after_path is None:
                return _patch_failure(TaskWorkspaceErrorCode.WRITE_DENIED)
            content, read_failure = _read_limited_file(after_path)
            if read_failure is not None:
                return PatchCollection(False, failure=read_failure)
            after_digest = hashlib.sha256(content).hexdigest()
            if before is not None and before[1] == after_digest:
                continue

            operation = "create" if before is None else "modify"
            if self._scope.get(relative) != operation:
                return _patch_failure(TaskWorkspaceErrorCode.WRITE_DENIED)
            if not self.policy.check_write(relative).allowed:
                return _patch_failure(TaskWorkspaceErrorCode.WRITE_DENIED)

            old_content = b""
            before_digest = None
            if before is not None:
                before_digest = before[1]
                baseline_path = self._baseline_content_root.joinpath(
                    *PurePosixPath(before[2]).parts
                )
                old_content, git_failure = _read_limited_file(baseline_path)
                if git_failure is not None:
                    return PatchCollection(False, failure=git_failure)
                if hashlib.sha256(old_content).hexdigest() != before_digest:
                    return _patch_failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED)

            patch = b"".join(
                difflib.diff_bytes(
                    difflib.unified_diff,
                    old_content.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=("a/" + relative).encode("utf-8"),
                    tofile=("b/" + relative).encode("utf-8"),
                )
            )
            total_patch_bytes += len(patch)
            if total_patch_bytes > _MAX_PATCH_TOTAL_BYTES:
                return _patch_failure(TaskWorkspaceErrorCode.SIZE_LIMIT)
            entries.append(
                PatchEntry(
                    path=relative,
                    operation=operation,
                    before_sha256=before_digest,
                    after_sha256=after_digest,
                    patch=patch,
                    patch_sha256=hashlib.sha256(patch).hexdigest(),
                    patch_size=len(patch),
                )
            )
        return PatchCollection(True, tuple(entries))

    def cleanup(self) -> TaskWorkspaceFailure | None:
        if self._cleaned:
            return None
        if not _remove_container(self._container_root, self._runtime_root):
            return TaskWorkspaceFailure(
                TaskWorkspaceErrorCode.CLEANUP_FAILED,
                "Task workspace cleanup failed.",
            )
        self._cleaned = True
        return None


def prepare_task_workspace(
    *,
    repository_root: str | Path,
    task: Mapping[str, Any],
    runtime_root: str | Path,
    access_policy: WorkspaceAccessPolicy,
) -> TaskWorkspaceResult:
    """Materialize a pinned task tree without oracle paths or Git metadata."""
    if not isinstance(access_policy, WorkspaceAccessPolicy):
        return TaskWorkspaceResult(None, _failure(TaskWorkspaceErrorCode.INVALID_TASK))
    validated = _validate_task_shape(task)
    if isinstance(validated, TaskWorkspaceFailure):
        return TaskWorkspaceResult(None, validated)
    task_id, revision, scope, oracle_paths = validated
    policy = WorkspaceAccessPolicy(
        hidden_paths=access_policy.hidden_paths,
        oracle_paths=tuple(sorted(set((*access_policy.oracle_paths, *oracle_paths)))),
    )
    if any(
        not policy.check_write(path).allowed
        or any(_overlap(path, protected) for protected in policy.configured_paths)
        for path in scope
    ):
        return TaskWorkspaceResult(None, _failure(TaskWorkspaceErrorCode.INVALID_TASK))

    try:
        repository = canonicalize_root_path(repository_root)
        runtime = canonicalize_root_path(runtime_root, must_exist=False)
        if canonical_roots_overlap(repository, runtime):
            return TaskWorkspaceResult(None, _failure(TaskWorkspaceErrorCode.INVALID_TASK))
        Path(runtime_root).expanduser().mkdir(parents=True, exist_ok=True)
        runtime = canonicalize_root_path(runtime_root, must_exist=False)
        if canonical_roots_overlap(repository, runtime):
            return TaskWorkspaceResult(None, _failure(TaskWorkspaceErrorCode.INVALID_TASK))
    except (OSError, WorkspaceRootError, ValueError, TypeError):
        return TaskWorkspaceResult(
            None,
            TaskWorkspaceFailure(
                TaskWorkspaceErrorCode.INVALID_TASK,
                "Task or runtime root is invalid.",
            ),
        )

    if _git_exit(repository, ["cat-file", "-e", f"{revision}^{{commit}}"]):
        return TaskWorkspaceResult(
            None,
            TaskWorkspaceFailure(
                TaskWorkspaceErrorCode.REVISION_NOT_FOUND,
                "Pinned task revision is unavailable.",
            ),
        )

    try:
        container = Path(tempfile.mkdtemp(prefix="task-", dir=runtime))
        root = container / "workspace"
        baseline_root = container / "baseline"
        baseline_root.mkdir()
        baseline = _checkout_snapshot(
            repository, revision, root, baseline_root, policy, oracle_paths, scope
        )
        for path, operation in scope.items():
            if not policy.check_write(path).allowed:
                raise _materialize_failure(TaskWorkspaceErrorCode.INVALID_TASK)
            if (operation == "modify") != (path in baseline):
                raise _materialize_failure(TaskWorkspaceErrorCode.INVALID_TASK)
    except _MaterializeError as error:
        failure = error.failure
    except (OSError, subprocess.SubprocessError):
        failure = _failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED)
    except BaseException:
        if "container" in locals():
            _remove_container(container, runtime)
        raise
    else:
        failure = None

    if failure is not None:
        if "container" in locals() and not _remove_container(container, runtime):
            return TaskWorkspaceResult(
                None,
                _failure(TaskWorkspaceErrorCode.CLEANUP_FAILED),
            )
        return TaskWorkspaceResult(None, failure)

    if "container" not in locals():
        return TaskWorkspaceResult(
            None,
            _failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED),
        )

    workspace = TaskWorkspace(
        task_id=task_id,
        revision=revision,
        root=root,
        policy=policy,
        _container_root=container,
        _runtime_root=runtime,
        _baseline_content_root=baseline_root,
        _baseline=baseline,
        _scope=scope,
    )
    return TaskWorkspaceResult(workspace)


class _MaterializeError(Exception):
    def __init__(self, failure: TaskWorkspaceFailure):
        self.failure = failure


def _validate_task_shape(
    task: Mapping[str, Any],
) -> tuple[str, str, dict[str, str], tuple[str, ...]] | TaskWorkspaceFailure:
    if not isinstance(task, Mapping):
        return _invalid_task()
    task_id = task.get("task_id")
    target = task.get("target")
    revision = target.get("revision") if isinstance(target, Mapping) else None
    if (
        not isinstance(task_id, str)
        or not _TASK_ID.fullmatch(task_id)
        or not isinstance(revision, str)
        or not _REVISION.fullmatch(revision)
    ):
        return _invalid_task()

    scope_entries = task.get("allowed_edit_scope")
    if not isinstance(scope_entries, list) or not scope_entries:
        return _invalid_task()
    scope: dict[str, str] = {}
    for entry in scope_entries:
        if not isinstance(entry, Mapping):
            return _invalid_task()
        path = entry.get("path")
        operation = entry.get("operation")
        canonical = canonical_workspace_relative_path(path, allow_root=False) if isinstance(path, str) else None
        if (
            canonical is None
            or canonical != path
            or not isinstance(operation, str)
            or operation not in {"create", "modify"}
            or canonical in scope
        ):
            return _invalid_task()
        scope[canonical] = operation

    oracle = task.get("oracle")
    test_files = oracle.get("test_files") if isinstance(oracle, Mapping) else None
    if not isinstance(test_files, list) or not test_files:
        return _invalid_task()
    oracle_paths: list[str] = []
    for entry in test_files:
        path = entry.get("path") if isinstance(entry, Mapping) else None
        canonical = canonical_workspace_relative_path(path, allow_root=False) if isinstance(path, str) else None
        if canonical is None or canonical != path or canonical in oracle_paths:
            return _invalid_task()
        if any(_overlap(canonical, editable) for editable in scope):
            return _invalid_task()
        oracle_paths.append(canonical)
    return task_id, revision, scope, tuple(oracle_paths)


def _checkout_snapshot(
    repository: Path,
    revision: str,
    root: Path,
    baseline_root: Path,
    policy: WorkspaceAccessPolicy,
    oracle_paths: tuple[str, ...],
    scope: dict[str, str],
) -> dict[str, tuple[int, str, str]]:
    clone = subprocess.run(
        ["git", "clone", "--no-hardlinks", "--local", "--no-checkout", str(repository), str(root)],
        cwd=root.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
        shell=False,
    )
    if clone.returncode != 0:
        raise _materialize_failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED)
    for arguments in (
        ["config", "core.autocrlf", "false"],
        ["config", "core.eol", "lf"],
        ["checkout", "--quiet", "--detach", revision],
    ):
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            cwd=root.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            raise _materialize_failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED)
    actual_revision = _git_text(root, ["rev-parse", "HEAD"])
    if actual_revision != revision:
        raise _materialize_failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED)
    git_metadata = root / ".git"
    if git_metadata.is_symlink() or not git_metadata.is_dir():
        raise _materialize_failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
    shutil.rmtree(git_metadata)

    baseline: dict[str, tuple[int, str, str]] = {}
    total_bytes = 0
    entries = 0
    pending = [(root, PurePosixPath())]
    while pending:
        directory, relative_directory = pending.pop()
        with os.scandir(directory) as items:
            for item in items:
                entries += 1
                if entries > _MAX_TREE_ENTRIES:
                    raise _materialize_failure(TaskWorkspaceErrorCode.SIZE_LIMIT)
                raw_relative = str(relative_directory / item.name)
                canonical = canonical_workspace_relative_path(raw_relative, allow_root=False)
                if canonical is None or canonical in baseline:
                    raise _materialize_failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
                path = Path(item.path)
                if item.is_symlink() or _is_junction(path):
                    raise _materialize_failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
                if item.is_dir(follow_symlinks=False):
                    if policy.is_protected(canonical) or _contains_oracle(canonical, oracle_paths):
                        shutil.rmtree(path)
                    else:
                        pending.append((path, PurePosixPath(raw_relative)))
                    continue
                if not item.is_file(follow_symlinks=False):
                    raise _materialize_failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
                if policy.is_protected(canonical) or _overlaps_any(canonical, oracle_paths):
                    path.unlink()
                    continue
                metadata = path.stat(follow_symlinks=False)
                if metadata.st_nlink != 1:
                    raise _materialize_failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
                total_bytes += metadata.st_size
                if metadata.st_size > _MAX_PATCH_FILE_BYTES or total_bytes > _MAX_WORKSPACE_BYTES:
                    raise _materialize_failure(TaskWorkspaceErrorCode.SIZE_LIMIT)
                content, failure = _read_limited_file(path)
                if failure is not None:
                    raise _MaterializeError(failure)
                baseline[canonical] = (len(content), hashlib.sha256(content).hexdigest(), raw_relative)
                if scope.get(canonical) == "modify":
                    baseline_destination = baseline_root.joinpath(*PurePosixPath(raw_relative).parts)
                    baseline_destination.parent.mkdir(parents=True, exist_ok=True)
                    baseline_destination.write_bytes(content)
    return baseline


def _scan_workspace(root: Path) -> tuple[dict[str, Path], TaskWorkspaceFailure | None]:
    files: dict[str, Path] = {}
    pending = [(root, PurePosixPath())]
    entries = 0
    total_bytes = 0
    while pending:
        directory, relative_directory = pending.pop()
        try:
            iterator = os.scandir(directory)
            with iterator:
                for entry in iterator:
                    entries += 1
                    if entries > _MAX_TREE_ENTRIES:
                        return {}, _failure(TaskWorkspaceErrorCode.SIZE_LIMIT)
                    relative = str(relative_directory / entry.name)
                    canonical = canonical_workspace_relative_path(relative, allow_root=False)
                    if canonical is None or canonical in files:
                        return {}, _failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
                    path = Path(entry.path)
                    if entry.is_symlink() or _is_junction(path):
                        return {}, _failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((path, PurePosixPath(relative)))
                    elif entry.is_file(follow_symlinks=False):
                        metadata = path.stat(follow_symlinks=False)
                        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                            return {}, _failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
                        total_bytes += metadata.st_size
                        if metadata.st_size > _MAX_PATCH_FILE_BYTES or total_bytes > _MAX_WORKSPACE_BYTES:
                            return {}, _failure(TaskWorkspaceErrorCode.SIZE_LIMIT)
                        files[canonical] = path
                    else:
                        return {}, _failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
        except OSError:
            return {}, _failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED)
    return files, None


def _read_limited_file(path: Path) -> tuple[bytes, TaskWorkspaceFailure | None]:
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return b"", _failure(TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY)
        if metadata.st_size > _MAX_PATCH_FILE_BYTES:
            return b"", _failure(TaskWorkspaceErrorCode.SIZE_LIMIT)
        with path.open("rb") as stream:
            content = stream.read(_MAX_PATCH_FILE_BYTES + 1)
        if len(content) > _MAX_PATCH_FILE_BYTES:
            return b"", _failure(TaskWorkspaceErrorCode.SIZE_LIMIT)
        return content, None
    except OSError:
        return b"", _failure(TaskWorkspaceErrorCode.ARCHIVE_FAILED)


def _git_exit(repository: Path, arguments: list[str]) -> bool:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        ).returncode != 0
    except (OSError, subprocess.SubprocessError):
        return True


def _git_text(repository: Path, arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("ascii").strip()
    except UnicodeError:
        return None


def _is_junction(path: Path) -> bool:
    try:
        return bool(hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _remove_container(container: Path, runtime_root: Path) -> bool:
    try:
        if container.parent.resolve(strict=True) != runtime_root:
            return False
        if container.is_symlink() or _is_junction(container):
            return False
        metadata = container.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            return False
        shutil.rmtree(container)
        return not container.exists()
    except OSError:
        return False


def _overlap(first: str, second: str) -> bool:
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def _overlaps_any(path: str, values: tuple[str, ...]) -> bool:
    return any(_overlap(path, value) for value in values)


def _contains_oracle(path: str, values: tuple[str, ...]) -> bool:
    return any(path == value or path.startswith(value + "/") for value in values)


def _invalid_task() -> TaskWorkspaceFailure:
    return _failure(TaskWorkspaceErrorCode.INVALID_TASK)


def _failure(code: TaskWorkspaceErrorCode) -> TaskWorkspaceFailure:
    messages = {
        TaskWorkspaceErrorCode.INVALID_TASK: "Trusted task scope or pinned revision is invalid.",
        TaskWorkspaceErrorCode.REVISION_NOT_FOUND: "Pinned task revision is unavailable.",
        TaskWorkspaceErrorCode.ARCHIVE_FAILED: "Pinned task tree or workspace evidence is unavailable.",
        TaskWorkspaceErrorCode.UNSUPPORTED_ENTRY: "Task tree contains an unsupported or unsafe entry.",
        TaskWorkspaceErrorCode.SIZE_LIMIT: "Task tree or patch exceeds a fixed size limit.",
        TaskWorkspaceErrorCode.WRITE_DENIED: "Task changed a path outside its trusted edit scope.",
        TaskWorkspaceErrorCode.CLEANUP_FAILED: "Task workspace cleanup failed.",
    }
    return TaskWorkspaceFailure(code, messages[code])


def _patch_failure(code: TaskWorkspaceErrorCode) -> PatchCollection:
    return PatchCollection(False, failure=_failure(code))


def _materialize_failure(code: TaskWorkspaceErrorCode) -> _MaterializeError:
    return _MaterializeError(_failure(code))
