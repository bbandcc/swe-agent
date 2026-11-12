"""Owned literal data only; every Python probe is intercepted before spawn."""
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.evaluation import python_toolchain as toolchain


class ToolchainBoundaryTests(unittest.TestCase):
    def assert_metadata_refused(self, relative, *, fields=None, error=None, dependency=False,
                                parent=False, stage=False, missing_attributes=False):
        """Inject data-only metadata at a real public staging/validation seam."""
        dependencies = self.root / "dependencies"
        dependency_data = b"# authored inert dependency\n"
        dependencies.mkdir(exist_ok=True)
        (dependencies / "package.py").write_bytes(dependency_data)
        target = self.root if parent else (dependencies if dependency else self.source) / relative
        secret = self.root / "external-sentinel.txt"
        secret.write_bytes(b"owned external sentinel; never read by toolchain")
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        actual_stat, actual_open = Path.stat, Path.open
        hits, secret_reads, processes = [], [], []

        def metadata(path, *args, **kwargs):
            info = actual_stat(path, *args, **kwargs)
            if path == target and kwargs.get("follow_symlinks") is False:
                hits.append(path)
                if error is not None:
                    raise error("authored metadata fault")
                values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                values.update(fields or {})
                if missing_attributes:
                    values.pop("st_file_attributes", None)
                return SimpleNamespace(**values)
            return info

        def opened(path, mode="r", *args, **kwargs):
            if path == secret and "r" in mode:
                secret_reads.append(path)
                raise AssertionError("External sentinel must never be read")
            return actual_open(path, mode, *args, **kwargs)

        def no_process(*_args, **_kwargs):
            processes.append(True)
            raise AssertionError("Inert fixture must never reach subprocess")

        with patch.object(Path, "stat", new=metadata), patch.object(Path, "open", new=opened), patch.object(
            toolchain.subprocess, "run", side_effect=no_process
        ), patch.object(toolchain, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")) as probe:
            with self.assertRaises(toolchain.PythonToolchainError):
                if dependency:
                    toolchain.stage_python_312(self.source, self.destination, reviewed_files=self.reviewed,
                        dependency_site_packages=dependencies,
                        reviewed_dependencies={"package.py": hashlib.sha256(dependency_data).hexdigest()})
                elif stage:
                    toolchain.stage_python_312(self.source, self.destination, reviewed_files=self.reviewed)
                else:
                    toolchain.validate_python_312(self.source, reviewed_files=self.reviewed)
        if not dependency:
            probe.assert_not_called()
        self.assertTrue(hits, "Fault never reached the intended metadata object")
        self.assertEqual(processes, [])
        self.assertEqual(secret_reads, [])
        self.assertFalse(self.destination.exists())
        self.assertEqual({p.relative_to(self.root).as_posix(): p.read_bytes()
                          for p in self.root.rglob("*") if p.is_file()}, before)

    def test_generic_reparse_file_refuses_before_execution(self):
        self.assert_metadata_refused("python.exe", fields={"st_file_attributes": 0x400, "st_reparse_tag": 0})

    def test_nonzero_reparse_tag_refuses_without_reparse_attribute(self):
        self.assert_metadata_refused("python.exe", fields={"st_file_attributes": 0x20, "st_reparse_tag": 0x8000001B})

    def test_unavailable_windows_attributes_refuse_before_probe(self):
        values = (0, None, -1, "unknown") if os.name == "nt" else (None, -1, "unknown")
        for value in values:
            with self.subTest(attributes=value):
                self.assert_metadata_refused("python.exe", fields={"st_file_attributes": value})
        if os.name == "nt":
            self.assert_metadata_refused("python.exe", missing_attributes=True)

    def test_invalid_reparse_tag_metadata_refuses_before_probe(self):
        for value in (None, -1, "unknown"):
            with self.subTest(tag=value):
                self.assert_metadata_refused("python.exe", fields={"st_reparse_tag": value})

    def test_metadata_permission_failure_refuses_before_probe(self):
        self.assert_metadata_refused("python.exe", error=PermissionError)

    def test_required_source_stat_disappearance_is_not_optional_leaf(self):
        self.assert_metadata_refused("python.exe", error=FileNotFoundError)

    def test_source_directory_generic_reparse_refuses_before_probe(self):
        for name in (".", "Lib", "Lib/encodings"):
            with self.subTest(directory=name):
                self.assert_metadata_refused(name, fields={"st_file_attributes": 0x410})

    def test_dependency_file_and_directory_generic_reparse_refuse_before_output(self):
        for name in (".", "package.py"):
            with self.subTest(dependency=name):
                self.assert_metadata_refused(name, fields={"st_file_attributes": 0x410}, dependency=True)

    def test_destination_parent_generic_reparse_refuses_before_probe_or_creation(self):
        self.assert_metadata_refused(".", fields={"st_file_attributes": 0x410}, parent=True, stage=True)

    def test_source_type_unconfirmable_refuses_before_probe(self):
        self.assert_metadata_refused("python.exe", fields={"st_mode": 0})

    def test_missing_required_reviewed_source_refuses_without_output(self):
        (self.source / "python.exe").unlink()
        with patch.object(toolchain, "_probe_python_312") as probe, patch.object(
            toolchain.subprocess, "run", side_effect=AssertionError("Missing source cannot execute")
        ):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.stage_python_312(self.source, self.destination, reviewed_files=self.reviewed)
        probe.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_normal_reviewed_source_validates_and_missing_create_leaf_stages(self):
        self.assertFalse(self.destination.exists())
        with patch.object(toolchain, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")), patch.object(
            toolchain.subprocess, "run", side_effect=AssertionError("Inert fixture cannot execute")
        ):
            self.assertEqual(toolchain.validate_python_312(self.source, reviewed_files=self.reviewed), self.source / "python.exe")
            self.assertEqual(toolchain.stage_python_312(self.source, self.destination, reviewed_files=self.reviewed), self.destination / "python.exe")
        for name, data in self.authored.items():
            self.assertEqual((self.source / name).read_bytes(), data)
            self.assertEqual((self.destination / name).read_bytes(), data)
            self.assertEqual(hashlib.sha256((self.destination / name).read_bytes()).hexdigest(), self.reviewed[name])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f2-toolchain-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "Lib/encodings").mkdir(parents=True)
        (self.source / "DLLs").mkdir()
        self.authored = {
            "python.exe": b"authored inert executable data; never executed",
            "Lib/encodings/__init__.py": b"# authored encoding fixture\n",
        }
        for name, data in self.authored.items():
            (self.source / name).write_bytes(data)
        self.reviewed = {name: hashlib.sha256(data).hexdigest() for name, data in self.authored.items()}
        self.destination = self.root / "staged"

    def test_unknown_executable_is_refused_before_host_probe(self):
        attempts = []

        def forbidden(*_args, **_kwargs):
            attempts.append("host probe")
            raise toolchain.PythonToolchainError("test sentinel blocked actual subprocess")

        with patch.object(toolchain.subprocess, "run", side_effect=forbidden):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.validate_python_312(self.source)
        self.assertEqual(attempts, [], "Unknown executable reached the host probe")

    def test_claimed_version_cannot_authorize_mismatched_source_bytes(self):
        attempts = []
        wrong = {**self.reviewed, "python.exe": "0" * 64}

        def forbidden(*_args, **_kwargs):
            attempts.append("host probe")
            raise toolchain.PythonToolchainError("test sentinel")

        with patch.object(toolchain.subprocess, "run", side_effect=forbidden):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.validate_python_312(self.source, reviewed_files=wrong)
        self.assertEqual(attempts, [], "Mismatched reviewed bytes reached execution")

    def stage(self):
        # The probe is an authored stdlib-only response; inert python.exe data
        # is never passed to a real process, including on an expected RED.
        with patch.object(toolchain, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")):
            return toolchain.stage_python_312(self.source, self.destination, reviewed_files=self.reviewed)

    def test_reviewed_fixed_source_preserves_successful_data_copy(self):
        executable = self.stage()
        self.assertEqual(executable, self.destination / "python.exe")
        for name, data in self.authored.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
            self.assertEqual((self.destination / name).stat().st_nlink, 1)

    def test_source_change_after_initial_review_is_not_published(self):
        original = Path.stat
        target = self.source / "Lib/encodings/__init__.py"
        changed = []

        def mutate(path, *args, **kwargs):
            if path == target and self.destination.exists() and not changed:
                changed.append(True)
                target.write_bytes(b"controlled source change after review\n")
            return original(path, *args, **kwargs)

        with patch.object(Path, "stat", new=mutate):
            with self.assertRaises(toolchain.PythonToolchainError):
                self.stage()
        self.assertEqual(changed, [True], "source-change fixture did not activate")
        self.assertFalse(self.destination.exists(), "failed staging left owned resources")

    def test_existing_destination_is_not_adopted_or_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / "python.exe"
        marker.write_bytes(b"preexisting caller-owned bytes")
        with self.assertRaises(toolchain.PythonToolchainError):
            self.stage()
        self.assertEqual(marker.read_bytes(), b"preexisting caller-owned bytes")

    def test_each_target_is_exclusively_created_and_failure_cleans_owned_storage(self):
        original = Path.open
        collisions = []

        def collide(path, mode="r", *args, **kwargs):
            if path == self.destination / "python.exe":
                collisions.append(mode)
                if mode == "xb":
                    raise FileExistsError("controlled exclusive-create collision")
                raise AssertionError("Output was opened with an overwriting mode")
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", new=collide):
            with self.assertRaises(toolchain.PythonToolchainError):
                self.stage()
        self.assertEqual(collisions, ["xb"])
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.source / "python.exe").read_bytes(), self.authored["python.exe"])

    def test_parent_link_is_refused_before_creating_or_writing_output(self):
        actual = toolchain._is_link
        with patch.object(toolchain, "_is_link", side_effect=lambda path: path == self.root or actual(path)):
            with self.assertRaises(toolchain.PythonToolchainError):
                self.stage()
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.source / "python.exe").read_bytes(), self.authored["python.exe"])

    def test_parent_identity_replacement_is_refused_before_creation(self):
        actual = toolchain._directory_identity
        calls = []

        def replace(path):
            value = actual(path)
            if path == self.root:
                calls.append(path)
                if len(calls) > 1:
                    return value[0], value[1] + 1  # identity substitute, no actual path redirection
            return value

        with patch.object(toolchain, "_directory_identity", side_effect=replace):
            with self.assertRaises(toolchain.PythonToolchainError):
                self.stage()
        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse(self.destination.exists())

    def test_changed_copied_bytes_are_not_published(self):
        original = Path.open
        changed = []
        target = self.destination / "python.exe"

        def corrupt(path, mode="r", *args, **kwargs):
            if path == target and mode == "rb" and not changed:
                changed.append(True)
                with original(path, "wb") as stream:
                    stream.write(b"controlled changed copied bytes")
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", new=corrupt):
            with self.assertRaises(toolchain.PythonToolchainError):
                self.stage()
        self.assertEqual(changed, [True])
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.source / "python.exe").read_bytes(), self.authored["python.exe"])

    def test_reviewed_dependency_bytes_are_copied_but_unreviewed_changes_refuse(self):
        dependencies = self.root / "dependencies"
        dependencies.mkdir()
        data = b"VALUE = 312\n"
        (dependencies / "package.py").write_bytes(data)
        reviewed = {"package.py": hashlib.sha256(data).hexdigest()}
        with patch.object(toolchain, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")):
            toolchain.stage_python_312(self.source, self.destination, reviewed_files=self.reviewed,
                dependency_site_packages=dependencies, reviewed_dependencies=reviewed)
            self.assertEqual((self.destination / "Lib/site-packages/package.py").read_bytes(), data)
            (dependencies / "package.py").write_bytes(b"unreviewed change")
            refused = self.root / "refused"
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.stage_python_312(self.source, refused, reviewed_files=self.reviewed,
                    dependency_site_packages=dependencies, reviewed_dependencies=reviewed)
        self.assertFalse(refused.exists())

    def test_unlisted_source_file_refuses_before_probe(self):
        (self.source / "startup.py").write_bytes(b"# unreviewed, never executed\n")
        with patch.object(toolchain.subprocess, "run", side_effect=AssertionError("Probe must not start")):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.validate_python_312(self.source, reviewed_files=self.reviewed)
        self.assertFalse(self.destination.exists())

    def test_replaced_source_handle_is_rejected_before_reading_other_bytes(self):
        protected = self.root / "protected.txt"
        protected.write_bytes(b"owned external sentinel, never a source input")
        original = Path.open
        reads = []

        class OtherHandle:
            def __init__(self):
                self.stream = original(protected, "rb")
            def __enter__(self): return self
            def __exit__(self, *_args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def read(self, *_args):
                reads.append(True)
                raise toolchain.PythonToolchainError("sentinel blocks external bytes")

        def redirect(path, mode="r", *args, **kwargs):
            if path == self.source / "python.exe" and mode == "rb":
                return OtherHandle()
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", new=redirect), patch.object(toolchain.subprocess, "run", side_effect=AssertionError("No probe")):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.validate_python_312(self.source, reviewed_files=self.reviewed)
        self.assertEqual(reads, [], "A replaced source handle was read before identity validation")
        self.assertEqual(protected.read_bytes(), b"owned external sentinel, never a source input")

    def test_source_directory_identity_change_refuses_before_probe(self):
        original = toolchain._directory_identity
        seen = []
        target = self.source / "Lib"

        def replace(path):
            value = original(path)
            if path == target:
                seen.append(True)
                if len(seen) > 1:
                    return value[0], value[1] + 1
            return value

        with patch.object(toolchain, "_directory_identity", side_effect=replace), patch.object(
            toolchain.subprocess, "run", side_effect=AssertionError("No probe after directory change")
        ):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.validate_python_312(self.source, reviewed_files=self.reviewed)
        self.assertGreaterEqual(len(seen), 2)
        self.assertFalse(self.destination.exists())

    def test_cleanup_fault_is_reported_then_owned_fixture_is_really_removed(self):
        original = Path.open

        def fail(path, mode="r", *args, **kwargs):
            if mode == "xb":
                raise OSError("controlled write failure")
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", new=fail), patch.object(
            toolchain.shutil, "rmtree", side_effect=OSError("controlled owner cleanup failure")
        ):
            with self.assertRaisesRegex(toolchain.PythonToolchainError, "cleanup failed"):
                self.stage()
        self.assertTrue(self.destination.is_dir())
        toolchain.shutil.rmtree(self.destination)  # Fault restored; owned test storage only.
        self.assertFalse(self.destination.exists())

    def test_empty_reviewed_dependencies_remain_supported(self):
        dependencies = self.root / "empty-dependencies"
        dependencies.mkdir()
        with patch.object(toolchain, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")):
            executable = toolchain.stage_python_312(self.source, self.destination,
                reviewed_files=self.reviewed, dependency_site_packages=dependencies, reviewed_dependencies={})
        self.assertTrue(executable.is_file())
        self.assertEqual(list((self.destination / "Lib/site-packages").iterdir()), [])

    def test_dependency_link_identity_is_refused_without_real_hardlinks(self):
        directory = self.root / "dependencies"
        directory.mkdir()
        target = directory / "package.py"
        data = b"# owned dependency data\n"
        target.write_bytes(data)
        original = Path.stat

        def linked(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == target:
                values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                values["st_nlink"] = 2
                return SimpleNamespace(**values)
            return info

        with patch.object(Path, "stat", new=linked), patch.object(
            toolchain, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")
        ):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.stage_python_312(self.source, self.destination,
                    dependency_site_packages=directory, reviewed_files=self.reviewed,
                    reviewed_dependencies={"package.py": hashlib.sha256(data).hexdigest()})
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(self.destination.exists())

    def test_unavailable_filesystem_identity_refuses_before_probe(self):
        original = Path.stat

        def unidentified(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.source / "python.exe":
                values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                values["st_ino"] = 0
                return SimpleNamespace(**values)
            return info

        with patch.object(Path, "stat", new=unidentified), patch.object(
            toolchain.subprocess, "run", side_effect=AssertionError("Unidentified source must not execute")
        ):
            with self.assertRaises(toolchain.PythonToolchainError):
                toolchain.validate_python_312(self.source, reviewed_files=self.reviewed)
