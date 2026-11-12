"""Fixed pilot task selection and task-bound observer admission."""

from pathlib import Path
import json
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from agent.evaluation.model_pilot import prepare_trial, load_trial
from agent.evaluation.pilot_control import build_observer, oracle_source_for
from agent.evaluation.pilot_tasks import get_pilot_task, PILOT_TASK_IDS
from agent.evaluation.pilot_workflow import PilotRunner, pilot_verification_spec
from agent.evaluation.model_pilot import IMAGE


CONTROL_ENV = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "", "PYTHONPATH": "", "PYTHONHOME": ""}


class PilotTaskTests(unittest.TestCase):
    def test_recomputed_digest_cannot_change_fixed_scope_image_retry_or_limits(self):
        mutations = ({"image": "python:latest"}, {"allowed_edit_scope": ["other.py"]},
                     {"automatic_retry": True}, {"automatic_resume": True},
                     {"timeout_seconds": float("inf")}, {"max_steps": 101},
                     {"frozen_inputs": {}})
        with tempfile.TemporaryDirectory() as directory:
            for index, mutation in enumerate(mutations):
                root = Path(directory) / str(index)
                plan = prepare_trial(root)
                plan.update(mutation)
                plan.pop("plan_digest")
                plan["plan_digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
                with self.subTest(fields=list(mutation)), self.assertRaises(ValueError):
                    load_trial(root)

    def test_recomputed_plan_digest_cannot_preinstall_the_gold_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            plan = prepare_trial(root, task_id="json-sum-squares")
            gold = get_pilot_task("json-sum-squares").gold
            (root / "G" / "main.py").write_bytes(gold)
            plan["frozen_inputs"]["G/main.py"] = hashlib.sha256(gold).hexdigest()
            plan.pop("plan_digest")
            plan["plan_digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_trial(root)

    def test_three_tasks_freeze_distinct_task_and_oracle_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            fingerprints = []
            for task_id in ("json-sum", "json-sum-squares", "json-weighted-sum"):
                root = Path(directory) / task_id
                plan = prepare_trial(root, task_id=task_id)
                self.assertEqual(plan["task_id"], task_id)
                self.assertEqual(load_trial(root)[1]["task_id"], task_id)
                self.assertEqual(plan["allowed_edit_scope"], ["main.py"])
                self.assertEqual(plan["model_policy"]["max_requests"], 8)
                fingerprints.append(plan["task_fingerprint"])
            self.assertEqual(len(set(fingerprints)), 3)

    def test_unknown_task_is_rejected_before_creating_any_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            for task_id in ("../../oracle", "other", None, ["json-sum"]):
                root = Path(directory) / "trial"
                with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                    prepare_trial(root, task_id=task_id)
                self.assertFalse(root.exists())

    def test_runner_binds_exact_task_oracle_and_detects_later_task_drift(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CONTROL_ENV):
            setup = build_observer(Path(directory), IMAGE, task_id="json-sum-squares")
            (setup.generation_root / "main.py").write_bytes(get_pilot_task("json-sum-squares").original)
            spec = pilot_verification_spec(setup.observer_root)
            files = {p.name: p.read_bytes() for p in setup.observer_root.iterdir()}
            with self.assertRaises(ValueError):
                PilotRunner(setup.generation_root, setup.observer_root, spec, files,
                            setup.receipt_path, IMAGE, task_id="json-weighted-sum")
            runner = PilotRunner(setup.generation_root, setup.observer_root, spec, files,
                                 setup.receipt_path, IMAGE, task_id="json-sum-squares")
            self.assertTrue(runner.permits(spec, setup.generation_root))
            runner.task_id = "json-weighted-sum"
            self.assertFalse(runner.permits(spec, setup.generation_root))
            self.assertNotEqual(oracle_source_for("json-sum-squares"), oracle_source_for("json-weighted-sum"))


@unittest.skipUnless(os.environ.get("SWE_AGENT_RUN_DOCKER_PILOT") == "1", "requires explicit Docker pilot environment")
class PilotTaskDockerControls(unittest.TestCase):
    def test_all_three_tasks_reject_original_and_wrong_and_accept_gold(self):
        evidence = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CONTROL_ENV):
            for task_id in PILOT_TASK_IDS:
                task = get_pilot_task(task_id)
                for label, source in (("original", task.original), ("gold", task.gold),
                                      ("wrong", b'print("{\\"total\\":999}")\n')):
                    with self.subTest(task_id=task_id, control=label):
                        setup = build_observer(Path(directory) / task_id / label, IMAGE, task_id=task_id)
                        (setup.generation_root / "main.py").write_bytes(source)
                        spec = pilot_verification_spec(setup.observer_root)
                        files = {p.name: p.read_bytes() for p in setup.observer_root.iterdir()}
                        runner = PilotRunner(setup.generation_root, setup.observer_root, spec, files,
                                             setup.receipt_path, IMAGE, task_id=task_id)
                        result = runner.run(spec)
                        self.assertEqual(result.status.value, "pass" if label == "gold" else "fail", result)
                        receipt = runner.receipts[0]
                        self.assertEqual(len(receipt["executions"]), 3)
                        self.assertTrue(receipt["cleanup_complete"])
                        evidence.append({"task_id": task_id, "control": label, "status": result.status.value,
                                         "source_hash": receipt["source_hash"], "image": receipt["image"],
                                         "cleanup_complete": receipt["cleanup_complete"],
                                         "executions": len(receipt["executions"]),
                                         "task_fingerprint": task.fingerprint})
        output = os.environ.get("SWE_AGENT_TASK_CONTROL_EVIDENCE")
        if output:
            Path(output).write_text(json.dumps({"kind": "OFFLINE_TASK_DOCKER_CONTROLS",
                                               "paid_requests": 0, "controls": evidence}, indent=2), encoding="utf-8")

