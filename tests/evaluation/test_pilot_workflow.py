"""Offline assembly controls; Docker integration is reported separately."""

import tempfile
import unittest
from pathlib import Path
import os
import sys
import json
import hashlib
from dataclasses import replace
from unittest.mock import patch

from langchain_core.messages import AIMessage
from agent.architect.runtime import ArchitectRuntime
from agent.architect.models import ResearchStep, ResearchEvaluation
from agent.common.entities import ImplementationPlan, ImplementationTask, AtomicTask
from agent.developer.runtime import DeveloperRuntime
from agent.developer.editing import DeveloperEditExecutor
from agent.editing import WorkspaceEditor
from agent.runtime import RunIdentity, WorkspaceIdentity, StartRequest, AgentCodeRevision, AgentRevisionStatus, semantic_config_digest
from agent.runtime.durable import start_run
from agent.runtime.trajectory import JsonlEventSink
from tests.runtime._config_support import RunConfigTestCase

from agent.evaluation.pilot_workflow import PilotRunner, pilot_graph_factory, pilot_verification_spec
from agent.evaluation.pilot_control import build_observer
from agent.verification import VerificationSpec
from agent.verification import VerificationRunner, VerificationResult, VerificationSummary, VerificationCheckStatus, VerificationAttempt
from agent.runtime.revision import WorkspaceRevision, WorkspaceRevisionReason
from agent.runtime.durable import DurableRunResult, DurableRunStatus
from agent.verification.evaluation import AcceptanceResult, AcceptanceReason


class PilotAssemblyTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        setup = build_observer(Path(temporary.name), IMAGE)
        (setup.generation_root / "main.py").write_bytes(b"# authored source\n")
        environment = patch.dict(os.environ, {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "", "PYTHONPATH": "", "PYTHONHOME": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        spec = pilot_verification_spec(setup.observer_root)
        files = {p.name: p.read_bytes() for p in setup.observer_root.iterdir()}
        runner = PilotRunner(setup.generation_root, setup.observer_root, spec, files, setup.receipt_path, IMAGE)
        return setup, spec, runner

    def response(self, setup, *, change=None):
        source = (setup.generation_root / "main.py").read_bytes()
        receipt = {"receipt_id": "a" * 32, "source_hash": hashlib.sha256(source).hexdigest(),
                   "image": IMAGE, "cleanup_complete": True, "status": "completed"}
        if change in {"source_hash", "image"}:
            receipt[change] = "incorrect"
        if change == "cleanup":
            receipt["cleanup_complete"] = False
        if change == "execution_error":
            receipt["status"] = "execution_error"
        if change == "drift":
            (setup.generation_root / "main.py").write_bytes(b"# drifted\n")
        if change != "missing":
            setup.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return VerificationResult.create(name="sum-contract", argv=pilot_verification_spec(setup.observer_root).argv,
            cwd=".", status=VerificationCheckStatus.PASS, exit_code=0,
            stdout="PRIVATE ORACLE TRACEBACK", stderr="PRIVATE EXPECTED VALUE")

    def test_fixed_observer_and_closed_generation_inventory(self):
        setup, spec, runner = self.fixture()
        self.assertTrue(runner.permits(spec, setup.generation_root))
        (setup.generation_root / "unexpected.py").write_bytes(b"# extra\n")
        self.assertFalse(runner.permits(spec, setup.generation_root))

    def test_observer_directory_is_rejected_without_recursive_enumeration(self):
        setup, spec, runner = self.fixture()
        (setup.observer_root / "unexpected").mkdir()
        with patch.object(Path, "rglob", side_effect=AssertionError("recursive inventory is forbidden")):
            self.assertFalse(runner.permits(spec, setup.generation_root))

    def test_observer_drift_is_refused(self):
        setup, spec, runner = self.fixture()
        (setup.observer_root / "test_oracle.py").write_text("import unknown_plugin", encoding="utf-8")
        self.assertFalse(runner.permits(spec, setup.generation_root))

    def test_arbitrary_plugin_test_path_or_pytest_configuration_is_refused(self):
        setup, spec, runner = self.fixture()
        files = {p.name: p.read_bytes() for p in setup.observer_root.iterdir()}
        for extra in (("-p", "unknown_plugin"), ("/tmp/unknown_test.py",), ("-c", "unknown.ini")):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                PilotRunner(setup.generation_root, setup.observer_root, replace(spec, argv=spec.argv + extra), files, setup.receipt_path, IMAGE)
        with patch.dict(os.environ, {"PYTEST_ADDOPTS": "-p unknown_plugin"}):
            self.assertFalse(runner.permits(spec, setup.generation_root))

    def test_missing_wrong_cleanup_and_drift_receipts_are_errors(self):
        for change in ("missing", "source_hash", "image", "cleanup", "drift", "execution_error"):
            with self.subTest(change=change):
                setup, spec, runner = self.fixture()
                with patch.object(VerificationRunner, "run", lambda _, __: self.response(setup, change=change)), patch("agent.evaluation.pilot_workflow.cleanup_leases", return_value=True):
                    result = runner.run(spec)
                self.assertEqual(result.status, VerificationCheckStatus.EXECUTION_ERROR)
                self.assertNotIn("PRIVATE", result.message)

    def test_duplicate_receipts_are_refused_and_private_diagnostics_are_hidden(self):
        setup, spec, runner = self.fixture()
        with patch.object(VerificationRunner, "run", lambda _, __: self.response(setup)), patch("agent.evaluation.pilot_workflow.cleanup_leases", return_value=True):
            first = runner.run(spec)
            duplicate = runner.run(spec)
        self.assertEqual(first.status, VerificationCheckStatus.PASS)
        self.assertNotIn("PRIVATE", first.stdout + first.stderr + first.message)
        self.assertIn("pilot_receipt=", first.message)
        self.assertEqual(duplicate.status, VerificationCheckStatus.EXECUTION_ERROR)

    def test_original_attempt_binding_rejects_audit_failure_and_final_drift(self):
        setup, spec, runner = self.fixture()
        with patch.object(VerificationRunner, "run", lambda _, __: self.response(setup)), patch("agent.evaluation.pilot_workflow.cleanup_leases", return_value=True):
            checked = runner.run(spec)
        attempt = VerificationAttempt.started(run_id="pilot", task_id="task", phase="post", repair_attempt=0,
            spec_index=0, spec=spec, attempt_ordinal=0,
            workspace_revision=WorkspaceRevision.unknown(WorkspaceRevisionReason.NOT_GIT)).with_result(VerificationSummary.from_result(checked))
        result = DurableRunResult(DurableRunStatus.COMPLETED, state={"verification_attempts": (attempt,),
            "acceptance": AcceptanceResult(True, AcceptanceReason.ACCEPTED)}, run_id="pilot", record_ref="records/pilot.json")
        from agent.runtime import RunRecord, AuditStatus, write_run_record
        identity = RunIdentity("pilot", "thread", "task", WorkspaceIdentity.from_root(setup.generation_root))
        runtime = runner.runtime_root
        record = RunRecord(schema_version=1, run_id="pilot", thread_id="thread", task_id="task",
            workspace_root=identity.workspace.canonical_root, workspace_root_digest=identity.workspace.root_digest,
            run_config_digest="a" * 64, runtime_status="completed", workflow_outcome="completed",
            verification_status="verified", error_code=None, budget=None, started_at=1, finished_at=2,
            last_event_sequence=None, audit_status=AuditStatus.COMPLETE, agent_revision={})
        saved = write_run_record(runtime, record)
        self.assertTrue(saved.success)
        result = replace(result, record_ref=saved.record_ref, state={**result.state, "run_identity": identity})
        self.assertEqual(runner.bind_attempts(result, runtime_root=runtime)[0]["attempt_id"], attempt.attempt_id)
        with self.assertRaises(ValueError):
            runner.bind_attempts(replace(result, audit_incomplete=True), runtime_root=runtime)
        with self.assertRaises(ValueError):
            runner.bind_attempts(replace(result, state={**result.state, "verification_attempts": (attempt, attempt)}), runtime_root=runtime)
        with self.assertRaises(ValueError):
            runner.bind_attempts(replace(result, state={**result.state, "run_identity": replace(identity, task_id="other-task")}), runtime_root=runtime)
        with self.assertRaises(ValueError):
            runner.bind_attempts(replace(result, state={**result.state, "run_identity": replace(identity, thread_id="other-thread")}), runtime_root=runtime)
        (setup.generation_root / "main.py").write_bytes(b"# final drift\n")
        with self.assertRaises(ValueError):
            runner.bind_attempts(result, runtime_root=runtime)

    def test_generation_cannot_overlap_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = VerificationSpec("pilot", ("pytest", "--junitxml=report.xml"), report_path="report.xml")
            with self.assertRaises(ValueError):
                PilotRunner(root, root, spec, {}, root / "receipt.json", "python@sha256:" + "a" * 64)


IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
PROGRAM = "import json,sys\ndata=json.load(sys.stdin)\nprint(json.dumps({'total': %s}))\n"


@unittest.skipUnless(os.environ.get("SWE_AGENT_RUN_DOCKER_PILOT") == "1", "requires explicit Docker pilot environment")
class PilotDockerGraphTests(RunConfigTestCase):
    def test_effective_deadline_refuses_incomplete_check_and_cleans_real_descendants(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            setup = build_observer(Path(directory), IMAGE)
            (setup.generation_root / "main.py").write_bytes(
                b"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\ntime.sleep(60)\n")
            spec = pilot_verification_spec(setup.observer_root, timeout_seconds=12)
            files = {p.name: p.read_bytes() for p in setup.observer_root.iterdir()}
            runner = PilotRunner(setup.generation_root, setup.observer_root, spec, files, setup.receipt_path, IMAGE)
            result = runner.run(spec)
            self.assertEqual(result.status, VerificationCheckStatus.EXECUTION_ERROR)
            leases = json.loads(setup.lease_path.read_text(encoding="utf-8"))
            self.assertGreater(len(leases), 0, "The control must really start a candidate.")
            for lease in leases:
                absent = subprocess.run(["docker", "container", "ls", "--all", "--filter",
                    "name=^/" + lease["container_name"] + "$", "--format", "{{.ID}}"],
                    capture_output=True, timeout=5, check=False)
                self.assertEqual(absent.returncode, 0, absent.stderr)
                self.assertEqual(absent.stdout.strip(), b"")
                self.assertFalse(Path(lease["snapshot"]).exists())

    def run_control(self, repair):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation = root / "workspace"
            setup = build_observer(root / "observer", IMAGE, generation_root=generation)
            initial = "sum(data['values'])" if repair else "0"
            source = PROGRAM % initial
            (generation / "main.py").write_bytes(source.encode())
            spec = pilot_verification_spec(setup.observer_root)
            reviewed = {p.name: p.read_bytes() for p in setup.observer_root.iterdir()}
            config = self.make_config(root, verification_specs=(spec,), max_cost_usd=None, max_steps=100)
            runner = PilotRunner(generation, setup.observer_root, spec, reviewed, setup.receipt_path, IMAGE,
                                 runtime_root=config.runtime_root)
            identity = RunIdentity("pilot-run", "pilot-thread", "pilot-task", WorkspaceIdentity.from_root(generation))
            request = StartRequest(identity, semantic_config_digest(config), AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN))
            plan = ImplementationPlan(tasks=[ImplementationTask(file_path="main.py", logical_task="Implement JSON sum", atomic_tasks=[AtomicTask(atomic_task="Update total")])])
            architect = ArchitectRuntime(
                plan_next_step=lambda _: ResearchStep(reasoning="fixed authored fixture", hypothesis="Read contract"),
                check_research_step=lambda _: ResearchEvaluation(reasoning="fixed fixture", is_valid=True),
                conduct_research=lambda _: AIMessage(content="The required output is the integer sum."),
                extract_implementation_plan=lambda _: plan,
                load_codebase_structure=lambda: "main.py",
            )
            def propose(values):
                feedback = values.get("verification_feedback")
                old = "0" if feedback else initial
                new = "sum(data['values']) + 0" if feedback else ("0" if repair else "sum(data['values'])")
                return f"<<<<<<< SEARCH\n{'total'!r}: {old}\n=======\n{'total'!r}: {new}\n>>>>>>> REPLACE"
            runtime = DeveloperRuntime(
                edit_executor=lambda: DeveloperEditExecutor(WorkspaceEditor(generation)),
                load_codebase_structure=lambda: "main.py",
                research_atomic_task=lambda _: AIMessage(content="fixed proposal"),
                propose_existing_file_edit=propose, propose_new_file=lambda _: "",
            )
            sink = JsonlEventSink(config.runtime_root)
            result = start_run(config, request, {"implementation_research_scratchpad": [AIMessage(content="Implement JSON sum")]},
                               graph_factory=pilot_graph_factory(runner, identity, sink, architect, runtime), event_sink=sink)
            self.assertEqual(result.status.value, "completed", result)
            self.assertTrue(result.state["acceptance"].accepted, result.state)
            self.assertEqual(result.state["repair_attempts"], int(repair))
            self.assertTrue(result.record_ref)
            bound = runner.bind_attempts(result, runtime_root=config.runtime_root)
            for attempt in result.state["verification_attempts"]:
                for reference in (attempt.result.stdout_artifact, attempt.result.stderr_artifact):
                    if reference is not None:
                        data = (config.runtime_root / reference.path).read_bytes()
                        self.assertEqual(hashlib.sha256(data).hexdigest(), reference.sha256)
            self.assertEqual([x["phase"] for x in bound], ["baseline", "post", "post"] if repair else ["baseline", "post"])
            self.assertTrue(all(x["receipt"]["cleanup_complete"] for x in bound))
            self.assertNotEqual(generation, runner.execution_root)
            self.assertIn("sum(data['values'])", (generation / "main.py").read_text())
            # Optional evidence export belongs to this explicitly enabled,
            # authored integration harness, never the production graph.
            output = os.environ.get("SWE_AGENT_PILOT_EVIDENCE_DIR")
            if output:
                destination = Path(output)
                destination.mkdir(parents=True, exist_ok=True)
                name = "regression-repair" if repair else "baseline-editor-post"
                evidence = {
                    "kind": "FAKE_GRAPH_NOT_MODEL_RESULT",
                    "run_record": json.loads((config.runtime_root / result.record_ref).read_text(encoding="utf-8")),
                    "bound_attempts": bound,
                    "final_source_hash": hashlib.sha256((generation / "main.py").read_bytes()).hexdigest(),
                    "repair_attempts": result.state["repair_attempts"],
                    "accepted": result.state["acceptance"].accepted,
                }
                (destination / (name + ".json")).write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    def test_fake_proposal_real_sqlite_baseline_editor_post(self):
        self.run_control(False)

    def test_regression_returns_to_same_developer_and_repairs(self):
        self.run_control(True)


if __name__ == "__main__":
    unittest.main()
