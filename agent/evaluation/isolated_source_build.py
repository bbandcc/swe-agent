"""One fixed source recipe; package code executes only in Windows AppContainer.

This returns derived build evidence, never a replacement historical lock entry.
It intentionally does not participate in frozen-environment admission.
"""
from __future__ import annotations

import base64
import csv
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from enum import Enum
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import tomllib
from typing import Any, Mapping
import zipfile

from agent.evaluation.task_manifest import validate_task_manifest
from agent.evaluation.python_toolchain import PythonToolchainError, stage_python_312
from agent.evaluation.windows_sandbox import SandboxCommand, SandboxResult, SandboxStatus, run_in_windows_appcontainer
from agent.workspace.paths import canonical_roots_overlap, canonical_workspace_relative_path, canonicalize_root_path


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    filename: str
    version: str
    url: str
    sha256: str
    size: int


SDIST = ArtifactIdentity("fuzzysearch-0.7.3.tar.gz", "0.7.3", "https://files.pythonhosted.org/packages/f7/28/3e9e4e55fd35356f331a22976694e151eb0214b68d3cd471936f9c09deba/fuzzysearch-0.7.3.tar.gz", "d5a1b114ceee50a5e181b2fe1ac1b4371ac8db92142770a48fed49ecbc37ca4c", 112677)
SETUPTOOLS_WHEEL = ArtifactIdentity("setuptools-75.8.0-py3-none-any.whl", "75.8.0", "https://files.pythonhosted.org/packages/69/8a/b9dc7678803429e4a3bc9ba462fa3dd9066824d3c607490235c6a796be5a/setuptools-75.8.0-py3-none-any.whl", "e3982f444617239225d675215d51f6ba05f845d4eec313da4418fdbb56fb27e3", 1228782)
WHEEL_WHEEL = ArtifactIdentity("wheel-0.45.1-py3-none-any.whl", "0.45.1", "https://files.pythonhosted.org/packages/0b/2c/87f3254fd8ffd29e4c02732eee68a83a1d3c346ae39bc6822dcbcb697f2b/wheel-0.45.1-py3-none-any.whl", "708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248", 72494)
MAX_ARCHIVE_ENTRIES = 4096
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_WHEEL_BYTES = 4 * 1024 * 1024
MAX_LOCK_BYTES = 4 * 1024 * 1024
_WHEEL_NAME = "fuzzysearch-0.7.3-py3-none-any.whl"
_DIST = "fuzzysearch-0.7.3.dist-info/"
# This fixed slice admits only tasks from the reviewed S5a seed, not caller
# supplied manifests or a rehashed task with an invented id.
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "evals/s5a/tasks.v1.json"
_MANIFEST_HASH = "7274b2769b39a242c0b8a4005e86bf92b842532c2f80117eb3ffe57df67b9253"
_REPOSITORY = "https://github.com/bbandcc/swe-agent.git"


