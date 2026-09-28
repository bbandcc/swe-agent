from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "ci.yml"
)


class ContinuousIntegrationContractTests(unittest.TestCase):
    def test_readme_and_contributing_share_the_supported_development_gates(self) -> None:
        root = Path(__file__).resolve().parents[1]
        docs = {
            name: (root / name).read_text(encoding="utf-8")
            for name in ("README.md", "CONTRIBUTING.md")
        }
        expected = (
            "Python 3.12 (the locked development and CI baseline)",
            "uv sync --locked",
            "uv run --locked python -m unittest discover -s tests -v",
            "uv run --locked python -m pytest",
            "uv run --locked python -m compileall -q agent tests",
        )
        for name, content in docs.items():
            for fragment in expected:
                with self.subTest(document=name, fragment=fragment):
                    self.assertIn(fragment, content)

    def test_ci_workflow_keeps_locked_cross_platform_project_gates(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), "cross-platform CI workflow is missing")
        workflow = WORKFLOW.read_text(encoding="utf-8")

        required_fragments = (
            "permissions:\n  contents: read",
            "os: [ubuntu-latest, windows-latest]",
            "fetch-depth: 0",
            "persist-credentials: false",
            'python-version: "3.12"',
            'version: "0.12.1"',
            "uv sync --locked",
            "uv run --locked python -m unittest discover -s tests -v",
            "uv run --locked python -m pytest",
            "uv run --locked python -m compileall -q agent tests",
            "uv run --locked python -m unittest -v tests.test_s5d1_installation",
            'PYTEST_ADDOPTS: "-rs"',
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)

        checkout_step = workflow.split("      - name: Check out source", 1)[1].split(
            "      - name:", 1
        )[0]
        self.assertIn("fetch-depth: 0", checkout_step)
        self.assertIn("persist-credentials: false", checkout_step)

        self.assertRegex(
            workflow,
            r"uses:\s*astral-sh/setup-uv@[0-9a-f]{40}",
        )
        self.assertIn("tests.runtime.test_identity", workflow)
        self.assertIn("tests.runtime.test_admission", workflow)
        self.assertIn("tests.runtime.test_access_policy", workflow)
        self.assertIn("tests.editing.test_workspace_editor", workflow)
        self.assertIn("tests.tools.test_workspace_boundary", workflow)
        self.assertNotIn("permissions: write-all", workflow)
        self.assertNotIn("secrets.", workflow)


if __name__ == "__main__":
    unittest.main()
