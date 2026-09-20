import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent.outcome import WorkflowOutcome
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionReason,
    AgentRevisionStatus,
    DurableRunResult,
    DurableRunStatus,
    RunConfigError,
    RunConfigErrorCode,
    RunIdentity,
    PreflightResult,
    PreflightStatus,
    PreflightWarning,
    PreflightWarningCode,
    StartRequest,
    RunSummary,
    WorkspaceIdentity,
    run_exit_code,
    semantic_config_digest,
    start_run,
)
from agent.runtime.__main__ import main
from agent.verification import VerificationStatus
from agent.verification import AcceptanceReason, AcceptanceResult
from agent.workspace import WorkspaceRootError, WorkspaceRootErrorCode
from tests.runtime._config_support import RunConfigTestCase


def durable_result(
    status: DurableRunStatus,
    *,
    outcome: WorkflowOutcome | None = None,
    verification: VerificationStatus | None = None,
    error_code: str | None = None,
    run_id: str = "run-1",
    preflight: PreflightResult | None = None,
    acceptance: AcceptanceResult | None = None,
) -> DurableRunResult:
    if (
        acceptance is None
        and outcome is WorkflowOutcome.COMPLETED
        and verification is VerificationStatus.VERIFIED
    ):
        acceptance = AcceptanceResult(True, AcceptanceReason.ACCEPTED)
    return DurableRunResult(
        status=status,
        state={
            "outcome": outcome,
            "verification_status": verification,
            "acceptance": acceptance,
        },
        error_code=error_code,
        preflight=preflight,
        run_id=run_id,
    )