class BuildStatus(str, Enum):
    BUILT = "built"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class BuildErrorCode(str, Enum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INVALID_INPUT = "invalid_input"
    TOKEN_INVALID = "token_invalid"
    TOOLCHAIN_INVALID = "toolchain_invalid"
    SANDBOX_FAILED = "sandbox_failed"
    TIMEOUT = "timeout"
    OUTPUT_INVALID = "output_invalid"
    CLEANUP_FAILED = "cleanup_failed"
    PUBLISH_FAILED = "publish_failed"


@dataclass(frozen=True, slots=True)
class DerivedWheel:
    relative_path: str
    sha256: str
    size: int
    media_type: str = "application/zip"


@dataclass(frozen=True, slots=True)
class BuildProvenance:
    task_id: str
    revision: str
    lock_path: str
    lock_sha256: str
    source: ArtifactIdentity
    bootstrap: tuple[ArtifactIdentity, ...]
    recipe_sha256: str
    python_identity: tuple[str, ...]
    python_executable_sha256: str
    wheel_sha256: str
    wheel_size: int
    sandbox_status: str
    token_is_appcontainer: bool
    schema_version: int = 1
    origin: str = "DERIVED_FROM_PINNED_SDIST"


@dataclass(frozen=True, slots=True)
class IsolatedBuildResult:
    status: BuildStatus
    error_code: BuildErrorCode | None = None
    cleanup_complete: bool = True
    wheel: DerivedWheel | None = None
    provenance: BuildProvenance | None = None
    sandbox_result: SandboxResult | None = None


class _Rejected(ValueError):
    pass


class _PublicationCleanupFailed(OSError):
    pass


def build_pinned_fuzzysearch_wheel(
    *, repository_root: str | Path, task: Mapping[str, Any], runtime_root: str | Path,
    python_toolchain_root: str | Path, sdist_path: str | Path,
    setuptools_wheel_path: str | Path, wheel_wheel_path: str | Path,
) -> IsolatedBuildResult:
    """Build exactly the pinned pure-Python recipe, without host package execution.

    Inputs are caller-supplied data files; this function neither downloads nor
    installs packages on the host. Resource limits and recipe are not options.
    A result reference is published only after sandbox AND staging cleanup.
    """
    if platform.system() != "Windows":
        return IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.UNSUPPORTED_PLATFORM)
    if not _normal_user_token():
        return IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.TOKEN_INVALID)
    container: Path | None = None
    result = IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.INVALID_INPUT)
    wheel_data: bytes | None = None
    provenance: BuildProvenance | None = None
    sandbox: SandboxResult | None = None
    try:
        repo = canonicalize_root_path(repository_root)
        runtime = canonicalize_root_path(runtime_root, must_exist=False)
        if canonical_roots_overlap(repo, runtime):
            raise _Rejected("Overlapping roots")
        task_id, revision, lock_path, lock_hash = _identity(repo, task)
        payloads = [_pinned_bytes(path, pin, (repo, runtime)) for path, pin in (
            (sdist_path, SDIST), (setuptools_wheel_path, SETUPTOOLS_WHEEL), (wheel_wheel_path, WHEEL_WHEEL))]
        runtime.mkdir(parents=True, exist_ok=True)
        runtime = canonicalize_root_path(runtime_root)
        if canonical_roots_overlap(repo, runtime):
            raise _Rejected("Overlapping canonical roots")
        source_python = canonicalize_root_path(python_toolchain_root)
        if any(canonical_roots_overlap(source_python, root) for root in (repo, runtime)):
            raise _Rejected("Untrusted toolchain location")
        container = Path(tempfile.mkdtemp(prefix="isolated-build-", dir=runtime))
        build = container / "build"
        build.mkdir()
        dependencies = container / "bootstrap"
        dependencies.mkdir()
        _stage_sdist(payloads[0], build / "source")
        _stage_bootstrap(payloads[1:], dependencies)
        executable = stage_python_312(source_python, container / "toolchain-build", dependency_site_packages=dependencies)
        # Remove the staging source before granting the lowbox any access.
        shutil.rmtree(dependencies)
        (build / "out").mkdir()
        command = SandboxCommand(
            workspace_root=build, python_toolchain_root=executable.parent,
            argv=(str(executable), "-B", "-S", "setup.py", "--noexts", "bdist_wheel", "--dist-dir", str(build / "out")),
            cwd="source/fuzzysearch-0.7.3", timeout_seconds=120,
            cpu_rate_percent=50, cpu_time_seconds=60,
            memory_limit_bytes=512 * 1024 * 1024, output_limit_bytes=64 * 1024,
        )
        python_hash = hashlib.sha256(_safe_bytes(executable, 32 * 1024 * 1024)).hexdigest()
        sandbox = run_in_windows_appcontainer(command)
        if not sandbox.cleanup_complete:
            result = IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.CLEANUP_FAILED, False, sandbox_result=sandbox)
        elif sandbox.status is SandboxStatus.TIMED_OUT:
            result = IsolatedBuildResult(BuildStatus.TIMED_OUT, BuildErrorCode.TIMEOUT, sandbox_result=sandbox)
        elif sandbox.status is not SandboxStatus.COMPLETED or sandbox.exit_code != 0 or not sandbox.started or not sandbox.token_is_appcontainer:
            result = IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.SANDBOX_FAILED, sandbox_result=sandbox)
        else:
            result = IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.OUTPUT_INVALID, sandbox_result=sandbox)
            wheel_data = _output_wheel(build / "out")
            digest = hashlib.sha256(wheel_data).hexdigest()
            recipe = {"schema": 1, "argv": ["python", "-B", "-S", "setup.py", "--noexts", "bdist_wheel", "--dist-dir", "<build>/out"],
                      "cwd": "source/fuzzysearch-0.7.3", "bootstrap": [asdict(SETUPTOOLS_WHEEL), asdict(WHEEL_WHEEL)],
                      "python": ["CPython", "3.12", "64", "AMD64", "win-amd64"],
                      "limits": [120, 60, 50, 512 * 1024 * 1024, 64 * 1024]}
            provenance = BuildProvenance(task_id, revision, lock_path, lock_hash, SDIST,
                (SETUPTOOLS_WHEEL, WHEEL_WHEEL), hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                ("CPython", "3.12", "64", "AMD64", "win-amd64"), python_hash, digest, len(wheel_data), sandbox.status.value, sandbox.token_is_appcontainer)
    except PythonToolchainError:
        result = IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.TOOLCHAIN_INVALID)
    except (_Rejected, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile):
        # Only anticipated input/filesystem/archive failures are converted.
        pass
    finally:
        if container is not None:
            try:
                shutil.rmtree(container)
                if container.exists():
                    raise OSError("Build staging still exists")
            except OSError:
                result = IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.CLEANUP_FAILED, False, sandbox_result=sandbox)
                wheel_data = None
                provenance = None
    if result.error_code is BuildErrorCode.CLEANUP_FAILED or provenance is None or wheel_data is None:
        return result
    try:
        reference = _publish(runtime, wheel_data)
    except _PublicationCleanupFailed:
        return IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.CLEANUP_FAILED, False, sandbox_result=sandbox)
    except (OSError, ValueError):
        return IsolatedBuildResult(BuildStatus.FAILED, BuildErrorCode.PUBLISH_FAILED, sandbox_result=sandbox)
    return IsolatedBuildResult(BuildStatus.BUILT, wheel=reference, provenance=provenance, sandbox_result=sandbox)


