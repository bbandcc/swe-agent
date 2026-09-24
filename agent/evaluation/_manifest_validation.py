"""Schema and Git evidence checks for the public task-manifest contract."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Mapping

from agent.evaluation.task_manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestIssue,
    ManifestIssueCode,
    ManifestValidationResult,
    manifest_content_hash,
)
from agent.evaluation._manifest_git import _git_output, _git_text, _repo_blob
from agent.workspace.paths import canonical_workspace_relative_path


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UNITTEST_MODULE_RE = re.compile(r"^tests(?:\.[a-z][a-z0-9_]*)+$")
_SPLITS = {"train", "dev", "holdout"}


def validate_task_manifest(
    document: object,
    *,
    repository_root: Path,
    expected_repository: str,
    expected_environment: Mapping[str, str],
) -> ManifestValidationResult:
    """Validate a trusted S5a manifest without running any task command."""
    if not isinstance(document, dict):
        return _invalid_result(
            ManifestIssueCode.INVALID_DOCUMENT,
            "$",
            "Manifest root must be an object.",
        )
    if any(not isinstance(key, str) for key in document):
        return _invalid_result(
            ManifestIssueCode.INVALID_DOCUMENT,
            "$",
            "Manifest object keys must be strings.",
        )

    issues: list[ManifestIssue] = []
    manifest_id = document.get("manifest_id")
    _check_keys(
        document,
        required={"schema_version", "manifest_id", "manifest_sha256", "tasks"},
        allowed={"schema_version", "manifest_id", "manifest_sha256", "tasks"},
        location="$",
        issues=issues,
    )
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        _issue(issues, ManifestIssueCode.INVALID_VALUE, "$/schema_version", "Unsupported manifest schema version.")
    if not _valid_identifier(manifest_id):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, "$/manifest_id", "Manifest id is invalid.")
        manifest_id = None

    supplied_manifest_hash = document.get("manifest_sha256")
    if not _valid_sha256(supplied_manifest_hash):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, "$/manifest_sha256", "Manifest hash must be lowercase SHA-256.")
    else:
        try:
            actual_hash = manifest_content_hash(document)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            actual_hash = None
        if actual_hash != supplied_manifest_hash:
            _issue(issues, ManifestIssueCode.MANIFEST_HASH_MISMATCH, "$/manifest_sha256", "Manifest content hash does not match.")

    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        _issue(issues, ManifestIssueCode.INVALID_VALUE, "$/tasks", "Manifest must contain at least one task.")
        tasks = []

    seen_ids: set[str] = set()
    task_ids: list[str] = []
    repo_root = Path(repository_root)
    commit_cache: dict[str, bool] = {}
    blob_cache: dict[tuple[str, str], bytes | None] = {}
    change_cache: dict[tuple[str, str], set[str] | None] = {}
    for index, task in enumerate(tasks):
        location = f"$/tasks/{index}"
        if not isinstance(task, dict):
            _issue(issues, ManifestIssueCode.INVALID_VALUE, location, "Task must be an object.")
            continue
        task_id = task.get("task_id")
        if not _valid_identifier(task_id):
            _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/task_id", "Task id is invalid.")
        else:
            task_ids.append(task_id)
            if task_id in seen_ids:
                _issue(issues, ManifestIssueCode.DUPLICATE_TASK_ID, f"{location}/task_id", "Task ids must be unique.")
            seen_ids.add(task_id)
        _validate_task(
            task,
            location=location,
            repository_root=repo_root,
            expected_repository=expected_repository,
            expected_environment=expected_environment,
            issues=issues,
            commit_cache=commit_cache,
            blob_cache=blob_cache,
            change_cache=change_cache,
        )

    return ManifestValidationResult(
        valid=not issues,
        manifest_id=manifest_id if isinstance(manifest_id, str) else None,
        task_ids=tuple(task_ids),
        issues=tuple(issues),
    )


def _validate_task(
    task: dict[str, Any],
    *,
    location: str,
    repository_root: Path,
    expected_repository: str,
    expected_environment: Mapping[str, str],
    issues: list[ManifestIssue],
    commit_cache: dict[str, bool],
    blob_cache: dict[tuple[str, str], bytes | None],
    change_cache: dict[tuple[str, str], set[str] | None],
) -> None:
    required = {
        "task_id", "task_text", "task_text_sha256", "target", "allowed_edit_scope",
        "checks", "oracle", "environment", "budget", "known_environment_failures",
        "split", "stratum", "provenance",
    }
    _check_keys(task, required=required, allowed=required, location=location, issues=issues)

    task_text = task.get("task_text")
    text_hash = task.get("task_text_sha256")
    if not isinstance(task_text, str) or not task_text.strip() or not _valid_sha256(text_hash):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/task_text", "Task text and its hash are required.")
    else:
        try:
            actual_task_hash = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
        except UnicodeError:
            actual_task_hash = None
        if actual_task_hash != text_hash:
            _issue(issues, ManifestIssueCode.TASK_HASH_MISMATCH, f"{location}/task_text_sha256", "Task text hash does not match.")

    split = task.get("split")
    if not isinstance(split, str) or split not in _SPLITS:
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/split", "Task split must be train, dev, or holdout.")
    if not _nonempty_text(task.get("stratum")):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/stratum", "Task stratum is required.")
    failures = task.get("known_environment_failures")
    if not isinstance(failures, list) or any(not _nonempty_text(item) for item in failures):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/known_environment_failures", "Known environment failures must be a list of descriptions.")

    environment = task.get("environment")
    _validate_environment(environment, expected_environment, f"{location}/environment", issues)
    _validate_budget(task.get("budget"), f"{location}/budget", issues)

    target = task.get("target")
    target_revision: str | None = None
    if not isinstance(target, dict):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/target", "Target must identify repository and revision.")
    else:
        _check_keys(
            target,
            required={"repo", "revision"},
            allowed={"repo", "revision"},
            location=f"{location}/target",
            issues=issues,
        )
        if target.get("repo") != expected_repository:
            _issue(issues, ManifestIssueCode.REPOSITORY_MISMATCH, f"{location}/target/repo", "Target repository does not match the trusted repository.")
        target_revision = _validate_revision(
            target.get("revision"),
            f"{location}/target/revision",
            repository_root,
            issues,
            commit_cache,
        )
        if target_revision is not None and isinstance(environment, dict):
            lock_path = _canonical_path(
                environment.get("lock_path"),
                f"{location}/environment/lock_path",
                issues,
            )
            lock_hash = environment.get("lock_sha256")
            if lock_path is not None and _valid_sha256(lock_hash):
                lock_blob = _repo_blob(
                    repository_root, target_revision, lock_path, blob_cache
                )
                if lock_blob is None or hashlib.sha256(lock_blob).hexdigest() != lock_hash:
                    _issue(issues, ManifestIssueCode.ENVIRONMENT_MISMATCH, f"{location}/environment/lock_sha256", "Dependency lock does not match the pinned target revision.")

    oracle = task.get("oracle")
    oracle_revision: str | None = None
    oracle_paths: list[str] = []
    if not isinstance(oracle, dict):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/oracle", "Oracle must identify pinned test files.")
    else:
        _check_keys(
            oracle,
            required={"revision", "test_files", "visibility"},
            allowed={"revision", "test_files", "visibility"},
            location=f"{location}/oracle",
            issues=issues,
        )
        oracle_revision = _validate_revision(
            oracle.get("revision"),
            f"{location}/oracle/revision",
            repository_root,
            issues,
            commit_cache,
        )
        if oracle.get("visibility") != "evaluator_only_declared":
            _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/oracle/visibility", "Oracle must be declared evaluator-only.")
        test_files = oracle.get("test_files")
        if not isinstance(test_files, list) or not test_files:
            _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/oracle/test_files", "At least one oracle test file is required.")
        else:
            seen_paths: set[str] = set()
            for file_index, item in enumerate(test_files):
                file_location = f"{location}/oracle/test_files/{file_index}"
                if not isinstance(item, dict):
                    _issue(issues, ManifestIssueCode.INVALID_VALUE, file_location, "Oracle file entry must be an object.")
                    continue
                _check_keys(
                    item,
                    required={"path", "sha256"},
                    allowed={"path", "sha256"},
                    location=file_location,
                    issues=issues,
                )
                path = _canonical_path(item.get("path"), f"{file_location}/path", issues)
                if path is None:
                    continue
                if path in seen_paths:
                    _issue(issues, ManifestIssueCode.INVALID_PATH, f"{file_location}/path", "Oracle paths must be unique after canonicalization.")
                seen_paths.add(path)
                oracle_paths.append(path)
                expected_hash = item.get("sha256")
                if not _valid_sha256(expected_hash):
                    _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{file_location}/sha256", "Oracle file hash must be lowercase SHA-256.")
                elif oracle_revision is not None:
                    blob = _repo_blob(repository_root, oracle_revision, path, blob_cache)
                    if blob is None:
                        _issue(issues, ManifestIssueCode.ORACLE_FILE_MISSING, f"{file_location}/path", "Pinned oracle file is absent from its revision.")
                    elif hashlib.sha256(blob).hexdigest() != expected_hash:
                        _issue(issues, ManifestIssueCode.ORACLE_HASH_MISMATCH, f"{file_location}/sha256", "Pinned oracle file hash does not match its revision.")

    scope_paths = _validate_scope(
        task.get("allowed_edit_scope"),
        location=f"{location}/allowed_edit_scope",
        target_revision=target_revision,
        oracle_revision=oracle_revision,
        repository_root=repository_root,
        issues=issues,
        blob_cache=blob_cache,
        change_cache=change_cache,
    )
    for scope_path in scope_paths:
        if any(_paths_overlap(scope_path, oracle_path) for oracle_path in oracle_paths):
            _issue(issues, ManifestIssueCode.SCOPE_ORACLE_OVERLAP, f"{location}/allowed_edit_scope", "Editable scope overlaps a hidden oracle path.")

    _validate_checks(
        task.get("checks"),
        location=f"{location}/checks",
        target_revision=target_revision,
        oracle_revision=oracle_revision,
        oracle_paths=set(oracle_paths),
        repository_root=repository_root,
        issues=issues,
        commit_cache=commit_cache,
        blob_cache=blob_cache,
    )
    _validate_provenance(
        task.get("provenance"),
        location=f"{location}/provenance",
        oracle_revision=oracle_revision,
        repository_root=repository_root,
        issues=issues,
    )


def _validate_scope(
    value: object,
    *,
    location: str,
    target_revision: str | None,
    oracle_revision: str | None,
    repository_root: Path,
    issues: list[ManifestIssue],
    blob_cache: dict[tuple[str, str], bytes | None],
    change_cache: dict[tuple[str, str], set[str] | None],
) -> list[str]:
    if not isinstance(value, list) or not value:
        _issue(issues, ManifestIssueCode.INVALID_VALUE, location, "Editable scope must contain exact file paths.")
        return []
    changed: set[str] | None = None
    if target_revision is not None and oracle_revision is not None:
        pair = (target_revision, oracle_revision)
        if pair not in change_cache:
            output = _git_output(repository_root, ["diff", "--name-only", target_revision, oracle_revision])
            if output is None:
                change_cache[pair] = None
            else:
                try:
                    change_cache[pair] = {
                        canonical_workspace_relative_path(item, allow_root=False) or item
                        for item in output.decode("utf-8").splitlines()
                    }
                except UnicodeError:
                    change_cache[pair] = None
        changed = change_cache[pair]
    normalized_paths: list[str] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(value):
        item_location = f"{location}/{index}"
        if not isinstance(item, dict):
            _issue(issues, ManifestIssueCode.INVALID_VALUE, item_location, "Scope entry must be an object.")
            continue
        _check_keys(
            item,
            required={"path", "operation"},
            allowed={"path", "operation"},
            location=item_location,
            issues=issues,
        )
        path = _canonical_path(item.get("path"), f"{item_location}/path", issues)
        if path is None:
            continue
        if path in seen_paths:
            _issue(issues, ManifestIssueCode.INVALID_PATH, f"{item_location}/path", "Editable paths must be unique after canonicalization.")
        seen_paths.add(path)
        normalized_paths.append(path)
        operation = item.get("operation")
        if not isinstance(operation, str) or operation not in {"create", "modify"}:
            _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{item_location}/operation", "Scope operation must be create or modify.")
            continue
        if target_revision and oracle_revision:
            exists_at_target = _repo_blob(repository_root, target_revision, path, blob_cache) is not None
            exists_at_oracle = _repo_blob(repository_root, oracle_revision, path, blob_cache) is not None
            if operation == "modify" and not exists_at_target:
                _issue(issues, ManifestIssueCode.INVALID_PATH, f"{item_location}/path", "Modify scope must exist at target revision.")
            if operation == "create" and (exists_at_target or not exists_at_oracle):
                _issue(issues, ManifestIssueCode.INVALID_PATH, f"{item_location}/path", "Create scope must be absent at target and present in oracle revision.")
            if changed is None or path not in changed:
                _issue(issues, ManifestIssueCode.SCOPE_NOT_IN_CHANGE, f"{item_location}/path", "Editable path is not proven by the pinned change.")
    return normalized_paths


def _validate_checks(
    value: object,
    *,
    location: str,
    target_revision: str | None,
    oracle_revision: str | None,
    oracle_paths: set[str],
    repository_root: Path,
    issues: list[ManifestIssue],
    commit_cache: dict[str, bool],
    blob_cache: dict[tuple[str, str], bytes | None],
) -> None:
    groups = {"baseline", "target", "regression"}
    if not isinstance(value, dict):
        _issue(issues, ManifestIssueCode.INVALID_CHECK, location, "Checks must contain baseline, target, and regression groups.")
        return
    _check_keys(value, required=groups, allowed=groups, location=location, issues=issues, code=ManifestIssueCode.INVALID_CHECK)
    seen_ids: set[str] = set()
    for group in sorted(groups):
        checks = value.get(group)
        group_location = f"{location}/{group}"
        if not isinstance(checks, list) or not checks:
            _issue(issues, ManifestIssueCode.INVALID_CHECK, group_location, "Each check group must be non-empty.")
            continue
        expected_test_revision = target_revision if group == "baseline" else oracle_revision
        for index, check in enumerate(checks):
            check_location = f"{group_location}/{index}"
            if not isinstance(check, dict):
                _issue(issues, ManifestIssueCode.INVALID_CHECK, check_location, "Check must be an object.")
                continue
            check_fields = {
                "check_id", "tests_revision", "argv", "shell", "cwd", "timeout_seconds"
            }
            _check_keys(
                check,
                required=check_fields,
                allowed=check_fields,
                location=check_location,
                issues=issues,
                code=ManifestIssueCode.INVALID_CHECK,
            )
            check_id = check.get("check_id")
            if not _valid_identifier(check_id) or check_id in seen_ids:
                _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/check_id", "Check ids must be valid and unique within a task.")
            elif isinstance(check_id, str):
                seen_ids.add(check_id)
            argv = check.get("argv")
            if not isinstance(argv, list) or not argv or any(
                not isinstance(argument, str) or not argument or "\x00" in argument
                for argument in argv
            ):
                _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/argv", "Check argv must be a non-empty array of non-empty strings.")
            if check.get("shell") is not False:
                _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/shell", "Check shell execution must be explicitly disabled.")
            module_paths: list[str] = []
            if (
                not isinstance(argv, list)
                or len(argv) < 4
                or argv[:3] != ["python", "-m", "unittest"]
            ):
                _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/argv", "Schema v1 supports only python -m unittest with explicit test modules.")
            else:
                for module in argv[3:]:
                    if not isinstance(module, str) or not _UNITTEST_MODULE_RE.fullmatch(module):
                        _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/argv", "Test command must name explicit tests.* modules.")
                        continue
                    module_path = module.replace(".", "/") + ".py"
                    module_paths.append(module_path)
            _canonical_path(check.get("cwd"), f"{check_location}/cwd", issues, allow_root=True)
            timeout = check.get("timeout_seconds")
            if not _finite_positive_number(timeout):
                _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/timeout_seconds", "Check timeout must be finite and positive.")
            tests_revision = _validate_revision(
                check.get("tests_revision"),
                f"{check_location}/tests_revision",
                repository_root,
                issues,
                commit_cache,
                invalid_code=ManifestIssueCode.INVALID_CHECK,
            )
            if expected_test_revision and tests_revision != expected_test_revision:
                _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/tests_revision", "Check tests revision does not match its baseline or oracle role.")
            if tests_revision is not None:
                for module_path in module_paths:
                    if _repo_blob(repository_root, tests_revision, module_path, blob_cache) is None:
                        _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/argv", "A pinned unittest module is absent from its tests revision.")
                    if group != "baseline" and module_path not in oracle_paths:
                        _issue(issues, ManifestIssueCode.INVALID_CHECK, f"{check_location}/argv", "Target and regression modules must be pinned as oracle files.")


def _validate_provenance(
    value: object,
    *,
    location: str,
    oracle_revision: str | None,
    repository_root: Path,
    issues: list[ManifestIssue],
) -> None:
    fields = {"kind", "commit", "subject", "task_text_basis"}
    if not isinstance(value, dict):
        _issue(issues, ManifestIssueCode.PROVENANCE_MISMATCH, location, "Task provenance is required.")
        return
    _check_keys(value, required=fields, allowed=fields, location=location, issues=issues, code=ManifestIssueCode.PROVENANCE_MISMATCH)
    commit = value.get("commit")
    if (
        value.get("kind") != "historical_acceptance_slice"
        or value.get("task_text_basis") != "commit_subject_and_pinned_tests"
        or not _nonempty_text(value.get("subject"))
        or not isinstance(commit, str)
        or commit != oracle_revision
    ):
        _issue(issues, ManifestIssueCode.PROVENANCE_MISMATCH, location, "Provenance must match the pinned historical oracle commit.")
        return
    actual_subject = _git_text(repository_root, ["show", "-s", "--format=%s", commit])
    if actual_subject != value.get("subject"):
        _issue(issues, ManifestIssueCode.PROVENANCE_MISMATCH, f"{location}/subject", "Pinned commit subject does not match provenance.")


def _validate_environment(
    value: object,
    expected: Mapping[str, str],
    location: str,
    issues: list[ManifestIssue],
) -> None:
    fields = {"os_family", "architecture", "python_version", "lock_path", "lock_sha256"}
    if not isinstance(value, dict):
        _issue(issues, ManifestIssueCode.ENVIRONMENT_MISMATCH, location, "Environment summary is required.")
        return
    _check_keys(value, required=fields, allowed=fields, location=location, issues=issues, code=ManifestIssueCode.ENVIRONMENT_MISMATCH)
    if (
        set(value) != fields
        or dict(value) != dict(expected)
        or not _valid_sha256(value.get("lock_sha256"))
    ):
        _issue(issues, ManifestIssueCode.ENVIRONMENT_MISMATCH, location, "Task environment does not match the trusted local environment summary.")


def _validate_budget(value: object, location: str, issues: list[ManifestIssue]) -> None:
    fields = {"max_steps", "max_cost_usd", "deadline_seconds", "calibration"}
    if not isinstance(value, dict):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, location, "Task budget is required.")
        return
    _check_keys(value, required=fields, allowed=fields, location=location, issues=issues)
    max_steps = value.get("max_steps")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/max_steps", "Budget max_steps must be a positive integer.")
    max_cost = value.get("max_cost_usd")
    if max_cost is not None and not _finite_positive_number(max_cost):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/max_cost_usd", "Budget max_cost_usd must be null or finite and positive.")
    if not _finite_positive_number(value.get("deadline_seconds")):
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/deadline_seconds", "Budget deadline must be finite and positive.")
    if value.get("calibration") != "provisional_unmeasured":
        _issue(issues, ManifestIssueCode.INVALID_VALUE, f"{location}/calibration", "Budget calibration status must remain explicit.")


def _validate_revision(
    value: object,
    location: str,
    repository_root: Path,
    issues: list[ManifestIssue],
    cache: dict[str, bool],
    *,
    invalid_code: ManifestIssueCode = ManifestIssueCode.INVALID_REVISION,
) -> str | None:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        _issue(issues, invalid_code, location, "Revision must be a full lowercase commit SHA.")
        return None
    if value not in cache:
        cache[value] = (
            _git_output(repository_root, ["cat-file", "-e", value + "^{commit}"])
            is not None
        )
    if not cache[value]:
        _issue(issues, ManifestIssueCode.UNKNOWN_REVISION, location, "Pinned commit is not present in the local repository.")
        return None
    return value


def _canonical_path(
    value: object,
    location: str,
    issues: list[ManifestIssue],
    *,
    allow_root: bool = False,
) -> str | None:
    if not isinstance(value, str):
        _issue(issues, ManifestIssueCode.INVALID_PATH, location, "Path must be a canonical workspace-relative string.")
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        _issue(issues, ManifestIssueCode.INVALID_PATH, location, "Path must be valid UTF-8.")
        return None
    normalized = canonical_workspace_relative_path(value, allow_root=allow_root)
    if normalized is None or normalized != value:
        _issue(issues, ManifestIssueCode.INVALID_PATH, location, "Path must be canonical, workspace-relative, and free of traversal.")
        return None
    return normalized


def _paths_overlap(first: str, second: str) -> bool:
    return (
        first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
    )


def _check_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    location: str,
    issues: list[ManifestIssue],
    code: ManifestIssueCode = ManifestIssueCode.UNKNOWN_FIELD,
) -> None:
    if any(not isinstance(key, str) for key in value):
        _issue(issues, code, location, "Object keys must be strings.")
        return
    for key in sorted(required - set(value)):
        _issue(issues, ManifestIssueCode.MISSING_FIELD, f"{location}/{key}", "Required field is missing.")
    for key in sorted(set(value) - allowed):
        _issue(issues, code, f"{location}/{key}", "Field is not part of this manifest schema.")


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_TASK_ID_RE.fullmatch(value))


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _finite_positive_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except (OverflowError, ValueError):
        return False


def _issue(
    issues: list[ManifestIssue],
    code: ManifestIssueCode,
    location: str,
    message: str,
) -> None:
    issues.append(ManifestIssue(code, location, message))


def _invalid_result(
    code: ManifestIssueCode,
    location: str,
    message: str,
) -> ManifestValidationResult:
    return ManifestValidationResult(False, None, (), (ManifestIssue(code, location, message),))
