import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools.codemap import (
    get_code_definitions,
    get_function_implementation,
    get_raw_file_content,
)
from agent.tools.search import search_keyword_in_directory


class WorkspaceReadBoundaryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
