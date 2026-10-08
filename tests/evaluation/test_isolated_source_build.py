"""Contract tests at the fixed fuzzysearch build boundary."""
import unittest
from unittest.mock import patch

from agent.evaluation.isolated_source_build import (
    BuildErrorCode, BuildStatus, build_pinned_fuzzysearch_wheel,
)


class IsolatedSourceBuildTests(unittest.TestCase):
    def test_non_windows_has_no_fallback_or_artifact(self):
        with patch("agent.evaluation.isolated_source_build.platform.system", return_value="Linux"):
            result = build_pinned_fuzzysearch_wheel(
                repository_root="missing", task={}, runtime_root="missing",
                python_toolchain_root="missing", sdist_path="missing",
                setuptools_wheel_path="missing", wheel_wheel_path="missing",
            )
        self.assertEqual(result.status, BuildStatus.FAILED)
        self.assertEqual(result.error_code, BuildErrorCode.UNSUPPORTED_PLATFORM)
        self.assertTrue(result.cleanup_complete)
        self.assertIsNone(result.wheel)
        self.assertIsNone(result.provenance)

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

from agent.evaluation.windows_sandbox import SandboxResult, SandboxStatus


def wheel_bytes(name="fuzzysearch", version="0.7.3", *, malformed=None, authors=False):
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": b"# synthetic package; never executed by the host\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Dist: attrs>=19.3\n\n".encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
    }
    if authors:
        files[f"{dist}/AUTHORS.rst"] = b"Synthetic author attribution\n"
    record = f"{dist}/RECORD"
    rows = [[path, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), str(len(data))] for path, data in files.items()]
    rows.append([record, "", ""])
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    files[record] = output.getvalue().encode()
    if malformed == "record": files[f"{name}/__init__.py"] += b"tampered"
    if malformed == "pth": files["evil.pth"] = b"import evil"
    if malformed == "tag": files[f"{dist}/WHEEL"] = b"Root-Is-Purelib: false\nTag: cp312-cp312-win_amd64\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in files.items(): archive.writestr(zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0)), data)
    return buffer.getvalue()


