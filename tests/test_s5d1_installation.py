from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATHS = (
    "agent/prompts/act_step_prompt.md",
    "agent/prompts/think_step_prompt.md",
    "agent/architect/prompts/check_research_already_explored.md",
    "agent/architect/prompts/conduct_research_plan_prompt.md",
    "agent/architect/prompts/extract_implementation_plan.md",
    "agent/architect/prompts/plan_next_step_prompt.md",
    "agent/developer/prompts/create_diff_prompt.md",
    "agent/developer/prompts/developing_prompt.md",
    "agent/developer/prompts/get_clear_implementation_plan.md",
    "agent/developer/prompts/implement_diff.md",
    "agent/developer/prompts/implement_new_file.md",
)


class PromptLoadingTests(unittest.TestCase):
    def test_all_prompts_render_from_non_repository_cwd_with_utf8_reads(self):
        script = r'''
import builtins
import io
import json
import os
from unittest.mock import patch

from helpers.prompts import markdown_to_prompt_template

prompts = json.loads(os.environ["S5D1_PROMPT_PATHS"])
original_builtin_open = builtins.open
original_io_open = io.open

def require_utf8(file, mode="r", *args, **kwargs):
    try:
        path = os.fspath(file)
    except TypeError:
        path = ""
    if path.lower().endswith(".md") and "r" in mode:
        if kwargs.get("encoding") != "utf-8":
            raise AssertionError(f"prompt read lacks explicit UTF-8: {path}")
    return original_builtin_open(file, mode, *args, **kwargs)

def require_utf8_io(file, mode="r", *args, **kwargs):
    try:
        path = os.fspath(file)
    except TypeError:
        path = ""
    if path.lower().endswith(".md") and "r" in mode:
        if kwargs.get("encoding") != "utf-8":
            raise AssertionError(f"prompt resource read lacks explicit UTF-8: {path}")
    return original_io_open(file, mode, *args, **kwargs)

rendered = []
with patch("builtins.open", require_utf8), patch("io.open", require_utf8_io):
    for path in prompts:
        template = markdown_to_prompt_template(path)
        values = {name: [] for name in template.input_variables}
        if hasattr(template, "format_messages"):
            messages = template.format_messages(**values)
            assert messages, path
            rendered.extend(str(message.content) for message in messages)
        else:
            rendered.append(template.format(**values))

assert len(prompts) == 11
assert any("\u2014" in text for text in rendered)
print(json.dumps({"rendered": len(prompts), "cwd": os.getcwd()}))
'''
        with tempfile.TemporaryDirectory(prefix="s5d1-nonrepo-cwd-") as cwd:
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            env["S5D1_PROMPT_PATHS"] = json.dumps(PROMPT_PATHS)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertEqual(json.loads(result.stdout)["rendered"], 11)


