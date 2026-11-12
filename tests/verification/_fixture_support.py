"""Admission for authored temporary fixtures only; never used by production.

Initial bytes come from the test's literal setup. Future versions must be
enumerated by that test, before the graph runs. No post-edit snapshot is taken.
"""

from pathlib import Path
import tempfile

from agent.graph import create_workflow_graph
from agent.verification.runner import VerificationRunner
from agent.verification.workflow import ReviewedVerificationInputs, VerificationController


class FixtureRunner:
    """Bind an audited deterministic test double to its owned fixture root."""

    def __init__(self, root, runner):
        self.execution_root = Path(root).resolve()
        self._runner = runner

    def run(self, spec):
        return self._runner.run(spec)


def reviewed_fixture(root, runner, specs, *, versions=None):
    root = Path(root).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or (root / ".git").exists():
        raise ValueError("A repository is not an authored temporary fixture")
    files = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in root.rglob("*") if p.is_file()}
    for path, future in (versions or {}).items():
        initial = (files[path],) if path in files else (None,)
        files[path] = initial + tuple(future)
    return ReviewedVerificationInputs(
        workspace_root=root, runner=runner, specs=specs, file_versions=files,
    )


def fixture_graph(*, fixture_root, fixture_versions=None, **kwargs):
    runner = kwargs.pop("verification_runner", None)
    if runner is None:
        runner = VerificationRunner(fixture_root)
    elif not isinstance(runner, VerificationRunner):
        runner = FixtureRunner(fixture_root, runner)
    specs = kwargs.get("verification_specs", ())
    return create_workflow_graph(
        **kwargs, workspace_root=fixture_root, verification_runner=runner,
        reviewed_inputs=reviewed_fixture(
            fixture_root, runner, specs, versions=fixture_versions,
        ),
    )


def fixture_controller(specs, runner, root, **kwargs):
    runner = FixtureRunner(root, runner)
    return VerificationController(
        specs, runner, root, **kwargs,
        reviewed_inputs=reviewed_fixture(root, runner, specs),
    )
