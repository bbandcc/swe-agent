"""Stage a bounded trusted CPython 3.12 runtime and locked dependencies."""

from __future__ import annotations

import shutil
import hashlib
import os
import struct
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath


_MAX_FILES = 20_000
_MAX_BYTES = 256 * 1024 * 1024
_MAX_ENTRIES = 20_000


# One reviewed native closure for the existing trusted CPython distribution.
# Hashes authorize bytes, not arbitrary conda metadata or DLL search paths.
_NATIVE_PROFILE = {
    "python.exe": (92672, "62c225fb9cdc41b139c7024581c233644f975ffc35314558c60ebefa6b88be01"),
    "python312.dll": (7388672, "751866bc6242ef67ec3e8623dbc45d2522061746952132b1e20632bc78aec9a8"),
    "DLLs/_ctypes.pyd": (121856, "e5e233eb8b7d8a03fd1ab2dc838178cb26ee3b78eff749d79f3371fc5f93954f"),
}
_FFI_IDENTITY = (40216, "ac70bc5452602c2df5eacbdab082d44650117d5729808bd48db2400b89de132a")


class PythonToolchainError(Exception):
    pass


def _no_links(path: Path) -> None:
    if any(_is_link(entry) for entry in (path, *path.parents)):
        raise PythonToolchainError("Toolchain path contains a link or junction.")


