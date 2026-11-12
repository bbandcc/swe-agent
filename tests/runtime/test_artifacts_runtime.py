import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from agent.common.entities import ImplementationPlan, PlanStatus
from agent.config import ModelSettings
from agent.developer.state import DeveloperStatus
from agent.graph import create_workflow_graph
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionReason,
    AgentRevisionStatus,
    KnownSecretFilter,
    ModelRetryPolicy,
    RunConfig,
    RunIdentity,
    StartRequest,
    TokenPricing,
    WorkspaceIdentity,
    semantic_config_digest,
)
from agent.runtime.trajectory import EventRecorder, JsonlEventSink
from agent.runtime.durable import DurableRunStatus, start_run
from agent.verification import VerificationRunner, VerificationSpec, VerificationSummary
from agent.verification.workflow import ReviewedVerificationInputs


class VerificationArtifactRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}))

    def test_sqlite_state_and_artifacts_keep_full_logs_out_of_state(self) -> None:
        canary = "S3_ARTIFACT_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime = root / "runtime"
            workspace.mkdir()
            (workspace / "test_canary.py").write_text(
                "def test_canary():\n"
                f"    print('{canary}')\n"
                "    assert True\n",
                encoding="utf-8",
                newline="",
            )
            config = RunConfig(
                workspace_root=workspace,
                runtime_root=runtime,
                model=ModelSettings(
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    base_url="https://api.deepseek.com",
                    api_key=SecretStr(canary),
                ),
                model_max_output_tokens=64,
                verification_specs=(
                    VerificationSpec(
                        name="pytest",
                        argv=(
                            sys.executable,
                            "-m",
                            "pytest",
                            "-p", "no:cacheprovider", "--assert=plain",
                            "-q",
                            "-s",
                            "--junitxml=report.xml",
                        ),
                        report_path="report.xml",
                    ),
                ),
                timeout_seconds=60.0,
                max_steps=8,
                max_cost_usd=None,
                pricing=TokenPricing(Decimal("0.1"), Decimal("0.2"), "test"),
                model_retry_policy=ModelRetryPolicy(max_attempts=1),
            )
            identity = RunIdentity(
                run_id="artifact-run",
                thread_id="artifact-thread",
                task_id="artifact-task",
                workspace=WorkspaceIdentity.from_root(workspace),
            )
            revision = AgentCodeRevision(
                None,
                AgentRevisionStatus.UNKNOWN,
                AgentRevisionReason.NOT_GIT,
            )
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                revision,
            )

            def factory(current, _run_id, saver, clock):
                def architect(_state):
                    return {
                        "implementation_plan": ImplementationPlan(
                            status=PlanStatus.NO_CHANGES,
                            tasks=[],
                        )
                    }

                def developer(_state):
                    return {"developer_status": DeveloperStatus.COMPLETED}

                secret_filter = KnownSecretFilter.from_run_config(current)
                recorder = EventRecorder(
                    JsonlEventSink(current.runtime_root, secret_filter=secret_filter),
                    run_id=_run_id,
                    thread_id=identity.thread_id,
                    task_id=identity.task_id,
                    secret_filter=secret_filter,
                    clock=clock,
                )
                runner = VerificationRunner(
                    current.workspace_root,
                    runtime_root=current.runtime_root,
                    secret_filter=secret_filter,
                )
                # These are the authored bytes above, fixed before dispatch;
                # never derive trust by scanning an unknown workspace.
                reviewed = ReviewedVerificationInputs(
                    workspace_root=current.workspace_root,
                    runner=runner,
                    specs=current.verification_specs,
                    file_versions={
                        "test_canary.py": (
                            "def test_canary():\n"
                            f"    print('{canary}')\n"
                            "    assert True\n"
                        ).encode("utf-8"),
                    },
                )
                self.assertEqual(runner.execution_root, current.workspace_root)
                for spec in current.verification_specs:
                    self.assertTrue(reviewed.permits(spec, runner, current.workspace_root))
                return create_workflow_graph(
                    architect=architect,
                    developer=developer,
                    verification_specs=current.verification_specs,
                    verification_runner=runner,
                    reviewed_inputs=reviewed,
                    workspace_root=current.workspace_root,
                    runtime_root=current.runtime_root,
                    durable_runtime=True,
                    clock=clock,
                    secret_filter=secret_filter,
                    event_recorder=recorder,
                ).compile(checkpointer=saver)

            result = start_run(
                config,
                request,
                {"implementation_research_scratchpad": []},
                graph_factory=factory,
                clock=lambda: 100.0,
            )

            self.assertEqual(result.status, DurableRunStatus.COMPLETED)
            self.assertIsNotNone(result.state)
            assert result.state is not None
            for key in ("baseline_verification", "post_verification"):
                checks = result.state[key]
                self.assertIsInstance(checks[0], VerificationSummary)
                self.assertNotIn(canary, repr(checks))
                self.assertNotIn("stdout=", repr(checks[0]))
                self.assertNotIn("stderr=", repr(checks[0]))
                self.assertIsNotNone(checks[0].stdout_artifact)
                self.assertNotIn(canary, checks[0].stdout)
            self.assertFalse((workspace / "report.xml").exists())
            events = [
                json.loads(line)
                for line in (runtime / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            verification_events = [
                event for event in events if event.get("event_type") == "verification"
            ]
            self.assertTrue(verification_events)
            self.assertTrue(
                any(event.get("artifact_refs") for event in verification_events)
            )
            self.assertTrue(
                any(
                    check.get("stdout_digest")
                    for event in verification_events
                    for check in event.get("summary", {}).get("checks", ())
                )
            )

            for path in runtime.rglob("*"):
                if path.is_file():
                    self.assertNotIn(canary.encode("utf-8"), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