class BuildContractTests(unittest.TestCase):
    """Synthetic fixed-input fixtures exercise the public orchestration seam.

    Pins are replaced only in these unit fixtures. The opt-in real test below
    uses production pins unchanged; synthetic tests are not build evidence.
    """
    def setUp(self):
        import agent.evaluation.isolated_source_build as module
        self.module = module
        token = patch.object(module, "_normal_user_token", return_value=True)
        token.start(); self.addCleanup(token.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="s5b2-build-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runtime = self.root / "runtime"
        (self.root / "python").mkdir()
        self.input_dir = self.root / "inputs"
        self.input_dir.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Build contract fixture")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path, data in [("fuzzysearch-0.7.3/setup.py", b"raise RuntimeError('HOST MUST NOT EXECUTE')\n"), ("fuzzysearch-0.7.3/src/fuzzysearch/__init__.py", b"fixture\n")]:
                member = tarfile.TarInfo(path); member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        self.source = buffer.getvalue()
        self.paths = []
        for key, filename, data, version in [
            ("SDIST", "fuzzysearch-0.7.3.tar.gz", self.source, "0.7.3"),
            ("SETUPTOOLS_WHEEL", "setuptools-75.8.0-py3-none-any.whl", wheel_bytes("setuptools", "75.8.0"), "75.8.0"),
            ("WHEEL_WHEEL", "wheel-0.45.1-py3-none-any.whl", wheel_bytes("wheel", "0.45.1"), "0.45.1"),
        ]:
            path = self.input_dir / filename; path.write_bytes(data); self.paths.append(path)
            identity = module.ArtifactIdentity(filename, version, "https://files.pythonhosted.org/fixture/" + filename, hashlib.sha256(data).hexdigest(), len(data))
            override = patch.object(module, key, identity); override.start(); self.addCleanup(override.stop)
        pin = module.SDIST
        lock = f'version = 1\n[[package]]\nname = "fuzzysearch"\nversion = "0.7.3"\nsdist = {{ url = "{pin.url}", hash = "sha256:{pin.sha256}", size = {pin.size} }}\n'.encode()
        (self.repo / "uv.lock").write_bytes(lock)
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_probe.py").write_bytes(b"# pinned evaluator fixture\n")
        self.git("add", "."); self.git("commit", "-qm", "fixture")
        self.revision = self.git("rev-parse", "HEAD").stdout.decode().strip()
        (self.repo / "implementation.py").write_bytes(b"# implementation fixture\n")
        self.git("add", "."); self.git("commit", "-qm", "oracle fixture")
        self.oracle_revision = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("checkout", "--detach", self.revision)
        self.task = {"task_id": "build-fixture", "task_text": "Build fixture",
            "task_text_sha256": hashlib.sha256(b"Build fixture").hexdigest(),
            "target": {"repo": module._REPOSITORY, "revision": self.revision},
            "allowed_edit_scope": [{"path": "implementation.py", "operation": "create"}],
            "oracle": {"revision": self.oracle_revision, "visibility": "evaluator_only_declared",
                "test_files": [{"path": "tests/test_probe.py", "sha256": hashlib.sha256(b"# pinned evaluator fixture\n").hexdigest()}]},
            "environment": {"os_family": "Windows", "architecture": "AMD64", "python_version": "3.12", "lock_path": "uv.lock", "lock_sha256": hashlib.sha256(lock).hexdigest()},
            "budget": {"max_steps": 10, "max_cost_usd": 1, "deadline_seconds": 60, "calibration": "provisional_unmeasured"},
            "known_environment_failures": [], "split": "dev", "stratum": "build-fixture",
            "provenance": {"kind": "historical_acceptance_slice", "commit": self.oracle_revision,
                "subject": "oracle fixture", "task_text_basis": "commit_subject_and_pinned_tests"}}
        self.manifest = self.root / "trusted-fixture.json"
        authority = patch.object(module, "_MANIFEST_PATH", self.manifest)
        authority.start(); self.addCleanup(authority.stop)
        self.write_trusted_fixture()

        platform = patch.object(module.platform, "system", return_value="Windows"); platform.start(); self.addCleanup(platform.stop)
        def stage(source, destination, **options):
            destination.mkdir(parents=True)
            (destination / "python.exe").write_bytes(b"fixture executable")
            shutil.copytree(options["dependency_site_packages"], destination / "Lib" / "site-packages")
            return destination / "python.exe"
        toolchain = patch.object(module, "stage_python_312", side_effect=stage); toolchain.start(); self.addCleanup(toolchain.stop)
        self.commands = []

    def write_trusted_fixture(self):
        from agent.evaluation.task_manifest import manifest_content_hash, validate_task_manifest
        self.task["checks"] = {phase: [{"check_id": phase + "-probe",
            "tests_revision": self.task["target"]["revision"] if phase == "baseline" else self.oracle_revision,
            "argv": ["python", "-m", "unittest", "tests.test_probe"], "cwd": ".",
            "shell": False, "timeout_seconds": 10}] for phase in ("baseline", "target", "regression")}
        document = {"schema_version": 1, "manifest_id": "build-fixture", "tasks": [self.task]}
        document["manifest_sha256"] = manifest_content_hash(document)
        validation = validate_task_manifest(document, repository_root=self.repo,
            expected_repository=self.module._REPOSITORY, expected_environment=self.task["environment"])
        self.assertTrue(validation.valid, validation.issues)
        self.manifest.write_text(json.dumps(document), encoding="utf-8")
        authority = patch.object(self.module, "_MANIFEST_HASH", document["manifest_sha256"])
        authority.start(); self.addCleanup(authority.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)

    def sandbox(self, command):
        self.commands.append(command)
        self.assertEqual(command.cwd, "source/fuzzysearch-0.7.3")
        self.assertEqual(command.argv[1:6], ("-B", "-S", "setup.py", "--noexts", "bdist_wheel"))
        self.assertEqual((command.workspace_root / command.cwd / "setup.py").read_bytes(), b"raise RuntimeError('HOST MUST NOT EXECUTE')\n")
        self.assertTrue((command.python_toolchain_root / "Lib/site-packages/setuptools/__init__.py").exists())
        (command.workspace_root / "out/fuzzysearch-0.7.3-py3-none-any.whl").write_bytes(wheel_bytes())
        return SandboxResult(SandboxStatus.COMPLETED, exit_code=0, started=True, token_is_appcontainer=True, cleanup_complete=True)

    def call(self, *, sandbox=None, **overrides):
        arguments = dict(repository_root=self.repo, task=self.task, runtime_root=self.runtime,
                         python_toolchain_root=self.root / "python", sdist_path=self.paths[0],
                         setuptools_wheel_path=self.paths[1], wheel_wheel_path=self.paths[2])
        arguments.update(overrides)
        with patch.object(self.module, "run_in_windows_appcontainer", side_effect=sandbox or self.sandbox):
            return build_pinned_fuzzysearch_wheel(**arguments)

    def test_success_publishes_only_after_cleanup_and_binds_provenance(self):
        before = self.git("status", "--porcelain").stdout
        result = self.call()
        self.assertEqual(result.status, BuildStatus.BUILT, result)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(result.provenance.task_id, "build-fixture")
        self.assertEqual(result.provenance.revision, self.revision)
        self.assertEqual(result.provenance.source, self.module.SDIST)
        self.assertEqual(result.provenance.origin, "DERIVED_FROM_PINNED_SDIST")
        artifact = self.runtime / result.wheel.relative_path
        self.assertEqual(artifact.read_bytes(), wheel_bytes())
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), result.wheel.sha256)
        self.assertFalse(self.commands[0].workspace_root.exists())
        self.assertEqual(self.git("status", "--porcelain").stdout, before)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.decode().strip(), self.revision)

    def test_audited_author_attribution_is_valid_with_complete_record(self):
        def build(command):
            result = self.sandbox(command)
            (command.workspace_root / "out/fuzzysearch-0.7.3-py3-none-any.whl").write_bytes(wheel_bytes(authors=True))
            return result
        result = self.call(sandbox=build)
        self.assertEqual(result.status, BuildStatus.BUILT, result)
        self.assertEqual((self.runtime / result.wheel.relative_path).read_bytes(), wheel_bytes(authors=True))
        self.assertTrue(result.cleanup_complete)

    def test_fabricated_task_id_cannot_receive_derived_provenance(self):
        task = json.loads(json.dumps(self.task))
        task["task_id"] = "fabricated-task"
        result = self.call(task=task)
        self.assertEqual(result.error_code, BuildErrorCode.INVALID_INPUT)
        self.assertIsNone(result.wheel)
        self.assertIsNone(result.provenance)
        self.assertEqual(self.commands, [])
        self.assertFalse(self.runtime.exists())

    def test_existing_commit_and_same_lock_cannot_rebind_task_revision(self):
        task = json.loads(json.dumps(self.task))
        task["target"]["revision"] = self.oracle_revision
        result = self.call(task=task)
        self.assertEqual(result.error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])
        self.assertIsNone(result.provenance)

    def test_rehashed_fabricated_manifest_cannot_replace_trusted_authority(self):
        from agent.evaluation.task_manifest import manifest_content_hash
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document["tasks"][0]["task_id"] = "fabricated-task"
        document["manifest_sha256"] = manifest_content_hash(document)
        self.manifest.write_text(json.dumps(document), encoding="utf-8")
        result = self.call(task=document["tasks"][0])
        self.assertEqual(result.error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])
        self.assertFalse(self.runtime.exists())

    def test_bad_sdist_hash_stops_before_dispatch(self):
        self.paths[0].write_bytes(b"invalid")
        result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.INVALID_INPUT)
        self.assertIsNone(result.wheel)
        self.assertEqual(self.commands, [])

    def test_bad_bootstrap_hash_stops_before_dispatch(self):
        self.paths[1].write_bytes(b"invalid")
        self.assertEqual(self.call().error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])

    def test_wrong_lock_and_revision_stop_before_dispatch(self):
        for value in ("0" * 64, "1" * 64):
            with self.subTest(hash=value):
                task = json.loads(json.dumps(self.task)); task["environment"]["lock_sha256"] = value
                self.assertEqual(self.call(task=task).error_code, BuildErrorCode.INVALID_INPUT)
        task = json.loads(json.dumps(self.task)); task["target"]["revision"] = "0" * 40
        self.assertEqual(self.call(task=task).error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])

    def test_current_worktree_lock_is_not_a_fallback(self):
        (self.repo / "uv.lock").write_bytes(b"changed current checkout\n")
        self.assertEqual(self.call().status, BuildStatus.BUILT)
        self.assertEqual((self.repo / "uv.lock").read_bytes(), b"changed current checkout\n")

    def test_invalid_python_architecture_is_a_toolchain_failure(self):
        from agent.evaluation.python_toolchain import PythonToolchainError
        with patch.object(self.module, "stage_python_312", side_effect=PythonToolchainError("wrong interpreter architecture")):
            result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.TOOLCHAIN_INVALID)
        self.assertEqual(self.commands, [])
        self.assertTrue(result.cleanup_complete)

    def test_elevated_or_appcontainer_caller_stops_before_staging(self):
        with patch.object(self.module, "_normal_user_token", return_value=False):
            result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.TOKEN_INVALID)
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.commands, [])

    def test_input_hardlink_is_rejected_without_modifying_source(self):
        alias = self.input_dir / "alias.tar.gz"
        try: os.link(self.paths[0], alias)
        except OSError as error: self.skipTest(f"Hardlink capability unavailable: {error}")
        before = self.paths[0].read_bytes()
        self.assertEqual(self.call(sdist_path=alias).error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.paths[0].read_bytes(), before)
        self.assertEqual(self.commands, [])

    def replace_source(self, members):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data, kind in members:
                member = tarfile.TarInfo(name); member.type = kind; member.size = len(data)
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE): member.linkname = "outside"
                archive.addfile(member, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        raw = buffer.getvalue(); self.paths[0].write_bytes(raw)
        pin = self.module.ArtifactIdentity(self.module.SDIST.filename, "0.7.3", self.module.SDIST.url, hashlib.sha256(raw).hexdigest(), len(raw))
        override = patch.object(self.module, "SDIST", pin); override.start(); self.addCleanup(override.stop)
        text = f'version = 1\n[[package]]\nname = "fuzzysearch"\nversion = "0.7.3"\nsdist = {{ url = "{pin.url}", hash = "sha256:{pin.sha256}", size = {pin.size} }}\n'.encode()
        (self.repo / "uv.lock").write_bytes(text)
        self.git("add", "uv.lock"); self.git("commit", "-qm", "unsafe fixture")
        self.task["target"]["revision"] = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.task["environment"]["lock_sha256"] = hashlib.sha256(text).hexdigest()
        self.write_trusted_fixture()

    def test_case_collision_in_ancestor_spelling_is_rejected(self):
        self.replace_source([
            ("fuzzysearch-0.7.3/setup.py", b"fixture", tarfile.REGTYPE),
            ("fuzzysearch-0.7.3/Foo/a.py", b"a", tarfile.REGTYPE),
            ("fuzzysearch-0.7.3/foo/b.py", b"b", tarfile.REGTYPE),
        ])
        self.assertEqual(self.call().error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])

    def test_archive_escape_is_rejected(self):
        self.replace_source([("fuzzysearch-0.7.3/setup.py", b"fixture", tarfile.REGTYPE), ("../outside", b"bad", tarfile.REGTYPE)])
        self.assertEqual(self.call().error_code, BuildErrorCode.INVALID_INPUT)
        self.assertFalse((self.runtime / "outside").exists())
        self.assertEqual(self.commands, [])

    def test_archive_link_is_rejected(self):
        self.replace_source([("fuzzysearch-0.7.3/setup.py", b"fixture", tarfile.REGTYPE), ("fuzzysearch-0.7.3/link", b"", tarfile.LNKTYPE)])
        self.assertEqual(self.call().error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])

    def test_archive_capacity_is_rejected(self):
        self.replace_source([("fuzzysearch-0.7.3/setup.py", b"x" * (4 * 1024 * 1024 + 1), tarfile.REGTYPE)])
        self.assertEqual(self.call().error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])

    def test_sandbox_failure_and_timeout_do_not_publish(self):
        for status, code in [(SandboxStatus.FAILED, BuildErrorCode.SANDBOX_FAILED), (SandboxStatus.TIMED_OUT, BuildErrorCode.TIMEOUT)]:
            with self.subTest(status=status):
                result = self.call(sandbox=lambda command: SandboxResult(status, exit_code=1, cleanup_complete=True))
                self.assertEqual(result.error_code, code)
                self.assertIsNone(result.wheel)
                self.assertTrue(result.cleanup_complete)

    def test_sandbox_cleanup_incomplete_cannot_publish(self):
        result = self.call(sandbox=lambda command: SandboxResult(SandboxStatus.COMPLETED, exit_code=0, started=True, token_is_appcontainer=True, cleanup_complete=False))
        self.assertEqual(result.error_code, BuildErrorCode.CLEANUP_FAILED)
        self.assertFalse(result.cleanup_complete)
        self.assertIsNone(result.wheel)

    def test_exit_zero_without_appcontainer_token_is_not_success(self):
        result = self.call(sandbox=lambda command: SandboxResult(SandboxStatus.COMPLETED, exit_code=0, started=True, token_is_appcontainer=False, cleanup_complete=True))
        self.assertEqual(result.error_code, BuildErrorCode.SANDBOX_FAILED)
        self.assertIsNone(result.wheel)

    def test_missing_and_multiple_output_are_not_success(self):
        def multiple(command):
            result = self.sandbox(command)
            (command.workspace_root / "out/extra.whl").write_bytes(b"extra")
            return result
        for callback in [lambda command: SandboxResult(SandboxStatus.COMPLETED, exit_code=0, started=True, token_is_appcontainer=True, cleanup_complete=True), multiple]:
            result = self.call(sandbox=callback)
            self.assertEqual(result.error_code, BuildErrorCode.OUTPUT_INVALID)
            self.assertIsNone(result.wheel)

    def test_invalid_wheel_record_tag_and_startup_hook_are_rejected(self):
        for kind in ("record", "tag", "pth"):
            with self.subTest(kind=kind):
                def invalid(command):
                    result = self.sandbox(command)
                    (command.workspace_root / "out/fuzzysearch-0.7.3-py3-none-any.whl").write_bytes(wheel_bytes(malformed=kind))
                    return result
                result = self.call(sandbox=invalid)
                self.assertEqual(result.error_code, BuildErrorCode.OUTPUT_INVALID)
                self.assertIsNone(result.wheel)
                self.assertIsNone(result.provenance)

    def test_build_workspace_cleanup_failure_does_not_publish(self):
        remove = shutil.rmtree
        def fail_build(path, *args, **kwargs):
            if Path(path).name.startswith("isolated-build-"): raise PermissionError("cleanup denied")
            return remove(path, *args, **kwargs)
        with patch("shutil.rmtree", side_effect=fail_build): result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.CLEANUP_FAILED)
        self.assertFalse(result.cleanup_complete)
        self.assertIsNone(result.wheel)
        self.assertFalse((self.runtime / "derived-wheels").exists())

    def test_unexpected_runtime_error_propagates_after_cleanup(self):
        seen = []
        def broken(command):
            seen.append(command.workspace_root)
            raise RuntimeError("unexpected programming bug")
        with self.assertRaisesRegex(RuntimeError, "unexpected programming bug"): self.call(sandbox=broken)
        self.assertFalse(seen[0].exists())

    def test_publication_permission_failure_is_structured(self):
        with patch("os.link", side_effect=PermissionError("publish denied")):
            result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.PUBLISH_FAILED)
        self.assertIsNone(result.wheel)
        self.assertIsNone(result.provenance)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(list((self.runtime / "derived-wheels").iterdir()), [])

    def test_post_publication_validation_failure_removes_only_new_artifact(self):
        target = self.runtime / "derived-wheels" / (hashlib.sha256(wheel_bytes()).hexdigest() + ".whl")
        original_link, original_open = os.link, Path.open
        published = False
        def link(source, destination, *args, **kwargs):
            nonlocal published
            value = original_link(source, destination, *args, **kwargs)
            if Path(destination) == target: published = True
            return value
        def fail_after_replace(path, *args, **kwargs):
            if published and path == target:
                raise PermissionError("post-replace verification denied")
            return original_open(path, *args, **kwargs)
        with patch("os.link", side_effect=link), patch.object(Path, "open", fail_after_replace):
            result = self.call()
        self.assertTrue(published)
        self.assertEqual(result.error_code, BuildErrorCode.PUBLISH_FAILED)
        self.assertIsNone(result.wheel)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_existing_same_hash_artifact_is_never_replaced_or_removed(self):
        first = self.call()
        target = self.runtime / first.wheel.relative_path
        before = (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_dev, target.stat().st_ino)
        with patch("os.link", side_effect=AssertionError("existing artifact must not be republished")), patch("os.replace", side_effect=AssertionError("replace forbidden")):
            repeated = self.call()
        self.assertEqual(repeated.status, BuildStatus.BUILT)
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_dev, target.stat().st_ino), before)
        original_open = Path.open
        def reject_read(path, *args, **kwargs):
            if path == target: raise PermissionError("existing validation unavailable")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", reject_read):
            failed = self.call()
        self.assertEqual(failed.error_code, BuildErrorCode.PUBLISH_FAILED)
        self.assertIsNone(failed.wheel)
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_dev, target.stat().st_ino), before)

    def test_publication_race_never_overwrites_or_removes_competitor(self):
        target = self.runtime / "derived-wheels" / (hashlib.sha256(wheel_bytes()).hexdigest() + ".whl")
        replace, link = os.replace, os.link
        for competing_bytes in (wheel_bytes(), b"different competing artifact"):
            with self.subTest(correct=competing_bytes == wheel_bytes()):
                if target.exists(): target.unlink()
                before = None
                def compete(operation, source, destination, *args, **kwargs):
                    nonlocal before
                    self.assertEqual(Path(destination), target)
                    target.write_bytes(competing_bytes)
                    os.utime(target, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
                    info = target.stat()
                    before = (target.read_bytes(), info.st_mtime_ns, info.st_dev, info.st_ino)
                    return operation(source, destination, *args, **kwargs)
                with patch("os.replace", side_effect=lambda *args, **kw: compete(replace, *args, **kw)), patch("os.link", side_effect=lambda *args, **kw: compete(link, *args, **kw)):
                    result = self.call()
                self.assertIsNotNone(before)
                info = target.stat()
                self.assertEqual((target.read_bytes(), info.st_mtime_ns, info.st_dev, info.st_ino), before)
                if competing_bytes != wheel_bytes():
                    self.assertEqual(result.error_code, BuildErrorCode.PUBLISH_FAILED)
                    self.assertIsNone(result.wheel)
                    self.assertIsNone(result.provenance)
                else:
                    self.assertIn(result.status, (BuildStatus.BUILT, BuildStatus.FAILED))
                self.assertEqual(list(target.parent.glob(".wheel-*")), [])

    def test_unsupported_atomic_publication_has_no_replace_fallback(self):
        import errno
        with patch("os.link", side_effect=OSError(errno.ENOTSUP, "Atomic publication unsupported")), patch("os.replace", side_effect=AssertionError("Unsafe fallback")):
            result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.PUBLISH_FAILED)
        self.assertEqual(result.status, BuildStatus.FAILED)
        self.assertTrue(result.cleanup_complete)
        self.assertIsNone(result.wheel)
        self.assertIsNone(result.provenance)
        self.assertEqual(list((self.runtime / "derived-wheels").iterdir()), [])

    def test_publication_spool_cleanup_failure_preserves_competitor(self):
        target = self.runtime / "derived-wheels" / (hashlib.sha256(wheel_bytes()).hexdigest() + ".whl")
        original_unlink, original_link = Path.unlink, os.link
        for competing in (False, True):
            with self.subTest(competing=competing):
                before = None
                def link(source, destination, **kwargs):
                    nonlocal before
                    if competing:
                        target.write_bytes(wheel_bytes())
                        info = target.stat()
                        before = (target.read_bytes(), info.st_mtime_ns, info.st_dev, info.st_ino)
                    return original_link(source, destination, **kwargs)
                def unlink(path, *args, **kwargs):
                    if path.name.startswith(".wheel-"):
                        raise PermissionError("Spool cleanup denied")
                    return original_unlink(path, *args, **kwargs)
                with patch("os.link", side_effect=link), patch.object(Path, "unlink", unlink):
                    result = self.call()
                self.assertEqual(result.status, BuildStatus.FAILED)
                self.assertEqual(result.error_code, BuildErrorCode.CLEANUP_FAILED)
                self.assertFalse(result.cleanup_complete)
                self.assertIsNone(result.wheel)
                self.assertIsNone(result.provenance)
                if competing:
                    info = target.stat()
                    self.assertEqual((target.read_bytes(), info.st_mtime_ns, info.st_dev, info.st_ino), before)
                    target.unlink()
                else:
                    self.assertFalse(target.exists())
                for spool in target.parent.glob(".wheel-*"):
                    spool.unlink()

    def test_runtime_overlap_is_rejected_before_dispatch(self):
        for root in (self.repo, self.repo / "runtime", self.repo.parent):
            with self.subTest(root=root):
                self.assertEqual(self.call(runtime_root=root).error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])

    def test_injected_staging_permission_error_is_not_success(self):
        original = Path.open
        def fail(path, *args, **kwargs):
            if path.name == "setup.py" and args and args[0] == "xb": raise PermissionError("stage denied")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", fail): result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.INVALID_INPUT)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(self.commands, [])

    def test_source_archive_entry_cap_is_checked_before_dispatch(self):
        members = [("fuzzysearch-0.7.3/setup.py", b"fixture", tarfile.REGTYPE)]
        members += [(f"fuzzysearch-0.7.3/p{i}", b"", tarfile.REGTYPE) for i in range(512)]
        self.replace_source(members)
        self.assertEqual(self.call().error_code, BuildErrorCode.INVALID_INPUT)
        self.assertEqual(self.commands, [])

    def test_output_size_cap_is_not_post_capture_truncation(self):
        def oversized(command):
            result = self.sandbox(command)
            with (command.workspace_root / "out/fuzzysearch-0.7.3-py3-none-any.whl").open("wb") as stream:
                stream.truncate(4 * 1024 * 1024 + 1)
            return result
        result = self.call(sandbox=oversized)
        self.assertEqual(result.error_code, BuildErrorCode.OUTPUT_INVALID)
        self.assertIsNone(result.wheel)

    def test_output_hardlink_is_rejected_without_mutating_host_file(self):
        host = self.input_dir / "host.whl"; host.write_bytes(wheel_bytes())
        before = host.stat().st_mtime_ns
        def linked(command):
            result = self.sandbox(command)
            path = command.workspace_root / "out/fuzzysearch-0.7.3-py3-none-any.whl"
            path.unlink()
            try: os.link(host, path)
            except OSError as error: self.skipTest(f"Hardlink capability unavailable: {error}")
            return result
        result = self.call(sandbox=linked)
        self.assertEqual(result.error_code, BuildErrorCode.OUTPUT_INVALID)
        self.assertEqual(host.read_bytes(), wheel_bytes())
        self.assertEqual(host.stat().st_mtime_ns, before)

    def test_symlink_input_and_publication_directory_fail_closed(self):
        alias = self.input_dir / "source-alias"
        try: alias.symlink_to(self.paths[0])
        except OSError as error: self.skipTest(f"Symlink capability unavailable: {error}")
        self.assertEqual(self.call(sdist_path=alias).error_code, BuildErrorCode.INVALID_INPUT)
        self.runtime.mkdir(exist_ok=True)
        outside = self.root / "outside"; outside.mkdir()
        (self.runtime / "derived-wheels").symlink_to(outside, target_is_directory=True)
        result = self.call()
        self.assertEqual(result.error_code, BuildErrorCode.PUBLISH_FAILED)
        self.assertEqual(list(outside.iterdir()), [])

    def test_correct_looking_wheel_with_wrong_package_is_rejected(self):
        def wrong(command):
            result = self.sandbox(command)
            (command.workspace_root / "out/fuzzysearch-0.7.3-py3-none-any.whl").write_bytes(wheel_bytes("other", "0.7.3"))
            return result
        self.assertEqual(self.call(sandbox=wrong).error_code, BuildErrorCode.OUTPUT_INVALID)

