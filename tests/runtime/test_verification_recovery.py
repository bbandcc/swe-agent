"""Durable verification attempt/recovery tests through public runtime seams."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent.developer.state import DeveloperStatus
from agent.graph import create_workflow_graph
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    ResumeRequest,
    RunConfig,
    RunIdentity,
    StartRequest,
    WorkspaceIdentity,
    semantic_config_digest,
)
from agent.runtime.durable import DurableRunStatus, resume_run, start_run
from agent.verification import (
    REPORT_SCHEMA,
    VerificationCase,
    VerificationCaseStatus,
    VerificationCheckStatus,
    VerificationRecoveryPolicy,
    VerificationReport,
    VerificationResult,
    VerificationSpec,
    verification_spec_digest,
)
from tests.runtime._config_support import RunConfigTestCase


def _passing_result(spec: VerificationSpec) -> VerificationResult:
    return VerificationResult.create(
        name=spec.name,
        argv=spec.argv,
        cwd=spec.cwd,
        status=VerificationCheckStatus.PASS,
        exit_code=0,
        report=VerificationReport(
            check_id=spec.name,
            report_schema=REPORT_SCHEMA,
            cases=(
                VerificationCase(
                    check_id=spec.name,
                    case_id="target",
                    status=VerificationCaseStatus.PASS,
                ),
            ),
        ),
    )


class MarkerRunner:
    """A deterministic injected runner that leaves a subprocess marker."""

    def __init__(self, root: Path, *, crash: bool = False) -> None:
        self.root = root
        self.crash = crash
        self.calls: list[str] = []

    def run(self, spec: VerificationSpec) -> VerificationResult:
        self.calls.append(spec.name)
        marker = self.root / f"{spec.name}.count"
        command = (
            "from pathlib import Path; "
            f"p=Path({str(marker)!r}); "
            "n=int(p.read_text() or '0') if p.exists() else 0; "
            "p.write_text(str(n+1), encoding='utf-8')"
        )
        subprocess.run([sys.executable, "-c", command], check=True)
        if self.crash:
            os._exit(97)
        return _passing_result(spec)


def _graph_factory(
    *,
    runner: MarkerRunner,
    interrupt_after: tuple[str, ...] = (),
):
    def factory(config, _run_id, saver, clock):
        builder = create_workflow_graph(
            architect=lambda _state: {},
            developer=lambda _state: {
                "developer_status": DeveloperStatus.COMPLETED,
            },
            verification_specs=config.verification_specs,
            verification_runner=runner,
            workspace_root=config.workspace_root,
            durable_runtime=True,
            clock=clock,
        )
        return builder.compile(
            checkpointer=saver,
            interrupt_after=list(interrupt_after) or None,
        )

    return factory


_CRASH_WORKER = r'''
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from agent.config import ModelSettings
from agent.developer.state import DeveloperStatus
from agent.graph import create_workflow_graph
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    ResumeRequest,
    RunConfig,
    RunIdentity,
    StartRequest,
    TokenPricing,
    WorkspaceIdentity,
    semantic_config_digest,
)
from agent.runtime.durable import resume_run, start_run
from agent.verification import VerificationCheckStatus, VerificationResult, VerificationSpec

root = Path(os.environ["S3_VERIFICATION_ROOT"])
workspace = root / "workspace"
runtime = root / "runtime"
workspace.mkdir(parents=True, exist_ok=True)
runtime.mkdir(parents=True, exist_ok=True)
marker = root / "marker.txt"
rerun = root / "rerun.txt"
spec = VerificationSpec("unit", ("pytest", "--junitxml=report.xml"), timeout_seconds=30)
config = RunConfig(
    workspace_root=workspace,
    runtime_root=runtime,
    model=ModelSettings(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key=None,
    ),
    model_max_output_tokens=64,
    verification_specs=(spec,),
    timeout_seconds=300,
    max_steps=20,
    pricing=TokenPricing(
        input_cost_per_million_tokens=Decimal("0.25"),
        output_cost_per_million_tokens=Decimal("0.50"),
        source="test",
    ),
)
identity = RunIdentity(
    run_id="verification-recovery-run",
    thread_id="verification-recovery-thread",
    task_id="verification-recovery-task",
    workspace=WorkspaceIdentity.from_root(workspace),
)
revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
request = StartRequest(identity, semantic_config_digest(config), revision)

class Runner:
    def run(self, configured):
        if os.environ.get("S3_VERIFICATION_MODE") == "resume":
            rerun.write_text("called", encoding="utf-8")
        script = (
            "from pathlib import Path; "
            f"p=Path({str(marker)!r}); "
            "n=int(p.read_text() or '0') if p.exists() else 0; "
            "p.write_text(str(n+1), encoding='utf-8')"
        )
        subprocess.run([sys.executable, "-c", script], check=True)
        if os.environ.get("S3_VERIFICATION_MODE") == "start":
            os._exit(97)
        return VerificationResult.create(
            name=configured.name,
            argv=configured.argv,
            cwd=configured.cwd,
            status=VerificationCheckStatus.PASS,
            exit_code=0,
        )

def factory(current_config, _run_id, saver, clock):
    builder = create_workflow_graph(
        architect=lambda _state: {},
        developer=lambda _state: {"developer_status": DeveloperStatus.COMPLETED},
        verification_specs=current_config.verification_specs,
        verification_runner=Runner(),
        workspace_root=current_config.workspace_root,
        durable_runtime=True,
        clock=clock,
    )
    return builder.compile(checkpointer=saver)

if os.environ.get("S3_VERIFICATION_MODE") == "start":
    result = start_run(config, request, {}, graph_factory=factory, clock=lambda: 100.0)
else:
    result = resume_run(
        config,
        ResumeRequest(identity, request.run_config_digest, revision),
        graph_factory=factory,
        clock=lambda: 150.0,
    )
print(json.dumps({"status": result.status.value, "error": result.error_code}))
'''


class VerificationRecoveryTests(RunConfigTestCase):
    def _request(self, config: RunConfig) -> StartRequest:
        identity = RunIdentity(
            run_id="verification-run",
            thread_id="verification-thread",
            task_id="verification-task",
            workspace=WorkspaceIdentity.from_root(config.workspace_root),
        )
        revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
        return StartRequest(identity, semantic_config_digest(config), revision)

    def _run_worker(self, root: Path, mode: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "S3_VERIFICATION_ROOT": str(root),
                "S3_VERIFICATION_MODE": mode,
                "PYTHONPATH": str(Path.cwd()),
            }
        )
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(_CRASH_WORKER)],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_started_side_effect_is_unknown_and_not_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = self._run_worker(root, "start")
            self.assertEqual(started.returncode, 97)
            self.assertEqual((root / "marker.txt").read_text(), "1")

            resumed = self._run_worker(root, "resume")

            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error"], "outcome_unknown")
            self.assertEqual((root / "marker.txt").read_text(), "1")
            self.assertFalse((root / "rerun.txt").exists())

    def test_result_recorded_is_reused_without_running_check_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(
                root,
                verification_specs=(
                    VerificationSpec(
                        "unit", ("pytest", "--junitxml=report.xml")
                    ),
                ),
                max_cost_usd=None,
            )
            request = self._request(config)
            first_runner = MarkerRunner(root)
            factory = _graph_factory(
                runner=first_runner,
                interrupt_after=("run_baseline_verification_check_0",),
            )
            started = start_run(
                config,
                request,
                {},
                graph_factory=factory,
                clock=lambda: 100.0,
            )
            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(first_runner.calls, ["unit"])
            self.assertEqual((root / "unit.count").read_text(), "1")

            second_runner = MarkerRunner(root)
            resumed = resume_run(
                config,
                ResumeRequest(
                    request.identity,
                    request.run_config_digest,
                    request.agent_revision,
                ),
                graph_factory=_graph_factory(
                    runner=second_runner,
                    interrupt_after=("finish_baseline_verification",),
                ),
                clock=lambda: 150.0,
            )

            self.assertEqual(resumed.status, DurableRunStatus.PAUSED)
            self.assertEqual(second_runner.calls, [])
            self.assertEqual((root / "unit.count").read_text(), "1")
            attempts = resumed.state["verification_attempts"]
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].status.value, "result_recorded")

    def test_multiple_specs_are_independent_and_completed_attempts_are_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = tuple(
                VerificationSpec(name, ("pytest", "--junitxml=report.xml"))
                for name in ("unit", "lint")
            )
            config = self.make_config(
                root,
                verification_specs=specs,
                max_cost_usd=None,
            )
            request = self._request(config)
            first_runner = MarkerRunner(root)
            started = start_run(
                config,
                request,
                {},
                graph_factory=_graph_factory(
                    runner=first_runner,
                    interrupt_after=("run_baseline_verification_check_1",),
                ),
                clock=lambda: 100.0,
            )
            self.assertEqual(started.status, DurableRunStatus.PAUSED)
            self.assertEqual(first_runner.calls, ["unit", "lint"])

            second_runner = MarkerRunner(root)
            resumed = resume_run(
                config,
                ResumeRequest(
                    request.identity,
                    request.run_config_digest,
                    request.agent_revision,
                ),
                graph_factory=_graph_factory(runner=second_runner),
                clock=lambda: 150.0,
            )

            self.assertEqual(resumed.status, DurableRunStatus.COMPLETED)
            self.assertEqual(second_runner.calls, ["unit", "lint"])
            self.assertEqual((root / "unit.count").read_text(), "2")
            self.assertEqual((root / "lint.count").read_text(), "2")
            attempts = resumed.state["verification_attempts"]
            self.assertEqual(
                [(item.phase, item.spec_index) for item in attempts],
                [("baseline", 0), ("baseline", 1), ("post", 0), ("post", 1)],
            )
            self.assertEqual(
                {item.status.value for item in attempts}, {"result_recorded"}
            )

    def test_rerun_policy_is_explicitly_unsupported_without_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.make_config(
                    Path(directory),
                    verification_recovery_policy=VerificationRecoveryPolicy.RERUN_ISOLATED,
                )

    def test_attempt_spec_digest_uses_canonical_numeric_encoding(self):
        integer_timeout = VerificationSpec(
            "unit", ("pytest", "--junitxml=report.xml"), timeout_seconds=30
        )
        float_timeout = VerificationSpec(
            "unit", ("pytest", "--junitxml=report.xml"), timeout_seconds=30.0
        )
        self.assertEqual(
            verification_spec_digest(integer_timeout),
            verification_spec_digest(float_timeout),
        )


if __name__ == "__main__":
    unittest.main()