class RunSummaryTests(RunConfigTestCase):
    def test_library_summary_is_versioned_and_keeps_lifecycle_separate(self) -> None:
        result = durable_result(
            DurableRunStatus.COMPLETED,
            outcome=WorkflowOutcome.COMPLETED,
            verification=VerificationStatus.VERIFIED,
        )

        summary = result.summary

        self.assertIsInstance(summary, RunSummary)
        self.assertEqual(
            summary.to_dict(),
            {
                "schema_version": 2,
                "runtime_status": "completed",
                "workflow_outcome": "completed",
                "verification_status": "verified",
                "error_code": None,
                "run_id": "run-1",
                "record_ref": None,
                "warnings": [],
                "acceptance": {"accepted": True, "reason": "accepted"},
                "audit_incomplete": False,
            },
        )
        self.assertEqual(run_exit_code(summary), 0)

    def test_summary_exposes_preflight_warning_codes_only(self) -> None:
        result = durable_result(
            DurableRunStatus.COMPLETED,
            outcome=WorkflowOutcome.COMPLETED,
            verification=VerificationStatus.VERIFIED,
            preflight=PreflightResult(
                status=PreflightStatus.ACCEPTED,
                warnings=(
                    PreflightWarning(
                        code=PreflightWarningCode.AGENT_REVISION_UNVERIFIED,
                        message="secret-bearing diagnostic text must not escape",
                    ),
                ),
            ),
        )

        summary = result.summary

        self.assertEqual(summary.warnings, ("agent_revision_unverified",))
        self.assertEqual(
            summary.to_dict()["warnings"], ["agent_revision_unverified"]
        )
        self.assertNotIn("secret-bearing", json.dumps(summary.to_dict()))

    def test_exit_code_mapping_is_conservative(self) -> None:
        cases = (
            (
                "failed",
                durable_result(
                    DurableRunStatus.FAILED,
                    outcome=WorkflowOutcome.FAILED,
                ),
                1,
            ),
            (
                "unverified",
                durable_result(
                    DurableRunStatus.COMPLETED,
                    outcome=WorkflowOutcome.UNVERIFIED,
                    verification=VerificationStatus.UNVERIFIED,
                ),
                1,
            ),
            (
                "no_changes",
                durable_result(
                    DurableRunStatus.COMPLETED,
                    outcome=WorkflowOutcome.NO_CHANGES,
                    verification=VerificationStatus.UNVERIFIED,
                ),
                1,
            ),
            (
                "pre_existing_failure",
                durable_result(
                    DurableRunStatus.COMPLETED,
                    outcome=WorkflowOutcome.COMPLETED,
                    verification=VerificationStatus.PRE_EXISTING_FAILURE,
                ),
                1,
            ),
            (
                "improved",
                durable_result(
                    DurableRunStatus.COMPLETED,
                    outcome=WorkflowOutcome.COMPLETED,
                    verification=VerificationStatus.IMPROVED,
                ),
                1,
            ),
            (
                "paused",
                durable_result(DurableRunStatus.PAUSED),
                3,
            ),
            (
                "unknown_call",
                durable_result(
                    DurableRunStatus.REJECTED,
                    error_code="outcome_unknown",
                ),
                2,
            ),
            (
                "preflight",
                durable_result(
                    DurableRunStatus.REJECTED,
                    error_code="workspace_mismatch",
                ),
                2,
            ),
            (
                "sqlite",
                durable_result(
                    DurableRunStatus.FAILED,
                    error_code="sqlite_error",
                ),
                2,
            ),
            (
                "unknown_runtime",
                durable_result(DurableRunStatus.FAILED),
                2,
            ),
        )
        for name, result, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(run_exit_code(result.summary), expected)

    def test_result_accepted_keeps_lifecycle_separate_from_business_acceptance(
        self,
    ) -> None:
        completed_unverified = durable_result(
            DurableRunStatus.COMPLETED,
            outcome=WorkflowOutcome.UNVERIFIED,
            verification=VerificationStatus.UNVERIFIED,
        )
        paused = durable_result(DurableRunStatus.PAUSED)

        self.assertTrue(completed_unverified.accepted)
        self.assertTrue(paused.accepted)
        self.assertEqual(run_exit_code(completed_unverified.summary), 1)
        self.assertEqual(run_exit_code(paused.summary), 3)
        self.assertFalse(
            durable_result(DurableRunStatus.REJECTED).accepted
        )
        self.assertFalse(durable_result(DurableRunStatus.FAILED).accepted)

    def test_trusted_allowed_failure_acceptance_is_exit_zero(self) -> None:
        result = durable_result(
            DurableRunStatus.COMPLETED,
            outcome=WorkflowOutcome.COMPLETED,
            verification=VerificationStatus.PRE_EXISTING_FAILURE,
            acceptance=AcceptanceResult(True, AcceptanceReason.ACCEPTED),
        )

        summary = result.summary

        self.assertEqual(summary.acceptance.reason, AcceptanceReason.ACCEPTED)
        self.assertEqual(run_exit_code(summary), 0)

    def test_malformed_checkpoint_acceptance_cannot_enable_exit_zero(self) -> None:
        result = DurableRunResult(
            DurableRunStatus.COMPLETED,
            state={
                "outcome": WorkflowOutcome.COMPLETED,
                "verification_status": VerificationStatus.VERIFIED,
                "acceptance": {"accepted": "true", "reason": "accepted"},
            },
        )

        self.assertIsNone(result.summary.acceptance)
        self.assertEqual(run_exit_code(result.summary), 1)

    def test_invalid_config_is_structured_by_cli(self) -> None:
        with patch(
            "agent.runtime.__main__.load_run_config",
            side_effect=RunConfigError(
                RunConfigErrorCode.INVALID_ROOT,
                "workspace root is invalid",
            ),
        ):
            code, payload = self.invoke_cli("start")

        self.assertEqual(code, 2)
        self.assertEqual(payload["runtime_status"], "rejected")
        self.assertEqual(payload["error_code"], "invalid_root")
        self.assertEqual(payload["run_id"], "run-1")

    def test_cli_emits_json_summary_and_exit_code_for_real_entry_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), verification_specs=())
            revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
            cases = (
                (
                    "verified",
                    durable_result(
                        DurableRunStatus.COMPLETED,
                        outcome=WorkflowOutcome.COMPLETED,
                        verification=VerificationStatus.VERIFIED,
                    ),
                    0,
                ),
                (
                    "accepted_pre_existing_failure",
                    durable_result(
                        DurableRunStatus.COMPLETED,
                        outcome=WorkflowOutcome.COMPLETED,
                        verification=VerificationStatus.PRE_EXISTING_FAILURE,
                        acceptance=AcceptanceResult(
                            True, AcceptanceReason.ACCEPTED
                        ),
                    ),
                    0,
                ),
                (
                    "failed",
                    durable_result(
                        DurableRunStatus.FAILED,
                        outcome=WorkflowOutcome.FAILED,
                    ),
                    1,
                ),
                (
                    "unverified",
                    durable_result(
                        DurableRunStatus.COMPLETED,
                        outcome=WorkflowOutcome.UNVERIFIED,
                        verification=VerificationStatus.UNVERIFIED,
                    ),
                    1,
                ),
                (
                    "no_changes",
                    durable_result(
                        DurableRunStatus.COMPLETED,
                        outcome=WorkflowOutcome.NO_CHANGES,
                        verification=VerificationStatus.UNVERIFIED,
                    ),
                    1,
                ),
                (
                    "paused",
                    durable_result(DurableRunStatus.PAUSED),
                    3,
                ),
                (
                    "unknown",
                    durable_result(
                        DurableRunStatus.REJECTED,
                        error_code="outcome_unknown",
                    ),
                    2,
                ),
            )
            for name, result, expected_code in cases:
                with self.subTest(name=name):
                    with (
                        patch(
                            "agent.runtime.__main__.load_run_config",
                            return_value=config,
                        ),
                        patch(
                            "agent.runtime.__main__.semantic_config_digest",
                            return_value="b" * 64,
                        ),
                        patch(
                            "agent.runtime.__main__.detect_agent_code_revision",
                            return_value=revision,
                        ),
                        patch(
                            "agent.runtime.__main__.start_run",
                            return_value=result,
                        ),
                    ):
                        output = io.StringIO()
                        with redirect_stdout(output):
                            code = main(
                                [
                                    "start",
                                    "--run-id",
                                    "run-1",
                                    "--thread-id",
                                    "thread-1",
                                    "--task-id",
                                    "task-1",
                                    "--task",
                                    "inspect",
                                ]
                            )
                    self.assertEqual(code, expected_code)
                    payload = json.loads(output.getvalue())
                    self.assertEqual(payload, result.summary.to_dict())

    def test_cli_turns_sqlite_error_into_json_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), verification_specs=())
            revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
            with (
                patch(
                    "agent.runtime.__main__.load_run_config",
                    return_value=config,
                ),
                patch(
                    "agent.runtime.__main__.semantic_config_digest",
                    return_value="b" * 64,
                ),
                patch(
                    "agent.runtime.__main__.detect_agent_code_revision",
                    return_value=revision,
                ),
                patch(
                    "agent.runtime.__main__.start_run",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(
                        [
                            "start",
                            "--run-id",
                            "run-1",
                            "--thread-id",
                            "thread-1",
                            "--task-id",
                            "task-1",
                            "--task",
                            "inspect",
                        ]
                    )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["error_code"], "sqlite_error")
            self.assertEqual(payload["runtime_status"], "failed")

    def test_cli_exposes_unknown_revision_warning_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), verification_specs=())
            result = durable_result(
                DurableRunStatus.COMPLETED,
                outcome=WorkflowOutcome.COMPLETED,
                verification=VerificationStatus.VERIFIED,
                preflight=PreflightResult(
                    status=PreflightStatus.ACCEPTED,
                    warnings=(
                        PreflightWarning(
                            code=PreflightWarningCode.AGENT_REVISION_UNVERIFIED,
                            message="do not expose this message",
                        ),
                    ),
                ),
            )
            with (
                patch(
                    "agent.runtime.__main__.load_run_config",
                    return_value=config,
                ),
                patch(
                    "agent.runtime.__main__.semantic_config_digest",
                    return_value="b" * 64,
                ),
                patch(
                    "agent.runtime.__main__.detect_agent_code_revision",
                    return_value=AgentCodeRevision(
                        None,
                        AgentRevisionStatus.UNKNOWN,
                        AgentRevisionReason.DIRTY,
                    ),
                ),
                patch(
                    "agent.runtime.__main__.start_run",
                    return_value=result,
                ),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(
                        [
                            "start",
                            "--run-id",
                            "run-1",
                            "--thread-id",
                            "thread-1",
                            "--task-id",
                            "task-1",
                            "--task",
                            "inspect",
                        ]
                    )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["warnings"], ["agent_revision_unverified"])
            self.assertNotIn("do not expose", output.getvalue())

    def test_cli_workspace_exception_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), verification_specs=())
            with (
                patch(
                    "agent.runtime.__main__.load_run_config",
                    return_value=config,
                ),
                patch(
                    "agent.runtime.__main__.WorkspaceIdentity.from_root",
                    side_effect=WorkspaceRootError(
                        WorkspaceRootErrorCode.NOT_FOUND,
                        "workspace is gone",
                    ),
                ),
            ):
                code, payload = self.invoke_cli("start")

            self.assertEqual(code, 2)
            self.assertEqual(payload["runtime_status"], "rejected")
            self.assertEqual(payload["error_code"], "workspace_error")

    def test_library_sqlite_and_workspace_errors_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=())
            identity = RunIdentity(
                run_id="run-1",
                thread_id="thread-1",
                task_id="task-1",
                workspace=WorkspaceIdentity.from_root(config.workspace_root),
            )
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN),
            )
            with patch(
                "agent.runtime.durable.sqlite3.connect",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                sqlite_result = start_run(config, request, {"value": 0})

            self.assertEqual(sqlite_result.status, DurableRunStatus.FAILED)
            self.assertEqual(sqlite_result.error_code, "sqlite_error")

            config.workspace_root.rmdir()
            workspace_result = start_run(config, request, {"value": 0})
            self.assertEqual(
                workspace_result.status, DurableRunStatus.REJECTED
            )
            self.assertEqual(workspace_result.error_code, "workspace_error")

    def test_library_runtime_root_error_is_structured_and_system_exit_propagates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, verification_specs=())
            identity = RunIdentity(
                run_id="run-1",
                thread_id="thread-1",
                task_id="task-1",
                workspace=WorkspaceIdentity.from_root(config.workspace_root),
            )
            request = StartRequest(
                identity,
                semantic_config_digest(config),
                AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN),
            )
            with patch(
                "pathlib.Path.mkdir",
                side_effect=OSError("runtime root is not writable"),
            ):
                runtime_result = start_run(config, request, {"value": 0})

            self.assertEqual(runtime_result.status, DurableRunStatus.FAILED)
            self.assertEqual(runtime_result.error_code, "runtime_root_error")

            def exits(*_args):
                raise SystemExit("stop")

            with self.assertRaises(SystemExit):
                start_run(
                    config,
                    request,
                    {"value": 0},
                    graph_factory=exits,
                )

            def fails(*_args):
                raise RuntimeError("unexpected graph programming failure")

            with self.assertRaises(RuntimeError):
                start_run(
                    config,
                    request,
                    {"value": 0},
                    graph_factory=fails,
                )

    def test_cli_does_not_swallow_unexpected_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), verification_specs=())
            with (
                patch(
                    "agent.runtime.__main__.load_run_config",
                    return_value=config,
                ),
                patch(
                    "agent.runtime.__main__.semantic_config_digest",
                    return_value="b" * 64,
                ),
                patch(
                    "agent.runtime.__main__.detect_agent_code_revision",
                    return_value=AgentCodeRevision(
                        "a" * 40, AgentRevisionStatus.KNOWN
                    ),
                ),
                patch(
                    "agent.runtime.__main__.start_run",
                    side_effect=RuntimeError("unexpected graph programming failure"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    main(
                        [
                            "start",
                            "--run-id",
                            "run-1",
                            "--thread-id",
                            "thread-1",
                            "--task-id",
                            "task-1",
                            "--task",
                            "inspect",
                        ]
                    )

    def test_subprocess_cli_invalid_config_emits_json_and_exit_two(self) -> None:
        environment = os.environ.copy()
        environment["AGENT_MODEL_PROVIDER"] = "invalid-provider"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent.runtime",
                "start",
                "--run-id",
                "run-1",
                "--thread-id",
                "thread-1",
                "--task-id",
                "task-1",
                "--task",
                "inspect",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["runtime_status"], "rejected")
        self.assertEqual(payload["error_code"], "invalid_value")

    def invoke_cli(self, command: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    command,
                    "--run-id",
                    "run-1",
                    "--thread-id",
                    "thread-1",
                    "--task-id",
                    "task-1",
                ]
                + (["--task", "inspect"] if command == "start" else [])
            )
        return code, json.loads(output.getvalue())


if __name__ == "__main__":
    unittest.main()
