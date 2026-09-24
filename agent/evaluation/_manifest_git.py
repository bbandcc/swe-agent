"""Bounded read-only Git evidence used by the S5a manifest validator."""

from __future__ import annotations

import subprocess
from pathlib import Path


_GIT_TIMEOUT_SECONDS = 5.0


def _repo_blob(
    repository_root: Path,
    revision: str,
    path: str,
    cache: dict[tuple[str, str], bytes | None],
) -> bytes | None:
    key = (revision, path)
    if key not in cache:
        cache[key] = _git_output(repository_root, ["cat-file", "blob", f"{revision}:{path}"])
    return cache[key]


def _git_text(repository_root: Path, arguments: list[str]) -> str | None:
    output = _git_output(repository_root, arguments)
    if output is None:
        return None
    try:
        return output.decode("utf-8").strip()
    except UnicodeError:
        return None


def _git_output(repository_root: Path, arguments: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None