class CleanWheelInstallationTests(unittest.TestCase):
    def test_clean_wheel_contains_and_loads_all_production_prompt_resources(self):
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "S5d.1 wheel validation requires the project uv tool")

        with tempfile.TemporaryDirectory(prefix="s5d1-wheel-install-") as temporary:
            root = Path(temporary)
            cache = (
                os.environ.get("S5D1_UV_CACHE_DIR")
                or os.environ.get("UV_CACHE_DIR")
                or str(root / "uv-cache")
            )
            build_env = os.environ.copy()
            build_env["UV_CACHE_DIR"] = cache
            build_env["VIRTUAL_ENV"] = sys.prefix
            source = root / "source"
            source.mkdir()
            for name in ("pyproject.toml", "README.md", ".python-version"):
                shutil.copy2(REPOSITORY_ROOT / name, source / name)
            for name in ("agent", "helpers"):
                shutil.copytree(
                    REPOSITORY_ROOT / name,
                    source / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            wheel_dir = root / "wheel"
            wheel_dir.mkdir()
            build = subprocess.run(
                [
                    uv,
                    "build",
                    "--wheel",
                    "--no-build-isolation",
                    "--out-dir",
                    str(wheel_dir),
                ],
                cwd=source,
                env=build_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(
                build.returncode,
                0,
                msg=f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}",
            )
            wheels = list(wheel_dir.glob("*.whl"))
            self.assertEqual(len(wheels), 1, build.stdout + build.stderr)
            wheel = wheels[0]

            with zipfile.ZipFile(wheel) as archive:
                members = set(archive.namelist())
            for package_file in (
                "agent/__init__.py",
                "agent/architect/graph.py",
                "agent/developer/graph.py",
                "agent/tools/write.py",
                "helpers/prompts.py",
            ):
                self.assertIn(package_file, members)
            for prompt_path in PROMPT_PATHS:
                self.assertIn(prompt_path, members)

            wheel_environment = root / "installed-environment"
            install_env = os.environ.copy()
            install_env["UV_PROJECT_ENVIRONMENT"] = str(wheel_environment)
            install_env["UV_CACHE_DIR"] = cache
            install_env["UV_LINK_MODE"] = "copy"
            install_env.pop("VIRTUAL_ENV", None)
            sync = subprocess.run(
                [
                    uv,
                    "sync",
                    "--locked",
                    "--no-install-project",
                    "--no-dev",
                    "--link-mode",
                    "copy",
                    "--python",
                    "3.12",
                ],
                cwd=REPOSITORY_ROOT,
                env=install_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(
                sync.returncode,
                0,
                msg=f"stdout:\n{sync.stdout}\nstderr:\n{sync.stderr}",
            )
            python = wheel_environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            self.assertTrue(python.is_file(), f"missing clean Python environment: {python}")
            install = subprocess.run(
                [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
                cwd=root,
                env=install_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(
                install.returncode,
                0,
                msg=f"stdout:\n{install.stdout}\nstderr:\n{install.stderr}",
            )

            run_cwd = root / "outside-repository"
            run_cwd.mkdir()
            smoke = r'''
import importlib.metadata
import json
import os
import sys
from pathlib import Path

import agent
import helpers.prompts
from helpers.prompts import markdown_to_prompt_template

expected = json.loads(os.environ["S5D1_PROMPT_PATHS"])
repo = Path(os.environ["S5D1_REPOSITORY_ROOT"]).resolve()
prefix = Path(sys.prefix).resolve()
assert sys.version_info[:2] == (3, 12), sys.version
for module in (agent, helpers.prompts):
    origin = Path(module.__file__).resolve()
    assert origin.is_relative_to(prefix), (module.__name__, origin, prefix)
    assert not origin.is_relative_to(repo), (module.__name__, origin, repo)
assert "PYTHONPATH" not in os.environ
for entry in sys.path:
    resolved = Path(entry or os.getcwd()).resolve()
    assert not resolved.is_relative_to(repo), (entry, repo)

distribution = importlib.metadata.distribution("swe-agent-langgraph")
files = {str(item).replace("\\", "/") for item in distribution.files or ()}
assert all(path in files for path in expected), [path for path in expected if path not in files]
direct_url_text = distribution.read_text("direct_url.json")
if direct_url_text:
    direct_url = json.loads(direct_url_text)
    assert not direct_url.get("dir_info", {}).get("editable", False), direct_url

rendered = 0
for path in expected:
    template = markdown_to_prompt_template(path)
    values = {name: [] for name in template.input_variables}
    if hasattr(template, "format_messages"):
        assert template.format_messages(**values), path
    else:
        assert template.format(**values) is not None
    rendered += 1
assert rendered == 11
print(json.dumps({"rendered": rendered, "prefix": str(prefix), "cwd": os.getcwd()}))
'''
            smoke_env = install_env.copy()
            smoke_env.pop("PYTHONPATH", None)
            smoke_env.pop("PYTHONHOME", None)
            smoke_env.pop("VIRTUAL_ENV", None)
            smoke_env["PYTHONNOUSERSITE"] = "1"
            smoke_env["S5D1_PROMPT_PATHS"] = json.dumps(PROMPT_PATHS)
            smoke_env["S5D1_REPOSITORY_ROOT"] = str(REPOSITORY_ROOT)
            result = subprocess.run(
                [str(python), "-c", smoke],
                cwd=run_cwd,
                env=smoke_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertEqual(json.loads(result.stdout)["rendered"], 11)

            cli = subprocess.run(
                [str(python), "-m", "agent.runtime", "--help"],
                cwd=run_cwd,
                env=smoke_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(
                cli.returncode,
                0,
                msg=f"stdout:\n{cli.stdout}\nstderr:\n{cli.stderr}",
            )
            self.assertIn("usage:", cli.stdout.lower())


if __name__ == "__main__":
    unittest.main()
