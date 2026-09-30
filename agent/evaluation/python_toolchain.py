"""Stage one trusted CPython 3.12 runtime beside, never inside, a task tree."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


_MAX_FILES = 20_000
_MAX_BYTES = 256 * 1024 * 1024


class PythonToolchainError(Exception):
    pass


def stage_python_312(source: str | Path, destination: Path) -> Path:
    """Copy a trusted, standalone 3.12 runtime; never grant the task its source."""
    source_path = Path(source)
    if _is_link(source_path):
        raise PythonToolchainError("Python toolchain root must not be a link.")
    try:
        root = source_path.resolve(strict=True)
        if not root.is_dir() or destination.exists():
            raise PythonToolchainError("Python toolchain source or destination is invalid.")
        executable = root / "python.exe"
        if not executable.is_file() or _is_link(executable):
            raise PythonToolchainError("Trusted Python executable is unavailable.")
        probe = subprocess.run(
            [str(executable), "-I", "-S", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=5, shell=False,
        )
        if probe.returncode or probe.stdout.strip() != b"3.12":
            raise PythonToolchainError("Trusted Python must be CPython 3.12.")
        destination.mkdir()
        count = 0
        size = 0

        def copy_file(original: Path, target: Path) -> None:
            nonlocal count, size
            metadata = original.stat(follow_symlinks=False)
            if _is_link(original) or not stat.S_ISREG(metadata.st_mode):
                raise PythonToolchainError("Python toolchain contains an unsafe entry.")
            count += 1
            size += metadata.st_size
            if count > _MAX_FILES or size > _MAX_BYTES:
                raise PythonToolchainError("Python toolchain exceeds its staging limit.")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)

        for entry in root.iterdir():
            if entry.name == "python.exe" or entry.suffix.lower() == ".dll":
                copy_file(entry, destination / entry.name)
        for directory in ("Lib", "DLLs"):
            source_dir = root / directory
            if not source_dir.is_dir() or _is_link(source_dir):
                raise PythonToolchainError("Trusted Python standard library is unavailable.")
            pending = [(source_dir, destination / directory)]
            while pending:
                current, target = pending.pop()
                for entry in current.iterdir():
                    if directory == "Lib" and entry.name in {"site-packages", "__pycache__"}:
                        continue
                    if _is_link(entry):
                        raise PythonToolchainError("Python toolchain contains a link or junction.")
                    if entry.is_dir():
                        pending.append((entry, target / entry.name))
                    else:
                        copy_file(entry, target / entry.name)
        if not (destination / "Lib" / "encodings").is_dir():
            raise PythonToolchainError("Trusted Python standard library is incomplete.")
        return destination / "python.exe"
    except (OSError, subprocess.SubprocessError) as error:
        raise PythonToolchainError("Trusted Python toolchain could not be staged.") from error


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