def _normal_user_token() -> bool:
    """No profile/ACL side effects before rejecting an elevated/lowbox caller."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    api.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    api.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    token = wintypes.HANDLE()
    if not api.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        return False
    try:
        for kind in (20, 29):
            value, length = wintypes.DWORD(), wintypes.DWORD()
            if not api.GetTokenInformation(token, kind, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(length)) or value.value:
                return False
        needed = wintypes.DWORD()
        api.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 512:
            return False
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.GetTokenInformation(token, 25, buffer, needed.value, ctypes.byref(needed)):
            return False
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        api.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        api.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        api.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        api.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        count = api.GetSidSubAuthorityCount(sid).contents.value
        return count > 0 and api.GetSidSubAuthority(sid, count - 1).contents.value <= 0x2000
    finally:
        kernel.CloseHandle(token)


def _identity(repo: Path, task: Mapping[str, Any]) -> tuple[str, str, str, str]:
    if not isinstance(task, Mapping):
        raise _Rejected("Task required")
    task_id = task.get("task_id")
    target, env = task.get("target"), task.get("environment")
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", task_id) or not isinstance(target, Mapping) or not isinstance(env, Mapping):
        raise _Rejected("Task identity")
    revision, lock_path, digest = target.get("revision"), env.get("lock_path"), env.get("lock_sha256")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise _Rejected("Pinned revision required")
    if (set(env) != {"os_family", "architecture", "python_version", "lock_path", "lock_sha256"}
        or [env.get(k) for k in ("os_family", "architecture", "python_version")] != ["Windows", "AMD64", "3.12"]
        or not isinstance(lock_path, str) or canonical_workspace_relative_path(lock_path, allow_root=False) != lock_path
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise _Rejected("Historical environment identity")
    document = json.loads(_safe_bytes(_MANIFEST_PATH, MAX_LOCK_BYTES).decode("utf-8"))
    if not isinstance(document, dict) or document.get("manifest_sha256") != _MANIFEST_HASH:
        raise _Rejected("Untrusted manifest identity")
    candidates = document.get("tasks")
    if not isinstance(candidates, list):
        raise _Rejected("Manifest tasks required")
    matches = [item for item in candidates if isinstance(item, dict) and item.get("task_id") == task_id]
    if len(matches) != 1 or dict(task) != matches[0]:
        raise _Rejected("Task differs from trusted S5a entry")
    validation = validate_task_manifest(document, repository_root=repo,
        expected_repository=_REPOSITORY, expected_environment=matches[0]["environment"])
    if not validation.valid:
        raise _Rejected("S5a manifest evidence invalid")
    def git(*args: str) -> bytes:
        response = subprocess.run(["git", "-C", str(repo), *args], shell=False, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True)
        return response.stdout
    # Resolve an immutable blob first, prove its byte bound, then read that blob.
    if git("cat-file", "-t", revision).strip() != b"commit":
        raise _Rejected("Target must be a commit")
    blob = git("rev-parse", "--verify", revision + ":" + lock_path).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", blob) or git("cat-file", "-t", blob).strip() != b"blob":
        raise _Rejected("Pinned lock must be a regular Git blob")
    if git("ls-tree", "--format=%(objectmode)", revision, "--", lock_path).strip() not in (b"100644", b"100755"):
        raise _Rejected("Pinned lock cannot be a Git symlink")
    size = int(git("cat-file", "-s", blob))
    if not 0 < size <= MAX_LOCK_BYTES:
        raise _Rejected("Lock byte limit")
    raw = git("cat-file", "blob", blob)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise _Rejected("Historical lock mismatch")
    package_list = tomllib.loads(raw.decode("utf-8")).get("package", [])
    if not isinstance(package_list, list) or any(not isinstance(p, dict) for p in package_list):
        raise _Rejected("Lock package shape")
    packages = [p for p in package_list if p.get("name") == "fuzzysearch"]
    if any(not isinstance(p.get("sdist"), dict) for p in packages):
        raise _Rejected("Source identity shape")
    if len(packages) != 1 or packages[0].get("version") != SDIST.version or packages[0].get("sdist", {}).get("url") != SDIST.url or packages[0].get("sdist", {}).get("hash") != "sha256:" + SDIST.sha256 or packages[0].get("sdist", {}).get("size") != SDIST.size:
        raise _Rejected("Pinned fuzzysearch source identity")
    return task_id, revision, lock_path, digest


def _safe_bytes(path: Path, limit: int) -> bytes:
    # Check every existing component before resolving, including the final fd.
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise _Rejected("Linked input")
    with path.open("rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > limit:
            raise _Rejected("Unsafe or oversized regular file")
        data = stream.read(limit + 1)
    if len(data) != metadata.st_size or len(data) > limit:
        raise _Rejected("Byte limit")
    return data


def _pinned_bytes(value: str | Path, pin: ArtifactIdentity, blocked: tuple[Path, ...]) -> bytes:
    path = Path(value)
    if not path.is_absolute() or any(canonical_roots_overlap(path.parent.resolve(), root) for root in blocked):
        raise _Rejected("Source must be outside repo/runtime")
    data = _safe_bytes(path, pin.size)
    if len(data) != pin.size or hashlib.sha256(data).hexdigest() != pin.sha256:
        raise _Rejected("Input hash/size mismatch")
    return data


def _member_path(name: str, seen: dict[str, str]) -> str:
    value = name.rstrip("/")
    parts = value.split("/")
    if not value or "\\" in value or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute() or any(p in ("", ".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((".", " ")) or p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]} for p in parts):
        raise _Rejected("Archive escape/invalid name")
    key = value.casefold()
    if key in seen:
        raise _Rejected("Archive duplicate/case collision")
    # Also forbid conflicting spelling of ancestor directories.
    for previous in seen.values():
        for old, new in zip(previous.split("/"), parts):
            if old.casefold() != new.casefold():
                break
            if old != new:
                raise _Rejected("Archive ancestor case collision")
    seen[key] = value
    return value


def _zip_members(data: bytes, max_entries: int = MAX_ARCHIVE_ENTRIES) -> dict[str, bytes]:
    # Inspect the bounded EOCD entry count BEFORE ZipFile materializes its index.
    eocd = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if eocd < 0 or len(data) - eocd < 22:
        raise _Rejected("ZIP end record")
    disk, start_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack_from("<4H2IH", data, eocd + 4)
    if disk or start_disk or disk_count != count or count > max_entries or count == 65535 or cd_offset + cd_size != eocd or eocd + 22 + comment != len(data):
        raise _Rejected("ZIP index bound/format")
    result: dict[str, bytes] = {}; seen: dict[str, str] = {}; total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) != count:
            raise _Rejected("ZIP index mismatch")
        for info in archive.infolist():
            path = _member_path(info.filename, seen)
            mode = info.external_attr >> 16
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise _Rejected("Unsupported ZIP compression")
            if info.flag_bits & 1 or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise _Rejected("ZIP link/device/encryption")
            if info.is_dir():
                continue
            total += info.file_size
            if info.file_size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
                raise _Rejected("ZIP expansion bound")
            with archive.open(info) as stream:
                content = stream.read(MAX_MEMBER_BYTES + 1)
            if len(content) != info.file_size:
                raise _Rejected("ZIP size mismatch")
            result[path] = content
    return result


def _stage_sdist(data: bytes, destination: Path) -> None:
    seen: dict[str, str] = {}; files: dict[str, bytes] = {}; total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for index, member in enumerate(archive):
            if index >= 512:
                raise _Rejected("Tar entry bound")
            path = _member_path(member.name, seen)
            if not (path == "fuzzysearch-0.7.3" or path.startswith("fuzzysearch-0.7.3/")) or not (member.isdir() or member.isfile()):
                raise _Rejected("Tar entry/link")
            if member.isdir():
                continue
            total += member.size
            if member.size > MAX_MEMBER_BYTES or total > 8 * 1024 * 1024:
                raise _Rejected("Tar expansion bound")
            stream = archive.extractfile(member)
            if stream is None:
                raise _Rejected("Tar member unreadable")
            with stream:
                content = stream.read(MAX_MEMBER_BYTES + 1)
            if len(content) != member.size:
                raise _Rejected("Tar size mismatch")
            files[path] = content
    if "fuzzysearch-0.7.3/setup.py" not in files:
        raise _Rejected("Missing build entry")
    _write_files(destination, files)


def _stage_bootstrap(payloads: list[bytes], destination: Path) -> None:
    seen: dict[str, str] = {}; all_files: dict[str, bytes] = {}; total = 0
    for payload in payloads:
        for name, data in _zip_members(payload).items():
            _member_path(name, seen)
            if name.endswith(".pth") or ".data/" in name:
                # .pth is not processed under -S. Do not copy startup hooks.
                if name.endswith(".pth"):
                    continue
                raise _Rejected("Unexpected bootstrap install scheme")
            total += len(data)
            if total > MAX_EXPANDED_BYTES or len(all_files) >= MAX_ARCHIVE_ENTRIES:
                raise _Rejected("Bootstrap aggregate bound")
            all_files[name] = data
    _write_files(destination, all_files)


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        target = root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        if hashlib.sha256(_safe_bytes(target, MAX_MEMBER_BYTES)).digest() != hashlib.sha256(data).digest():
            raise _Rejected("Staging mismatch")


def _output_wheel(folder: Path) -> bytes:
    if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
        raise _Rejected("Linked output")
    with os.scandir(folder) as entries:
        first = next(entries, None)
        if first is None or next(entries, None) is not None or first.name != _WHEEL_NAME:
            raise _Rejected("Expected exactly one wheel")
        data = _safe_bytes(Path(first.path), MAX_WHEEL_BYTES)
    files = _zip_members(data, max_entries=512)
    allowed_metadata = {"METADATA", "WHEEL", "RECORD", "top_level.txt", "LICENSE", "LICENSE.txt", "AUTHORS.rst"}
    for path in files:
        if path.startswith("fuzzysearch/") and path.endswith(".py"):
            continue
        if path.startswith(_DIST) and path[len(_DIST):] in allowed_metadata:
            continue
        raise _Rejected("Unexpected wheel code/install entry")
    if "fuzzysearch/__init__.py" not in files:
        raise _Rejected("Empty package")
    metadata = BytesParser().parsebytes(files.get(_DIST + "METADATA", b""))
    wheel = BytesParser().parsebytes(files.get(_DIST + "WHEEL", b""))
    if metadata.get_all("Name") != ["fuzzysearch"] or metadata.get_all("Version") != ["0.7.3"] or metadata.get_all("Requires-Dist") not in (["attrs>=19.3"], ["attrs >=19.3"]):
        raise _Rejected("Wheel metadata")
    if wheel.get_all("Wheel-Version") != ["1.0"] or wheel.get_all("Root-Is-Purelib") != ["true"] or wheel.get_all("Tag") != ["py3-none-any"]:
        raise _Rejected("Wheel tag/purelib")
    record_path = _DIST + "RECORD"
    records = list(csv.reader(io.StringIO(files.get(record_path, b"").decode("utf-8"))))
    if len(records) != len(files):
        raise _Rejected("RECORD membership")
    seen: set[str] = set()
    for row in records:
        if len(row) != 3 or row[0] not in files or row[0] in seen:
            raise _Rejected("RECORD shape/duplicate")
        seen.add(row[0]); path, digest, size = row
        if path == record_path:
            if digest or size: raise _Rejected("RECORD self entry")
        elif digest != "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(files[path]).digest()).rstrip(b"=").decode() or size != str(len(files[path])):
            raise _Rejected("RECORD content")
    return data


def _publish(runtime: Path, data: bytes) -> DerivedWheel:
    directory = runtime / "derived-wheels"
    canonicalize_root_path(directory, must_exist=False)
    directory.mkdir(exist_ok=True)
    directory = canonicalize_root_path(directory)
    if not directory.is_relative_to(runtime):
        raise _Rejected("Publication escape")
    digest = hashlib.sha256(data).hexdigest()
    target = directory / (digest + ".whl")
    if target.exists() or target.is_symlink():
        if _safe_bytes(target, MAX_WHEEL_BYTES) != data:
            raise _Rejected("Artifact collision")
    else:
        temporary: Path | None = None
        owned_identity = None
        published_by_us = False
        try:
            fd, name = tempfile.mkstemp(prefix=".wheel-", dir=directory)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                spool = os.fstat(stream.fileno())
                owned_identity = (spool.st_dev, spool.st_ino)
                if spool.st_nlink != 1:
                    raise _Rejected("Linked publish spool")
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            try:
                # Atomic create-if-absent: unlike replace/rename, link never
                # replaces an existing destination. No unsafe fallback.
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                if _safe_bytes(target, MAX_WHEEL_BYTES) != data:
                    raise _Rejected("Competing artifact collision")
            else:
                published_by_us = True
                # The final artifact must have nlink == 1 before validation.
                temporary.unlink()
                if _safe_bytes(target, MAX_WHEEL_BYTES) != data:
                    raise _Rejected("Published bytes mismatch")
        except BaseException:
            # Roll back only the inode created by this publication. Never remove
            # a pre-existing/concurrently supplied artifact at the same path.
            try:
                if published_by_us and (target.exists() or target.is_symlink()):
                    current = target.lstat()
                    if (current.st_dev, current.st_ino) == owned_identity:
                        target.unlink()
                    else:
                        raise _PublicationCleanupFailed("Publication ownership changed")
            except OSError as error:
                raise _PublicationCleanupFailed("Publication rollback incomplete") from error
            raise
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError as error:
                    raise _PublicationCleanupFailed("Publication spool cleanup incomplete") from error
    return DerivedWheel(target.relative_to(runtime).as_posix(), digest, len(data))
