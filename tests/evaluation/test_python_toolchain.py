from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.evaluation.python_toolchain import PythonToolchainError, stage_python_312


class PythonToolchainDependencyStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="s5b-python-stage-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.python = self.root / "cpython"
        (self.python / "Lib" / "encodings").mkdir(parents=True)
        (self.python / "Lib" / "encodings" / "__init__.py").write_text("", encoding="utf-8")
        (self.python / "DLLs").mkdir()
        (self.python / "python.exe").write_bytes(b"trusted CPython executable placeholder")
        self.dependencies = self.root / "dependencies" / "Lib" / "site-packages"
        package = self.dependencies / "frozen_package"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = 312\n", encoding="utf-8", newline="")
        # Explicit hashes of authored fixture bytes; never capture unknown files.
        self.reviewed = {
            "python.exe": hashlib.sha256(b"trusted CPython executable placeholder").hexdigest(),
            "Lib/encodings/__init__.py": hashlib.sha256(b"").hexdigest(),
        }
        self.reviewed_dependencies = {
            "frozen_package/__init__.py": hashlib.sha256(b"VALUE = 312\n").hexdigest(),
        }

    def _probe_as_cpython_312(self, command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, b"CPython|3.12|64|AMD64|win-amd64\n", b""
        )

    def test_python_executable_must_prove_64_bit_amd64_identity(self) -> None:
        from agent.evaluation.python_toolchain import validate_python_312

        with patch(
            "agent.evaluation.python_toolchain._probe_python_312",
            return_value=("CPython", "3.12", "32", "ARM64", "win-arm64"),
        ):
            with self.assertRaisesRegex(PythonToolchainError, "AMD64"):
                validate_python_312(self.python, expected_architecture="AMD64", reviewed_files=self.reviewed)

    def test_dependency_tree_is_copied_under_staged_python_and_bounded_contract(self) -> None:
        destination = self.root / "staged"
        with patch("agent.evaluation.python_toolchain.subprocess.run", side_effect=self._probe_as_cpython_312):
            executable = stage_python_312(
                self.python,
                destination,
                dependency_site_packages=self.dependencies,
                reviewed_files=self.reviewed,
                reviewed_dependencies=self.reviewed_dependencies,
            )
        self.assertEqual(executable, destination / "python.exe")
        copied = destination / "Lib" / "site-packages" / "frozen_package" / "__init__.py"
        self.assertEqual(copied.read_bytes(), (self.dependencies / "frozen_package" / "__init__.py").read_bytes())
        self.assertEqual(copied.stat().st_nlink, 1)

    def test_hardlinked_dependency_file_is_rejected(self) -> None:
        linked = self.dependencies / "frozen_package" / "linked.py"
        original = self.root / "outside.py"
        original.write_bytes(b"outside")
        try:
            linked.hardlink_to(original)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"filesystem does not support hard links: {error}")
        with patch("agent.evaluation.python_toolchain.subprocess.run", side_effect=self._probe_as_cpython_312):
            with self.assertRaises(PythonToolchainError):
                stage_python_312(
                    self.python,
                    self.root / "staged",
                    dependency_site_packages=self.dependencies,
                    reviewed_files=self.reviewed,
                    reviewed_dependencies=self.reviewed_dependencies,
                )
        self.assertEqual(original.read_bytes(), b"outside")

    def test_dependency_copy_obeys_combined_file_hard_limit(self) -> None:
        from agent.evaluation import python_toolchain

        with (
            patch("agent.evaluation.python_toolchain.subprocess.run", side_effect=self._probe_as_cpython_312),
            patch.object(python_toolchain, "_MAX_FILES", 2),
        ):
            with self.assertRaises(PythonToolchainError):
                stage_python_312(
                    self.python,
                    self.root / "staged",
                    dependency_site_packages=self.dependencies,
                    reviewed_files=self.reviewed,
                    reviewed_dependencies=self.reviewed_dependencies,
                )
        self.assertFalse((self.root / "staged" / "Lib" / "site-packages" / "frozen_package" / "__init__.py").exists())

    def test_dependency_symlink_is_rejected_when_supported(self) -> None:
        outside = self.root / "outside.py"
        outside.write_text("secret = True\n", encoding="utf-8")
        link = self.dependencies / "frozen_package" / "linked.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"filesystem does not support symlinks: {error}")
        with patch("agent.evaluation.python_toolchain.subprocess.run", side_effect=self._probe_as_cpython_312):
            with self.assertRaises(PythonToolchainError):
                stage_python_312(
                    self.python,
                    self.root / "staged",
                    dependency_site_packages=self.dependencies,
                    reviewed_files=self.reviewed,
                    reviewed_dependencies=self.reviewed_dependencies,
                )
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret = True\n")

    @unittest.skipUnless(os.name == "nt", "directory junctions are Windows path types")
    def test_dependency_junction_is_rejected(self) -> None:
        external = self.root / "outside-dependency"
        external.mkdir()
        outside_file = external / "outside.py"
        outside_file.write_bytes(b"outside dependency bytes")
        junction = self.dependencies / "frozen_package" / "linked-directory"
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        result = subprocess.run(
            [str(cmd), "/d", "/c", "mklink", "/J", str(junction), str(external)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            self.skipTest(f"junction creation unavailable: {result.stderr.decode(errors='replace')}")
        with patch("agent.evaluation.python_toolchain.subprocess.run", side_effect=self._probe_as_cpython_312):
            with self.assertRaises(PythonToolchainError):
                stage_python_312(
                    self.python,
                    self.root / "staged",
                    dependency_site_packages=self.dependencies,
                    reviewed_files=self.reviewed,
                    reviewed_dependencies=self.reviewed_dependencies,
                )
        self.assertEqual(outside_file.read_bytes(), b"outside dependency bytes")


class FixedNativeDllStagingTests(unittest.TestCase):
    def setUp(self):
        PythonToolchainDependencyStagingTests.setUp(self)
        import hashlib, struct
        from agent.evaluation import python_toolchain as module
        self.module = module
        def image(payload):
            data = bytearray(512); data[:2] = b"MZ"
            struct.pack_into("<I", data, 60, 128); data[128:132] = b"PE\0\0"
            struct.pack_into("<H", data, 132, 0x8664)
            struct.pack_into("<H", data, 152, 0x20b)
            return bytes(data) + payload
        self.native = image(b"reviewed ffi fixture")
        self.profile = {}
        for name in ("python.exe", "python312.dll", "DLLs/_ctypes.pyd"):
            data = image(name.encode())
            (self.python / name).write_bytes(data)
            self.profile[name] = (len(data), hashlib.sha256(data).hexdigest())
        self.ffi = self.python / "Library/bin/ffi-8.dll"
        self.ffi.parent.mkdir(parents=True)
        self.ffi.write_bytes(self.native)
        self.pins = (len(self.native), hashlib.sha256(self.native).hexdigest())
        self.reviewed = {
            "Lib/encodings/__init__.py": hashlib.sha256(b"").hexdigest(),
            **{name: digest for name, (_, digest) in self.profile.items()},
            "Library/bin/ffi-8.dll": self.pins[1],
        }

    def _stage_fixed(self, destination=None, version="3.12.3"):
        with patch.object(self.module, "_NATIVE_PROFILE", self.profile, create=True), patch.object(self.module, "_FFI_IDENTITY", self.pins, create=True), patch.object(self.module, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")), patch.object(self.module, "_probe_native_version", return_value=version, create=True):
            return stage_python_312(self.python, destination or self.root / "staged", dependency_site_packages=self.dependencies, reviewed_files=self.reviewed, reviewed_dependencies=self.reviewed_dependencies)

    def test_reviewed_native_dependency_is_an_independent_bounded_copy(self):
        import hashlib
        shared = self.root / "source-cache.dll"
        shared.hardlink_to(self.ffi)
        before = (self.ffi.read_bytes(), self.ffi.stat().st_mtime_ns, self.ffi.stat().st_ino)
        executable = self._stage_fixed()
        target = executable.parent / "DLLs/ffi-8.dll"
        self.assertTrue(target.is_file(), "reviewed ctypes dependency missing from staged DLLs")
        self.assertEqual(target.read_bytes(), self.native)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertNotEqual(target.stat().st_ino, self.ffi.stat().st_ino)
        self.assertEqual((self.ffi.read_bytes(), self.ffi.stat().st_mtime_ns, self.ffi.stat().st_ino), before)

    def test_reviewed_native_dependency_without_shared_source_link(self):
        before = self.ffi.read_bytes()
        executable = self._stage_fixed()
        target = executable.parent / "DLLs/ffi-8.dll"
        self.assertEqual(target.read_bytes(), self.native)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertNotEqual(target.stat().st_ino, self.ffi.stat().st_ino)
        self.assertEqual(self.ffi.read_bytes(), before)


    def test_missing_or_tampered_fixed_dll_is_rejected(self):
        for content in (None, b"wrong" + self.native[5:]):
            with self.subTest(missing=content is None):
                if content is None: self.ffi.unlink()
                else: self.ffi.write_bytes(content)
                with self.assertRaises(PythonToolchainError): self._stage_fixed()
                self.assertFalse((self.root / "staged").exists())

    def test_profile_patch_version_and_partial_hash_mismatch_are_rejected(self):
        with self.assertRaises(PythonToolchainError): self._stage_fixed(version="3.12.4")
        (self.python / "python312.dll").write_bytes(b"wrong version")
        with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertFalse((self.root / "staged").exists())

    def test_dll_wrong_architecture_is_rejected_even_when_fixture_hash_matches(self):
        import hashlib, struct
        bad = bytearray(self.native); struct.pack_into("<H", bad, 132, 0x14c)
        self.ffi.write_bytes(bad); self.pins = (len(bad), hashlib.sha256(bad).hexdigest())
        self.reviewed["Library/bin/ffi-8.dll"] = self.pins[1]
        with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertFalse((self.root / "staged").exists())

    def test_more_than_the_reviewed_source_link_count_is_rejected(self):
        for name in ("cache-one", "cache-two"):
            (self.root / name).hardlink_to(self.ffi)
        with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertFalse((self.root / "staged").exists())

    def test_source_junction_is_rejected_before_owned_directory_creation(self):
        import shutil
        if os.name != "nt": self.skipTest("Windows junction contract")
        outside = self.root / "external-bin"
        outside.mkdir()
        (outside / "ffi-8.dll").write_bytes(self.native)
        self.ffi.unlink()
        self.ffi.parent.rmdir()
        cmd = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
        completed = subprocess.run([str(cmd), "/d", "/c", "mklink", "/J", str(self.ffi.parent), str(outside)], capture_output=True, timeout=10, shell=False)
        if completed.returncode: self.skipTest("Junction capability unavailable: " + completed.stderr.decode(errors="replace"))
        with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertFalse((self.root / "staged").exists())
        self.assertEqual((outside / "ffi-8.dll").read_bytes(), self.native)

    def test_source_mutation_during_fd_snapshot_is_rejected(self):
        actual = os.fstat; reads = 0
        def mutate(fd):
            nonlocal reads
            info = actual(fd)
            if info.st_size == len(self.native):
                reads += 1
                if reads == 2: self.ffi.write_bytes(b"tamper" + self.native[6:])
                info = actual(fd)
            return info
        with patch("os.fstat", side_effect=mutate):
            with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertFalse((self.root / "staged").exists())

    def test_destination_collision_is_not_overwritten(self):
        (self.python / "DLLs/ffi-8.dll").write_bytes(b"existing competitor")
        with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertFalse((self.root / "staged").exists())
        self.assertEqual((self.python / "DLLs/ffi-8.dll").read_bytes(), b"existing competitor")
        self.assertEqual(self.ffi.read_bytes(), self.native)

    def test_native_copy_obeys_combined_file_byte_and_entry_caps(self):
        standard = sum((self.python / path).stat().st_size for path in self.profile)
        for setting, value in (("_MAX_BYTES", standard + len(self.native) - 1), ("_MAX_FILES", 3), ("_MAX_ENTRIES", 5)):
            with self.subTest(cap=setting), patch.object(self.module, setting, value):
                destination = self.root / setting
                with self.assertRaises(PythonToolchainError): self._stage_fixed(destination)
                self.assertFalse((destination / "DLLs/ffi-8.dll").exists())

    def test_native_destination_creation_failure_is_structured(self):
        actual = Path.open
        def fail(path, mode="r", *args, **kwargs):
            if path.name == "ffi-8.dll" and mode == "xb": raise OSError("injected exclusive create failure")
            return actual(path, mode, *args, **kwargs)
        with patch.object(Path, "open", new=fail):
            with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertFalse((self.root / "staged/DLLs/ffi-8.dll").exists())

    def test_other_python_profile_does_not_gain_native_payload(self):
        with patch.object(self.module, "_NATIVE_PROFILE", {name: (size, "0" * 64) for name, (size, _) in self.profile.items()}), patch.object(self.module, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")):
            executable = stage_python_312(self.python, self.root / "ordinary", reviewed_files=self.reviewed)
        self.assertFalse((executable.parent / "DLLs/ffi-8.dll").exists())


    def test_copied_python_profile_mutation_is_rejected(self):
        actual = Path.open
        corrupted = []
        def corrupt(path, mode="r", *args, **kwargs):
            if path == self.root / "staged/python312.dll" and mode == "rb" and not corrupted:
                corrupted.append(True)
                with actual(path, "wb") as stream:
                    stream.write(b"changed after profile snapshot")
            return actual(path, mode, *args, **kwargs)
        with patch.object(Path, "open", new=corrupt):
            with self.assertRaises(PythonToolchainError): self._stage_fixed()
        self.assertEqual(corrupted, [True])
        self.assertFalse((self.root / "staged/DLLs/ffi-8.dll").exists())


    def test_other_profile_keeps_existing_trusted_python_source_hardlinks(self):
        (self.root / "python-cache-one").hardlink_to(self.python / "python.exe")
        (self.root / "python-cache-two").hardlink_to(self.python / "python.exe")
        with patch.object(self.module, "_NATIVE_PROFILE", {name: (size, "0" * 64) for name, (size, _) in self.profile.items()}), patch.object(self.module, "_probe_python_312", return_value=("CPython", "3.12", "64", "AMD64", "win-amd64")):
            executable = stage_python_312(self.python, self.root / "ordinary", reviewed_files=self.reviewed)
        self.assertEqual(executable.stat().st_nlink, 1)
        self.assertFalse((executable.parent / "DLLs/ffi-8.dll").exists())


if __name__ == "__main__":
    unittest.main()