def _directory_identity(path: Path) -> tuple[int, int]:
    _no_links(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_ino <= 0:
        raise PythonToolchainError("Toolchain directory identity is unavailable.")
    return info.st_dev, info.st_ino


def _file_identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _review_inventory(source: str | Path, reviewed: Mapping[str, str] | None, *, unlinked: bool = False):
    """Match a complete, externally reviewed inventory; never infer approval.

    Trusted Python harnesses provide known hashes, not hashes captured from
    unknown current files. The source and its ancestors must remain under
    their exclusive control, including the interpreter's implicit host/runtime
    dependencies: this is not an equal-privilege process sandbox.
    """
    if not isinstance(reviewed, Mapping) or len(reviewed) > _MAX_FILES:
        raise PythonToolchainError("Author-reviewed Python input inventory is required before execution.")
    expected = {}
    for name, digest in tuple(reviewed.items()):
        if (not isinstance(name, str) or not name or "\\" in name
            or PurePosixPath(name).as_posix() != name or ".." in PurePosixPath(name).parts
            or PureWindowsPath(name).drive or PurePosixPath(name).is_absolute()
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
            raise PythonToolchainError("Reviewed toolchain inventory is invalid.")
        key = os.path.normcase(name)
        if key in expected:
            raise PythonToolchainError("Reviewed toolchain paths alias each other.")
        expected[key] = digest
    path = Path(source).absolute()
    _no_links(path)
    root = path.resolve(strict=True)
    directories = {}
    snapshots = {}
    pending = [root]
    total = entries = 0
    while pending:
        directory = pending.pop()
        directories[directory] = _directory_identity(directory)
        for entry in directory.iterdir():
            entries += 1
            if entries > _MAX_ENTRIES:
                raise PythonToolchainError("Python toolchain exceeds its entry limit.")
            _no_links(entry)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
                continue
            if not stat.S_ISREG(info.st_mode) or (unlinked and info.st_nlink != 1):
                raise PythonToolchainError("Python toolchain contains an unsafe entry.")
            name = entry.relative_to(root).as_posix()
            data = _native_snapshot(entry, _MAX_BYTES - total, (1,) if unlinked else None)
            if expected.pop(os.path.normcase(name), None) != hashlib.sha256(data).hexdigest():
                raise PythonToolchainError("Python input differs from the author-reviewed inventory.")
            snapshots[name] = (data, _file_identity(entry.stat(follow_symlinks=False)))
            total += len(data)
            if total > _MAX_BYTES or len(snapshots) > _MAX_FILES:
                raise PythonToolchainError("Python toolchain exceeds its staging limit.")
    if expected:
        raise PythonToolchainError("Reviewed Python input is missing.")
    _check_directories(directories)
    return root, snapshots, directories


def _check_directories(directories) -> None:
    for path, identity in directories.items():
        if _directory_identity(path) != identity:
            raise PythonToolchainError("Toolchain directory identity changed.")


def _check_inventory(root, snapshots, directories, *, unlinked=False) -> None:
    _check_directories(directories)
    expected = {name: hashlib.sha256(value[0]).hexdigest() for name, value in snapshots.items()}
    _, current, current_directories = _review_inventory(root, expected, unlinked=unlinked)
    if current != snapshots or current_directories != directories:
        raise PythonToolchainError("Python input changed during toolchain preparation.")


def validate_python_312(
    source: str | Path, *, expected_architecture: str = "AMD64",
    reviewed_files: Mapping[str, str] | None = None,
) -> Path:
    """Probe only author-reviewed, controlled CPython inputs; version is not provenance."""
    if not isinstance(reviewed_files, Mapping) or not reviewed_files:
        raise PythonToolchainError("Author-reviewed Python input inventory is required before execution.")
    try:
        reviewed_files = dict(reviewed_files)
        source_path = Path(source)
        if _is_link(source_path):
            raise PythonToolchainError("Python toolchain root must not be a link.")
        root, snapshots, directories = _review_inventory(source, reviewed_files)
        if not root.is_dir():
            raise PythonToolchainError("Python toolchain source is invalid.")
        executable = root / "python.exe"
        metadata = executable.stat(follow_symlinks=False)
        if _is_link(executable) or not stat.S_ISREG(metadata.st_mode):
            raise PythonToolchainError("Trusted Python executable is unavailable.")
        if not isinstance(expected_architecture, str) or expected_architecture.upper() != "AMD64":
            raise PythonToolchainError("Trusted Python must be 64-bit CPython 3.12 AMD64.")
        identity = _probe_python_312(executable)
        expected_machine = expected_architecture.upper()
        if (
            expected_machine != "AMD64"
            or identity != ("CPython", "3.12", "64", expected_machine, "win-amd64")
        ):
            raise PythonToolchainError(
                "Trusted Python must be 64-bit CPython 3.12 AMD64."
            )
        _check_inventory(root, snapshots, directories)
        return executable
    except (OSError, UnicodeError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
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
    reviewed_files: Mapping[str, str] | None = None,
    reviewed_dependencies: Mapping[str, str] | None = None,
) -> Path:
    """Stage only reviewed fixed inputs into new, exclusively owned storage.

    Inventories come from a trusted author/harness, never model output or a
    current-directory hash capture. Parents and inputs must be controlled by
    that author with no concurrent writers. Version probes are not approval.
    """
    owned = False
    directories = {}
    created = {}
    try:
        reviewed_files = dict(reviewed_files) if isinstance(reviewed_files, Mapping) else None
        reviewed_dependencies = dict(reviewed_dependencies) if isinstance(reviewed_dependencies, Mapping) else None
        requested = Path(destination).absolute()
        _no_links(requested)
        destination = requested.parent.resolve(strict=True) / requested.name
        parent_identity = _directory_identity(destination.parent)
        _no_links(destination)
        if destination.exists():
            raise PythonToolchainError("Python toolchain destination is invalid.")
        executable = validate_python_312(source, reviewed_files=reviewed_files)
        root, snapshots, source_directories = _review_inventory(source, reviewed_files)
        if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
            raise PythonToolchainError("Toolchain source and destination overlap.")
        dependency_snapshot = None
        if dependency_site_packages is not None:
            dependency_snapshot = _review_inventory(dependency_site_packages, reviewed_dependencies, unlinked=True)
            dependency_root = dependency_snapshot[0]
            if (destination == dependency_root or destination.is_relative_to(dependency_root)
                or dependency_root.is_relative_to(destination)):
                raise PythonToolchainError("Dependency source and destination overlap.")
        native = _fixed_native_snapshot(root)
        _check_inventory(root, snapshots, source_directories)
        outputs = {}
        output_directories = {"Lib", "DLLs"}
        for name, (data, _identity) in snapshots.items():
            parts = PurePosixPath(name).parts
            if ((len(parts) == 1 and (name == "python.exe" or Path(name).suffix.lower() == ".dll"))
                or (parts[0] in {"Lib", "DLLs"} and not
                    (parts[0] == "Lib" and any(p in {"site-packages", "__pycache__"} for p in parts[1:])))):
                outputs[name] = data
        for path in source_directories:
            name = path.relative_to(root).as_posix()
            parts = PurePosixPath(name).parts
            if parts and parts[0] in {"Lib", "DLLs"} and not any(p in {"site-packages", "__pycache__"} for p in parts[1:]):
                output_directories.add(name)
        if native is not None:
            if "DLLs/ffi-8.dll" in outputs:
                raise PythonToolchainError("Native DLL destination already exists in source inputs.")
            outputs["DLLs/ffi-8.dll"] = native
        if dependency_snapshot is not None:
            dependency_root, dependencies, dependency_directories = dependency_snapshot
            _check_inventory(dependency_root, dependencies, dependency_directories, unlinked=True)
            output_directories.add("Lib/site-packages")
            for name, (data, _identity) in dependencies.items():
                outputs["Lib/site-packages/" + name] = data
            for path in dependency_directories:
                relative = path.relative_to(dependency_root).as_posix()
                if relative != ".":
                    output_directories.add("Lib/site-packages/" + relative)
        for name in outputs:
            for parent in PurePosixPath(name).parents:
                if parent.as_posix() != ".":
                    output_directories.add(parent.as_posix())
        if ("python.exe" not in outputs or "Lib/encodings" not in output_directories
            or len(outputs) > _MAX_FILES or sum(map(len, outputs.values())) > _MAX_BYTES
            or len(outputs) + len(output_directories) > _MAX_ENTRIES):
            raise PythonToolchainError("Python toolchain is incomplete or exceeds its staging limit.")
        if _directory_identity(destination.parent) != parent_identity:
            raise PythonToolchainError("Toolchain destination parent changed.")
        destination.mkdir()  # Exclusive; never adopt an existing directory.
        owned = True
        directories[destination.parent] = parent_identity
        directories[destination] = _directory_identity(destination)
        for name in sorted(output_directories, key=lambda n: (len(PurePosixPath(n).parts), n)):
            _check_directories(directories)
            target = destination.joinpath(*PurePosixPath(name).parts)
            _no_links(target)
            target.mkdir()
            directories[target] = _directory_identity(target)
        for name, data in outputs.items():
            _check_directories(directories)
            target = destination.joinpath(*PurePosixPath(name).parts)
            _no_links(target)
            with target.open("xb") as stream:
                info = os.fstat(stream.fileno())
                created[target] = (info.st_dev, info.st_ino)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PythonToolchainError("Staged Python file identity is unsafe.")
                _check_directories(directories)
                current = target.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != created[target] or _is_link(target):
                    raise PythonToolchainError("Staged Python path changed before write.")
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if _native_snapshot(target, len(data), (1,)) != data:
                raise PythonToolchainError("Staged Python contents changed.")
        _check_inventory(root, snapshots, source_directories)
        if dependency_snapshot is not None:
            _check_inventory(*dependency_snapshot, unlinked=True)
        _check_directories(directories)
        expected_outputs = {os.path.normcase(name): data for name, data in outputs.items()}
        for path in destination.rglob("*"):
            _no_links(path)
            if path.is_dir():
                if path not in directories:
                    raise PythonToolchainError("Unexpected directory in staged Python.")
                continue
            name = os.path.normcase(path.relative_to(destination).as_posix())
            expected = expected_outputs.pop(name, None)
            info = path.stat(follow_symlinks=False)
            if (expected is None or (info.st_dev, info.st_ino) != created.get(path)
                or _native_snapshot(path, len(expected), (1,)) != expected):
                raise PythonToolchainError("Staged Python publication identity changed.")
        if expected_outputs:
            raise PythonToolchainError("Staged Python file disappeared before publication.")
        return destination / "python.exe"
    except BaseException as error:
        if owned:
            try:
                _check_directories(directories)
                for path in destination.rglob("*"):
                    _no_links(path)
                    if path.is_dir():
                        if path not in directories:
                            raise OSError("Unowned directory in failed staging")
                    elif path not in created:
                        raise OSError("Unowned file in failed staging")
                    else:
                        info = path.stat(follow_symlinks=False)
                        if (info.st_dev, info.st_ino) != created[path]:
                            raise OSError("Owned staging file was replaced")
                shutil.rmtree(destination)
                if destination.exists():
                    raise OSError("Owned Python staging still exists")
            except (OSError, PythonToolchainError) as cleanup_error:
                raise PythonToolchainError("Python toolchain cleanup failed; owned storage was not removed.") from cleanup_error
        if isinstance(error, (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError)):
            raise PythonToolchainError("Trusted Python toolchain could not be staged.") from error
        raise


def _is_link(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
        attributes = getattr(metadata, "st_file_attributes", 0)
        tag = getattr(metadata, "st_reparse_tag", 0)
        if (not isinstance(attributes, int) or not isinstance(tag, int)
                or attributes < 0 or tag < 0 or (os.name == "nt" and not attributes)):
            raise PythonToolchainError("Toolchain path attributes are unavailable.")
        return (bool(attributes & 0x400) or bool(tag) or path.is_symlink()
                or (hasattr(path, "is_junction") and path.is_junction()))
    except FileNotFoundError:
        # Optional profile members and exclusive-create leaves may be absent.
        # Required sources still fail their subsequent stat/open operation.
        return False
    except OSError:
        return True


def _native_snapshot(path: Path, limit: int, links: tuple[int, ...] | None) -> bytes:
    # No resolution before link checks; never share an inode with the lowbox.
    for entry in (path, *path.parents):
        if _is_link(entry):
            raise PythonToolchainError("Native DLL path contains a link or junction.")
    checked = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(checked.st_mode) or checked.st_ino <= 0:
        raise PythonToolchainError("Snapshot source identity is unavailable.")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (before.st_nlink < 1 or (links is not None and before.st_nlink not in links)) or before.st_size > limit:
            raise PythonToolchainError("Native DLL input identity is unsafe.")
        def path_identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
        if path_identity(before) != path_identity(checked) or any(_is_link(entry) for entry in (path, *path.parents)):
            raise PythonToolchainError("Snapshot source handle differs from the checked path.")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
    # Windows fd/path st_ctime differ (legacy creation vs change time).
    if identity(before) != identity(after) or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_nlink) or len(data) != before.st_size or len(data) > limit or any(_is_link(entry) for entry in (path, *path.parents)):
        raise PythonToolchainError("Native DLL changed during snapshot.")
    return data


def _is_amd64_pe(data: bytes) -> bool:
    if len(data) < 64 or data[:2] != b"MZ":
        return False
    offset = struct.unpack_from("<I", data, 60)[0]
    return (offset <= len(data) - 26 and data[offset:offset + 4] == b"PE\0\0"
            and struct.unpack_from("<H", data, offset + 4)[0] == 0x8664
            and struct.unpack_from("<H", data, offset + 24)[0] == 0x20b)


def _probe_native_version(executable: Path) -> str:
    probe = subprocess.run([str(executable), "-I", "-S", "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                           cwd=executable.parent, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, shell=False)
    if probe.returncode != 0:
        raise PythonToolchainError("Native Python version probe failed.")
    return probe.stdout.strip().decode("ascii")


def _fixed_native_snapshot(root: Path) -> bytes | None:
    images = {}
    try:
        for name, (size, digest) in _NATIVE_PROFILE.items():
            path = root / name
            if not path.exists() and not _is_link(path):
                continue
            if path.stat(follow_symlinks=False).st_size != size:
                continue
            data = _native_snapshot(path, size, None)
            images[name] = data
        # Nonmatching distributions retain their previous staging behavior.
        matches = [name for name, data in images.items()
                   if (len(data), hashlib.sha256(data).hexdigest()) == _NATIVE_PROFILE[name]]
        if not matches:
            return None
        if len(matches) != len(_NATIVE_PROFILE) or any(not _is_amd64_pe(data) for data in images.values()):
            raise PythonToolchainError("Reviewed native Python profile mismatch.")
        if _probe_native_version(root / "python.exe") != "3.12.3":
            raise PythonToolchainError("Reviewed native Python version mismatch.")
        data = _native_snapshot(root / "Library" / "bin" / "ffi-8.dll", _FFI_IDENTITY[0], (1, 2))
        if (len(data), hashlib.sha256(data).hexdigest()) != _FFI_IDENTITY or not _is_amd64_pe(data):
            raise PythonToolchainError("Reviewed ffi DLL identity mismatch.")
        return data
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise PythonToolchainError("Reviewed native DLL preparation failed.") from error
