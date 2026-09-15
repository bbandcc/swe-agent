import hashlib
import json
import os
import math
import subprocess
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionReason,
    AgentRevisionStatus,
    PreflightErrorCode,
    PreflightStatus,
    PreflightWarningCode,
    ResumeRequest,
    RunCheckpoint,
    RunIdentity,
    StartRequest,
    WorkspaceIdentity,
    detect_agent_code_revision,
    preflight_resume,
    preflight_start,
)


class FakeCheckpointLookup:
    def __init__(self, checkpoint: RunCheckpoint | None) -> None:
        self.checkpoint = checkpoint
        self.requested_thread_ids: list[str] = []

    def get(self, thread_id: str) -> RunCheckpoint | None:
        self.requested_thread_ids.append(thread_id)
        return self.checkpoint


def known_revision(character: str) -> AgentCodeRevision:
    return AgentCodeRevision(
        commit_sha=character * 40,
        status=AgentRevisionStatus.KNOWN,
    )


class IdentityTests(unittest.TestCase):
    def test_workspace_identity_is_stable_path_identity_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file.txt").write_text("before", encoding="utf-8")
            before = WorkspaceIdentity.from_root(root)

            (root / "file.txt").write_text("after", encoding="utf-8")
            after = WorkspaceIdentity.from_root(root)

            self.assertEqual(before, after)
            self.assertEqual(before.canonical_root, str(root.resolve()))
            self.assertEqual(len(before.root_digest), 64)

    def test_rejects_workspace_identity_for_symbolic_link_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "target"
            target.mkdir()
            link = parent / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            with self.assertRaises(ValueError):
                WorkspaceIdentity.from_root(link)

            comparison_key = os.path.normcase(str(link.absolute()))
            digest = hashlib.sha256(
                comparison_key.encode("utf-8")
            ).hexdigest()
            with self.assertRaises(ValueError):
                WorkspaceIdentity(str(link.absolute()), digest)

    def test_rejects_identity_for_missing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            comparison_key = os.path.normcase(str(missing.absolute()))
            digest = hashlib.sha256(
                comparison_key.encode("utf-8")
            ).hexdigest()

            with self.assertRaises(ValueError):
                WorkspaceIdentity(str(missing.absolute()), digest)

    def test_rejects_forged_workspace_path_digest_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                WorkspaceIdentity(
                    canonical_root=str(Path(directory).resolve()),
                    root_digest="a" * 64,
                )

    @unittest.skipUnless(os.name == "nt", "Windows path comparison only")
    def test_workspace_identity_equality_uses_windows_normcase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            canonical = str(Path(directory).resolve())
            alternate_case = canonical.swapcase()
            expected_digest = hashlib.sha256(
                os.path.normcase(canonical).encode("utf-8")
            ).hexdigest()

            self.assertEqual(
                WorkspaceIdentity(canonical, expected_digest),
                WorkspaceIdentity(alternate_case, expected_digest),
            )

    def test_detects_clean_and_dirty_git_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "--quiet")
            (root / "agent.py").write_text("value = 1\n", encoding="utf-8")
            self._git(root, "add", "agent.py")
            self._git(
                root,
                "-c",
                "user.name=S3 Test",
                "-c",
                "user.email=s3@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "initial",
            )
            expected_sha = self._git(root, "rev-parse", "HEAD").stdout.strip()

            clean = detect_agent_code_revision(root)
            (root / "agent.py").write_text("value = 2\n", encoding="utf-8")
            dirty = detect_agent_code_revision(root)

            self.assertEqual(clean.status, AgentRevisionStatus.KNOWN)
            self.assertEqual(clean.commit_sha, expected_sha)
            self.assertIsNone(clean.reason)
            self.assertEqual(dirty.status, AgentRevisionStatus.UNKNOWN)
            self.assertIsNone(dirty.commit_sha)
            self.assertEqual(dirty.reason, AgentRevisionReason.DIRTY)

    def test_reports_unknown_revision_without_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = detect_agent_code_revision(Path(directory))

            self.assertEqual(revision.status, AgentRevisionStatus.UNKNOWN)
            self.assertIsNone(revision.commit_sha)
            self.assertEqual(revision.reason, AgentRevisionReason.NOT_GIT)

    def test_reports_unknown_revision_when_git_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = detect_agent_code_revision(
                Path(directory), git_executable="missing-s3-git-executable"
            )

            self.assertEqual(revision.status, AgentRevisionStatus.UNKNOWN)
            self.assertEqual(
                revision.reason, AgentRevisionReason.GIT_UNAVAILABLE
            )

    def test_unknown_revision_rejects_unstructured_reason(self) -> None:
        with self.assertRaises(ValueError):
            AgentCodeRevision(
                commit_sha=None,
                status=AgentRevisionStatus.UNKNOWN,
                reason="arbitrary",
            )

    def test_revision_timeout_must_be_finite_and_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for timeout in (0, -1, math.nan, math.inf):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(ValueError):
                        detect_agent_code_revision(
                            Path(directory), timeout_seconds=timeout
                        )

    def test_start_accepts_new_thread_and_rejects_existing_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request, checkpoint = self._request_and_checkpoint(Path(directory))
            missing = FakeCheckpointLookup(None)

            accepted = preflight_start(request, missing)
            rejected = preflight_start(
                request, FakeCheckpointLookup(checkpoint)
            )

            self.assertEqual(accepted.status, PreflightStatus.ACCEPTED)
            self.assertIsNone(accepted.error_code)
            self.assertEqual(missing.requested_thread_ids, ["thread-1"])
            self.assertEqual(rejected.status, PreflightStatus.REJECTED)
            self.assertEqual(
                rejected.error_code,
                PreflightErrorCode.THREAD_ALREADY_EXISTS,
            )

    def test_resume_rejects_missing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request, _ = self._request_and_checkpoint(Path(directory))

            result = preflight_resume(
                ResumeRequest(
                    identity=request.identity,
                    run_config_digest=request.run_config_digest,
                    agent_revision=request.agent_revision,
                ),
                FakeCheckpointLookup(None),
            )

            self.assertEqual(result.status, PreflightStatus.REJECTED)
            self.assertEqual(
                result.error_code,
                PreflightErrorCode.CHECKPOINT_NOT_FOUND,
            )

    def test_resume_accepts_matching_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request, checkpoint = self._request_and_checkpoint(Path(directory))

            result = preflight_resume(
                ResumeRequest(
                    identity=request.identity,
                    run_config_digest=request.run_config_digest,
                    agent_revision=request.agent_revision,
                ),
                FakeCheckpointLookup(checkpoint),
            )

            self.assertEqual(result.status, PreflightStatus.ACCEPTED)
            self.assertEqual(result.warnings, ())

    def test_resume_rejects_every_identity_binding_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request, checkpoint = self._request_and_checkpoint(root)
            other_workspace = root / "other-workspace"
            other_workspace.mkdir()
            other_identity = WorkspaceIdentity.from_root(other_workspace)
            cases = (
                (
                    replace(
                        checkpoint,
                        identity=replace(
                            checkpoint.identity, workspace=other_identity
                        ),
                    ),
                    PreflightErrorCode.WORKSPACE_MISMATCH,
                ),
                (
                    replace(
                        checkpoint,
                        identity=replace(
                            checkpoint.identity, run_id="other-run"
                        ),
                    ),
                    PreflightErrorCode.THREAD_IDENTITY_MISMATCH,
                ),
                (
                    replace(
                        checkpoint,
                        identity=replace(
                            checkpoint.identity, task_id="other-task"
                        ),
                    ),
                    PreflightErrorCode.THREAD_IDENTITY_MISMATCH,
                ),
                (
                    replace(
                        checkpoint,
                        identity=replace(
                            checkpoint.identity, thread_id="other-thread"
                        ),
                    ),
                    PreflightErrorCode.THREAD_IDENTITY_MISMATCH,
                ),
                (
                    replace(checkpoint, run_config_digest="b" * 64),
                    PreflightErrorCode.RUN_CONFIG_MISMATCH,
                ),
                (
                    replace(
                        checkpoint,
                        agent_revision=known_revision("b"),
                    ),
                    PreflightErrorCode.AGENT_REVISION_MISMATCH,
                ),
            )
            resume_request = ResumeRequest(
                identity=request.identity,
                run_config_digest=request.run_config_digest,
                agent_revision=request.agent_revision,
            )

            for stored, error_code in cases:
                with self.subTest(error_code=error_code):
                    result = preflight_resume(
                        resume_request,
                        FakeCheckpointLookup(stored),
                    )
                    self.assertEqual(result.status, PreflightStatus.REJECTED)
                    self.assertEqual(result.error_code, error_code)

    def test_unknown_revision_only_adds_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request, checkpoint = self._request_and_checkpoint(Path(directory))
            unknown = AgentCodeRevision(
                commit_sha=None,
                status=AgentRevisionStatus.UNKNOWN,
                reason=AgentRevisionReason.DIRTY,
            )
            request = ResumeRequest(
                identity=request.identity,
                run_config_digest=request.run_config_digest,
                agent_revision=unknown,
            )

            result = preflight_resume(
                request,
                FakeCheckpointLookup(checkpoint),
            )

            self.assertEqual(result.status, PreflightStatus.ACCEPTED)
            self.assertEqual(len(result.warnings), 1)
            self.assertEqual(
                result.warnings[0].code,
                PreflightWarningCode.AGENT_REVISION_UNVERIFIED,
            )

    def test_requests_are_secret_free_serializable_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request, _ = self._request_and_checkpoint(Path(directory))

            serialized = json.dumps(asdict(request), default=str)

            self.assertNotIn("api-key", serialized)
            self.assertIn("run_config_digest", serialized)

    @staticmethod
    def _request_and_checkpoint(
        root: Path,
    ) -> tuple[StartRequest, RunCheckpoint]:
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        identity = RunIdentity(
            run_id="run-1",
            thread_id="thread-1",
            task_id="task-1",
            workspace=WorkspaceIdentity.from_root(workspace),
        )
        request = StartRequest(
            identity=identity,
            run_config_digest="a" * 64,
            agent_revision=known_revision("a"),
        )
        return request, RunCheckpoint(
            identity=identity,
            run_config_digest=request.run_config_digest,
            agent_revision=request.agent_revision,
        )

    @staticmethod
    def _git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", "-C", str(root), *argv],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return completed


if __name__ == "__main__":
    unittest.main()
