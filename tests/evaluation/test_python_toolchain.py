from __future__ import annotations

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
        (package / "__init__.py").write_text("VALUE = 312\n", encoding="utf-8")

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
                validate_python_312(self.python, expected_architecture="AMD64")

    def test_dependency_tree_is_copied_under_staged_python_and_bounded_contract(self) -> None:
        destination = self.root / "staged"
        with patch("agent.evaluation.python_toolchain.subprocess.run", side_effect=self._probe_as_cpython_312):
            executable = stage_python_312(
                self.python,
                destination,
                dependency_site_packages=self.dependencies,
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
                )
        self.assertEqual(outside_file.read_bytes(), b"outside dependency bytes")


if __name__ == "__main__":
    unittest.main()
