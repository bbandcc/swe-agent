"""Stage a bounded trusted CPython 3.12 runtime and locked dependencies."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path


_MAX_FILES = 20_000
_MAX_BYTES = 256 * 1024 * 1024
_MAX_ENTRIES = 20_000


class PythonToolchainError(Exception):
    pass


def validate_python_312(
    source: str | Path, *, expected_architecture: str = "AMD64"
) -> Path:
    """Validate the executable itself as 64-bit CPython 3.12 AMD64."""
    source_path = Path(source)
    if _is_link(source_path):
        raise PythonToolchainError("Python toolchain root must not be a link.")
    try:
        root = source_path.resolve(strict=True)
        if not root.is_dir():
            raise PythonToolchainError("Python toolchain source is invalid.")
        executable = root / "python.exe"
        metadata = executable.stat(follow_symlinks=False)
        if _is_link(executable) or not stat.S_ISREG(metadata.st_mode):
            raise PythonToolchainError("Trusted Python executable is unavailable.")
        identity = _probe_python_312(executable)
        expected_machine = expected_architecture.upper()
        if (
            expected_machine != "AMD64"
            or identity != ("CPython", "3.12", "64", expected_machine, "win-amd64")
        ):
            raise PythonToolchainError(
                "Trusted Python must be 64-bit CPython 3.12 AMD64."
            )
        return executable
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise PythonToolchainError("Trusted Python toolchain could not be validated.") from error


def _probe_python_312(executable: Path) -> tuple[str, ...]:
    probe = subprocess.run(
        [
            str(executable),
            "-I",
            "-S",
            "-c",
            "import platform,struct,sys,sysconfig; "
            "print('|'.join((platform.python_implementation(), "
            "'%d.%d' % sys.version_info[:2], str(struct.calcsize('P') * 8), "
            "platform.machine().upper(), sysconfig.get_platform().lower())))",
        ],
        cwd=executable.parent,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        shell=False,
    )
    if probe.returncode != 0:
        raise PythonToolchainError("Trusted Python identity probe failed.")
    return tuple(probe.stdout.strip().decode("ascii").split("|"))


def stage_python_312(
    source: str | Path,
    destination: Path,
    *,
    dependency_site_packages: str | Path | None = None,
) -> Path:
    """Copy CPython 3.12 and optional bounded dependencies to an owned sibling."""
    executable = validate_python_312(source)
    root = executable.parent
    if destination.exists():
        raise PythonToolchainError("Python toolchain destination is invalid.")

    dependencies: Path | None = None
    if dependency_site_packages is not None:
        dependency_path = Path(dependency_site_packages)
        if _is_link(dependency_path):
            raise PythonToolchainError("Dependency source must not be a link.")
        try:
            dependencies = dependency_path.resolve(strict=True)
            if not dependencies.is_dir():
                raise PythonToolchainError("Dependency source is invalid.")
        except (OSError, RuntimeError, ValueError) as error:
            raise PythonToolchainError("Dependency source is unavailable.") from error

    try:
        destination.mkdir()
        count = 0
        entry_count = 0
        size = 0

        def count_entry() -> None:
            nonlocal entry_count
            entry_count += 1
            if entry_count > _MAX_ENTRIES:
                raise PythonToolchainError("Python toolchain exceeds its entry limit.")

        def make_directory(path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            count_entry()

        def copy_file(original: Path, target: Path, *, require_unlinked: bool = False) -> None:
            nonlocal count, size
            metadata = original.stat(follow_symlinks=False)
            if (
                _is_link(original)
                or not stat.S_ISREG(metadata.st_mode)
                or (require_unlinked and metadata.st_nlink != 1)
            ):
                raise PythonToolchainError("Python toolchain contains an unsafe entry.")
            count += 1
            size += metadata.st_size
            if count > _MAX_FILES or size > _MAX_BYTES:
                raise PythonToolchainError("Python toolchain exceeds its staging limit.")
            count_entry()
            shutil.copyfile(original, target)

        for entry in root.iterdir():
            if entry.name == "python.exe" or entry.suffix.lower() == ".dll":
                copy_file(entry, destination / entry.name)
        for directory in ("Lib", "DLLs"):
            source_dir = root / directory
            if not source_dir.is_dir() or _is_link(source_dir):
                raise PythonToolchainError("Trusted Python standard library is unavailable.")
            destination_dir = destination / directory
            make_directory(destination_dir)
            pending = [(source_dir, destination_dir)]
            while pending:
                current, target = pending.pop()
                for entry in current.iterdir():
                    if directory == "Lib" and entry.name in {"site-packages", "__pycache__"}:
                        continue
                    if _is_link(entry):
                        raise PythonToolchainError("Python toolchain contains a link or junction.")
                    if entry.is_dir():
                        target_directory = target / entry.name
                        make_directory(target_directory)
                        pending.append((entry, target_directory))
                    else:
                        copy_file(entry, target / entry.name)

        if dependencies is not None:
            dependency_destination = destination / "Lib" / "site-packages"
            make_directory(dependency_destination)
            pending = [(dependencies, dependency_destination)]
            while pending:
                current, target = pending.pop()
                if _is_link(current):
                    raise PythonToolchainError("Dependencies contain a link or junction.")
                for entry in current.iterdir():
                    if _is_link(entry):
                        raise PythonToolchainError("Dependencies contain a link or junction.")
                    if entry.is_dir():
                        target_directory = target / entry.name
                        make_directory(target_directory)
                        pending.append((entry, target_directory))
                    else:
                        copy_file(entry, target / entry.name, require_unlinked=True)

        if not (destination / "Lib" / "encodings").is_dir():
            raise PythonToolchainError("Python standard library is incomplete.")
        return destination / "python.exe"
    except (OSError, subprocess.SubprocessError) as error:
        raise PythonToolchainError("Trusted Python toolchain could not be staged.") from error


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True
