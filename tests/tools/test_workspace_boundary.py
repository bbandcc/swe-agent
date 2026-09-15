import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.developer.editing import DeveloperEditExecutor
from agent.developer.runtime import default_developer_runtime
from agent.editing import EditStatus
from agent.tools.codemap import (
    get_code_definitions,
    get_function_implementation,
    get_raw_file_content,
)
from agent.tools.search import search_keyword_in_directory
from agent.workspace import WorkspacePathResolver


class WorkspaceReadBoundaryTests(unittest.TestCase):
    def test_resolver_accepts_workspace_below_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            real_parent = parent / "real-parent"
            workspace = real_parent / "workspace"
            workspace.mkdir(parents=True)
            target = workspace / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            link = parent / "linked-parent"
            try:
                link.symlink_to(real_parent, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            result = WorkspacePathResolver(link / "workspace").resolve_file(
                "app.py"
            )

            self.assertTrue(result.ok, result)
            self.assertEqual(result.path, target.resolve())

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows path type")
    def test_resolver_accepts_workspace_below_junction_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            real_parent = parent / "real-parent"
            workspace = real_parent / "workspace"
            workspace.mkdir(parents=True)
            target = workspace / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            junction = parent / "junction-parent"
            completed = subprocess.run(
                [
                    "pwsh.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-CommandWithArgs",
                    (
                        "New-Item -ItemType Junction -Path $args[0] "
                        "-Target $args[1] | Out-Null"
                    ),
                    str(junction),
                    str(real_parent),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(
                    f"junction creation unavailable: {completed.stderr}"
                )

            result = WorkspacePathResolver(
                junction / "workspace"
            ).resolve_file("app.py")

            self.assertTrue(result.ok, result)
            self.assertEqual(result.path, target.resolve())

    def test_default_reader_and_editor_share_configured_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            configured = parent / "configured"
            configured.mkdir()
            target = configured / "app.py"
            target.write_text("value = 1\n", encoding="utf-8", newline="")
            fallback = parent / "workspace_repo"
            fallback.mkdir()
            decoy = fallback / "app.py"
            decoy.write_text("value = 999\n", encoding="utf-8", newline="")
            previous_cwd = Path.cwd()
            try:
                os.chdir(parent)
                with patch.dict(
                    os.environ,
                    {"SWE_AGENT_WORKSPACE": str(configured)},
                    clear=False,
                ):
                    read_result = get_raw_file_content.invoke(
                        {"file_path": "./workspace_repo/app.py"}
                    )
                    executor: DeveloperEditExecutor = (
                        default_developer_runtime().edit_executor()
                    )
                    snapshot = executor.prepare("workspace_repo/app.py")
                    edit_result = executor.apply(
                        snapshot,
                        (
                            "<<<<<<< SEARCH\nvalue = 1\n=======\n"
                            "value = 2\n>>>>>>> REPLACE"
                        ),
                        task_id="workspace-config.step-1",
                    )
            finally:
                os.chdir(previous_cwd)

            self.assertTrue(read_result["ok"], read_result)
            self.assertEqual(read_result["content"], "value = 1\n")
            self.assertEqual(edit_result.status, EditStatus.APPLIED)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(decoy.read_text(encoding="utf-8"), "value = 999\n")

    def test_model_controlled_read_tools_read_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "app.py"
            source.write_text(
                "def answer():\n    return 42\n",
                encoding="utf-8",
                newline="",
            )
            with patch.dict(
                os.environ, {"SWE_AGENT_WORKSPACE": str(root)}, clear=False
            ):
                results = (
                    search_keyword_in_directory.invoke(
                        {"directory": ".", "search_term": "answer"}
                    ),
                    get_code_definitions.invoke({"file_path": "app.py"}),
                    get_function_implementation.invoke(
                        {"file_path": "app.py", "function_name": "answer"}
                    ),
                    get_raw_file_content.invoke({"file_path": "app.py"}),
                )

            for result in results:
                self.assertTrue(result["ok"], result)
                self.assertIn("answer", result["content"])

    def test_model_controlled_read_tools_reject_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "workspace"
            root.mkdir()
            outside = parent / "secret.py"
            outside.write_text(
                "def secret():\n    return 'token'\n",
                encoding="utf-8",
                newline="",
            )
            calls = (
                (
                    search_keyword_in_directory,
                    {"directory": str(parent), "search_term": "token"},
                ),
                (get_code_definitions, {"file_path": str(outside)}),
                (
                    get_function_implementation,
                    {"file_path": str(outside), "function_name": "secret"},
                ),
                (get_raw_file_content, {"file_path": str(outside)}),
            )

            with patch.dict(
                os.environ, {"SWE_AGENT_WORKSPACE": str(root)}, clear=False
            ):
                for workspace_tool, arguments in calls:
                    with self.subTest(tool=workspace_tool.name):
                        result = workspace_tool.invoke(arguments)
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["error_code"], "path_invalid")
                        self.assertNotIn("token", str(result))

    def test_search_does_not_follow_nested_junction_or_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "workspace"
            outside = parent / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.py").write_text(
                "TOKEN = 'private'\n", encoding="utf-8", newline=""
            )
            linked = root / "linked"
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "pwsh.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-CommandWithArgs",
                        (
                            "New-Item -ItemType Junction -Path $args[0] "
                            "-Target $args[1] | Out-Null"
                        ),
                        str(linked),
                        str(outside),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    self.skipTest(
                        f"junction creation unavailable: {completed.stderr}"
                    )
            else:
                try:
                    linked.symlink_to(outside, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"directory symlinks are unavailable: {error}")

            with patch.dict(
                os.environ, {"SWE_AGENT_WORKSPACE": str(root)}, clear=False
            ):
                result = search_keyword_in_directory.invoke(
                    {"directory": ".", "search_term": "TOKEN"}
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "No matches found.")

    def test_search_reports_files_skipped_for_decode_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.py").write_text(
                "TOKEN = 'visible'\n", encoding="utf-8", newline=""
            )
            (root / "invalid.py").write_bytes(b"TOKEN = '\xff'\n")

            with patch.dict(
                os.environ, {"SWE_AGENT_WORKSPACE": str(root)}, clear=False
            ):
                result = search_keyword_in_directory.invoke(
                    {"directory": ".", "search_term": "TOKEN"}
                )

            self.assertTrue(result["ok"], result)
            self.assertIn("valid.py", result["content"])
            self.assertEqual(result["skipped_files"], ["invalid.py"])
            self.assertEqual(len(result["warnings"]), 1)
            self.assertIn("invalid.py", result["warnings"][0])

    def test_search_reports_files_skipped_for_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unreadable.py").write_text(
                "TOKEN = 'hidden'\n", encoding="utf-8", newline=""
            )

            with (
                patch.dict(
                    os.environ,
                    {"SWE_AGENT_WORKSPACE": str(root)},
                    clear=False,
                ),
                patch.object(Path, "open", side_effect=OSError("read denied")),
            ):
                result = search_keyword_in_directory.invoke(
                    {"directory": ".", "search_term": "TOKEN"}
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["content"], "No matches found.")
            self.assertEqual(result["skipped_files"], ["unreadable.py"])
            self.assertIn("OSError", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