@unittest.skipUnless(
    os.name == "nt" and os.environ.get("S5B_RUN_APPCONTAINER_TESTS") == "1" and os.environ.get("S5B_RUN_ISOLATED_BUILD_TESTS") == "1",
    "Requires explicit normal-user Windows isolated-build canary; a skip is not build evidence.",
)
class RealIsolatedBuildTests(unittest.TestCase):
    """Production pins and real backend only. Never runs a task/oracle/model."""
    def setUp(self):
        from tests.evaluation.real_appcontainer_preflight import require_normal_user_token
        require_normal_user_token()  # Before downloads, profile creation, ACL, or canaries.
        import agent.evaluation.isolated_source_build as module
        import sys
        self.module = module
        self.python = Path(os.environ.get("S5B_BUILD_PYTHON_ROOT", sys.base_prefix))
        self.temp = tempfile.TemporaryDirectory(prefix="s5b2-real-build-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inputs = self.root / "inputs"; self.inputs.mkdir()
        self.repository = Path(__file__).resolve().parents[2]
        manifest = json.loads((self.repository / "evals/s5a/tasks.v1.json").read_text(encoding="utf-8"))
        self.task = next(task for task in manifest["tasks"] if task["task_id"] == "symbols-tsx-exact-source-v1")
        self.assertEqual(self.task["target"]["revision"], "c8b0840812a7b2f1d7d56450bdea908d623a7606")
        self.assertEqual(self.task["environment"]["lock_sha256"], "c270b21033292b4c578ca30cbe06a79718a33f272d2a05570aaaa31f57dca04f")
        self.before_head = self.git("rev-parse", "HEAD")
        self.before_status = self.git("status", "--porcelain")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repository), *args], shell=False, check=True, stdout=subprocess.PIPE).stdout

    def download(self, pin):
        import urllib.request
        from urllib.parse import urlparse
        import stat
        local = os.environ.get("S5B_BUILD_INPUT_DIR")
        if local:
            root = Path(local)
            if not root.is_absolute():
                raise ValueError("Pinned input directory must be absolute")
            path = root / pin.filename
            current = Path(path.anchor)
            for part in path.parts[1:]:
                current /= part
                if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                    raise ValueError("Pinned input has an unsafe link")
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != pin.size:
                    raise ValueError("Pinned input type/link/size mismatch")
                data = stream.read(pin.size + 1)
            if len(data) != pin.size or hashlib.sha256(data).hexdigest() != pin.sha256:
                raise ValueError("Pinned input SHA-256/size mismatch")
            return path
        with urllib.request.urlopen(pin.url, timeout=30) as response:
            self.assertEqual(urlparse(response.url).hostname, "files.pythonhosted.org")
            data = response.read(pin.size + 1)
        self.assertEqual(len(data), pin.size)
        self.assertEqual(hashlib.sha256(data).hexdigest(), pin.sha256)
        path = self.inputs / pin.filename
        path.write_bytes(data)
        return path

    def copied_packages(self, archives, destination):
        """Read-only, authenticated test inputs; no host import/install/hooks."""
        destination.mkdir()
        for path in archives:
            with zipfile.ZipFile(path) as archive:
                self.assertLess(len(archive.infolist()), 4096)
                for member in archive.infolist():
                    relative = Path(member.filename)
                    self.assertFalse(relative.is_absolute() or ".." in relative.parts or "\\" in member.filename)
                    self.assertLessEqual(member.file_size, 4 * 1024 * 1024)
                    if member.is_dir() or member.filename.endswith(".pth"): continue
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source:
                        data = source.read(4 * 1024 * 1024 + 1)
                    self.assertEqual(len(data), member.file_size)
                    target.write_bytes(data)

    def test_real_pinned_build_and_read_only_dependency_import(self):
        from agent.evaluation.python_toolchain import stage_python_312
        from agent.evaluation.windows_sandbox import SandboxCommand, run_in_windows_appcontainer
        source = self.download(self.module.SDIST)
        setuptools = self.download(self.module.SETUPTOOLS_WHEEL)
        wheel = self.download(self.module.WHEEL_WHEEL)
        inputs_before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (source, setuptools, wheel)}
        # Positive bootstrap/toolchain control: executable startup failure cannot
        # masquerade as host/source/write denial or build success.
        control_parent = self.root / "control"; control_parent.mkdir()
        control = control_parent / "workspace"; control.mkdir()
        packages = self.root / "control-packages"
        self.copied_packages((setuptools, wheel), packages)
        python = stage_python_312(self.python, control_parent / "toolchain-control", dependency_site_packages=packages)
        bootstrap = run_in_windows_appcontainer(SandboxCommand(
            workspace_root=control, python_toolchain_root=python.parent,
            argv=(str(python), "-B", "-S", "-c", "import setuptools,wheel; print('BOOTSTRAP_IMPORT_OK')"), timeout_seconds=20,
        ))
        self.assertEqual(bootstrap.status, SandboxStatus.COMPLETED, bootstrap)
        self.assertTrue(bootstrap.started and bootstrap.token_is_appcontainer and bootstrap.cleanup_complete)
        self.assertIn(b"BOOTSTRAP_IMPORT_OK", bootstrap.stdout)
        with patch.dict(os.environ, {"PYTHONPATH": "S5B_PARENT_PYTHONPATH_CANARY", "S5B_BUILD_SECRET_CANARY": "S5B_SECRET_CANARY"}):
            result = build_pinned_fuzzysearch_wheel(
                repository_root=self.repository, task=self.task, runtime_root=self.root / "runtime",
                python_toolchain_root=self.python, sdist_path=source,
                setuptools_wheel_path=setuptools, wheel_wheel_path=wheel,
            )
        self.assertEqual(result.status, BuildStatus.BUILT, result)
        self.assertTrue(result.cleanup_complete)
        self.assertTrue(result.sandbox_result.started and result.sandbox_result.token_is_appcontainer)
        self.assertEqual(result.provenance.source, self.module.SDIST)
        self.assertEqual(result.provenance.lock_sha256, self.task["environment"]["lock_sha256"])
        artifact = self.root / "runtime" / result.wheel.relative_path
        self.assertEqual(len(artifact.read_bytes()), result.wheel.size)
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), result.wheel.sha256)
        # Functional smoke is also isolated. attrs is a historical runtime wheel,
        # not a new build requirement and never enters frozen admission here.
        attrs = self.download(self.module.ArtifactIdentity(
            "attrs-25.1.0-py3-none-any.whl", "25.1.0",
            "https://files.pythonhosted.org/packages/fc/30/d4986a882011f9df997a55e6becd864812ccfcd821d64aac8570ee39f719/attrs-25.1.0-py3-none-any.whl",
            "c75a69e28a550a7e93789579c22aa26b0f5b83b75dc4e08fe092980051e1090a", 63152,
        ))
        smoke_parent = self.root / "smoke"; smoke_parent.mkdir()
        smoke = smoke_parent / "workspace"; smoke.mkdir()
        dependency_source = self.root / "smoke-packages"
        self.copied_packages((artifact, attrs), dependency_source)
        python = stage_python_312(self.python, smoke_parent / "toolchain-smoke", dependency_site_packages=dependency_source)
        oracle = self.root / "oracle.txt"; oracle.write_bytes(b"S5B_ORACLE_CANARY")
        host = self.root / "host.txt"; host.write_bytes(b"S5B_HOST_CANARY")
        staged = python.parent / "Lib/site-packages/fuzzysearch/__init__.py"
        staged_before = staged.read_bytes()
        source_paths = [host, oracle, source, dependency_source / "fuzzysearch/__init__.py"]
        script = (
            "import os,pathlib,fuzzysearch; "
            "assert fuzzysearch.find_near_matches('abc','abc',max_l_dist=0); "
            "assert 'S5B_BUILD_SECRET_CANARY' not in os.environ; "
            "assert 'S5B_PARENT_PYTHONPATH_CANARY' not in os.environ.get('PYTHONPATH','');\n"
            f"for name in {list(map(str, source_paths))!r}:\n"
            " try: pathlib.Path(name).read_bytes()\n"
            " except PermissionError: pass\n"
            " else: raise AssertionError('host/oracle/source accessible')\n"
            f"for name in {list(map(str, [host, oracle, staged]))!r}:\n"
            " try: pathlib.Path(name).write_bytes(b'tampered')\n"
            " except PermissionError: pass\n"
            " else: raise AssertionError('protected write allowed')\n"
            "pathlib.Path('writable.txt').write_bytes(b'ok'); print('ISOLATED_IMPORT_OK')"
        )
        with patch.dict(os.environ, {"PYTHONPATH": "S5B_PARENT_PYTHONPATH_CANARY", "S5B_BUILD_SECRET_CANARY": "S5B_SECRET_CANARY"}):
            smoke_result = run_in_windows_appcontainer(SandboxCommand(
                workspace_root=smoke, python_toolchain_root=python.parent,
                argv=(str(python), "-B", "-S", "-c", script), timeout_seconds=20,
            ))
        self.assertEqual(smoke_result.status, SandboxStatus.COMPLETED, smoke_result)
        self.assertTrue(smoke_result.started and smoke_result.token_is_appcontainer and smoke_result.cleanup_complete)
        self.assertIn(b"ISOLATED_IMPORT_OK", smoke_result.stdout)
        self.assertEqual(staged.read_bytes(), staged_before)
        self.assertEqual(host.read_bytes(), b"S5B_HOST_CANARY")
        self.assertEqual(oracle.read_bytes(), b"S5B_ORACLE_CANARY")
        for path, (data, mtime) in inputs_before.items():
            self.assertEqual(path.read_bytes(), data); self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before_head)
        self.assertEqual(self.git("status", "--porcelain"), self.before_status)
        self.assertFalse(list((self.root / "runtime").glob("isolated-build-*")))
        print("ISOLATED_BUILD_RECEIPT=" + json.dumps({
            "task_id": result.provenance.task_id, "revision": result.provenance.revision,
            "lock_sha256": result.provenance.lock_sha256,
            "wheel_sha256": result.wheel.sha256, "wheel_size": result.wheel.size,
            "recipe_sha256": result.provenance.recipe_sha256,
            "bootstrap_import": "passed", "isolated_import": "passed",
            "token_is_appcontainer": result.sandbox_result.token_is_appcontainer,
            "cleanup_complete": result.cleanup_complete,
            "duration_seconds": result.sandbox_result.duration_seconds,
            "cpu_time_seconds": result.sandbox_result.cpu_time_seconds,
            "peak_memory_bytes": result.sandbox_result.peak_memory_bytes,
        }, sort_keys=True))


