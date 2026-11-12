"""Fresh-process public imports only; no model or verification execution."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PublicImportTests(unittest.TestCase):
    def run_child(self, source):
        result = subprocess.run(
            [sys.executable, "-B", "-c", source], cwd=ROOT,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", timeout=30, check=False, shell=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runner_import_without_graph_or_runtime_preload(self):
        self.run_child(
            "import sys\n"
            "assert 'agent.graph' not in sys.modules and 'agent.runtime' not in sys.modules\n"
            "from agent.verification.runner import VerificationRunner\n"
            "from agent.verification import VerificationRunner as PublicRunner\n"
            "assert VerificationRunner is PublicRunner\n"
        )

    def test_runconfig_import_without_graph_or_verification_preload(self):
        self.run_child(
            "import sys\n"
            "assert 'agent.graph' not in sys.modules and 'agent.verification' not in sys.modules\n"
            "from agent.runtime import RunConfig\n"
            "from agent.runtime.config import RunConfig as Config\n"
            "assert RunConfig is Config\n"
        )

    def test_import_orders_preserve_public_types_and_attempt_validation(self):
        for first in (
            "agent.verification.runner", "agent.runtime.config",
            "agent.verification.contracts", "agent.graph",
        ):
            with self.subTest(first=first):
                self.run_child(
                    f"import importlib; importlib.import_module({first!r})\n"
                    "import agent.runtime as runtime\n"
                    "import agent.runtime.config as config\n"
                    "import agent.runtime.revision as revision\n"
                    "import agent.verification as verification\n"
                    "import agent.verification.contracts as contracts\n"
                    "assert runtime.RunConfig is config.RunConfig\n"
                    "for name in ('VerificationSpec', 'RepairScopePolicy', 'VerificationRecoveryPolicy'):\n"
                    "    assert getattr(config, name) is getattr(contracts, name) is getattr(verification, name)\n"
                    "assert contracts.WorkspaceRevision is revision.WorkspaceRevision is runtime.WorkspaceRevision\n"
                    "spec = verification.VerificationSpec('fixed', ('pytest', '--junitxml=report.xml'))\n"
                    "assert contracts.verification_spec_digest(spec) == verification.verification_spec_digest(spec)\n"
                    "workspace = revision.WorkspaceRevision(None, None, None, None, revision.WorkspaceRevisionStatus.UNKNOWN, revision.WorkspaceRevisionReason.NOT_GIT)\n"
                    "attempt = contracts.VerificationAttempt.started(run_id='run', task_id='task', phase='baseline', repair_attempt=0, spec_index=0, spec=spec, attempt_ordinal=0, workspace_revision=workspace)\n"
                    "assert attempt.workspace_revision is workspace\n"
                    "assert attempt.status is verification.VerificationAttemptStatus.STARTED\n"
                    "try:\n"
                    "    contracts.VerificationAttempt.started(run_id='run', task_id='task', phase='baseline', repair_attempt=0, spec_index=0, spec=spec, attempt_ordinal=0, workspace_revision=object())\n"
                    "except ValueError as error:\n"
                    "    assert 'workspace revision' in str(error)\n"
                    "else:\n"
                    "    raise AssertionError('Invalid revision was accepted')\n"
                )