class CanaryInputTests(unittest.TestCase):
    def test_explicit_local_pin_uses_no_network(self):
        from agent.evaluation.isolated_source_build import ArtifactIdentity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data = b"authenticated input fixture"
            pin = ArtifactIdentity("pinned.bin", "1", "https://files.pythonhosted.org/pinned.bin", hashlib.sha256(data).hexdigest(), len(data))
            path = root / pin.filename; path.write_bytes(data)
            canary = RealIsolatedBuildTests("test_real_pinned_build_and_read_only_dependency_import")
            canary.inputs = root / "unused"
            with patch.dict(os.environ, {"S5B_BUILD_INPUT_DIR": str(root)}), patch("urllib.request.urlopen", side_effect=AssertionError("local input must not download")):
                self.assertEqual(canary.download(pin), path)

    def test_missing_or_tampered_local_pin_cannot_download_a_fallback(self):
        from agent.evaluation.isolated_source_build import ArtifactIdentity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data = b"authenticated input fixture"
            pin = ArtifactIdentity("pinned.bin", "1", "https://files.pythonhosted.org/pinned.bin", hashlib.sha256(data).hexdigest(), len(data))
            canary = RealIsolatedBuildTests("test_real_pinned_build_and_read_only_dependency_import")
            with patch.dict(os.environ, {"S5B_BUILD_INPUT_DIR": str(root)}), patch("urllib.request.urlopen", side_effect=AssertionError("no fallback")):
                with self.assertRaises(FileNotFoundError): canary.download(pin)
                path = root / pin.filename
                path.write_bytes(b"x" * len(data))
                with self.assertRaisesRegex(ValueError, "SHA-256"): canary.download(pin)
                path.write_bytes(data)
                alias = root / "alias.bin"
                try: os.link(path, alias)
                except OSError as error: self.skipTest(f"Hardlink capability unavailable: {error}")
                with self.assertRaisesRegex(ValueError, "link"): canary.download(pin)
