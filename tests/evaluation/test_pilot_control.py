"""Only trusted, fixed inputs; real Docker is explicitly opt-in via the script."""

import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent.evaluation.pilot_control import build_observer, cleanup_leases, decode_response, run_candidate

IMAGE = "python@sha256:" + "1" * 64


def docker_receipt(argv, **kwargs):
    if argv[1] == "inspect":
        content = [{"HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True,
            "PidsLimit": 16, "Memory": 67_108_864, "MemorySwap": 67_108_864,
            "NanoCpus": 500_000_000, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]},
            "Config": {"User": "65534:65534"}, "Image": "sha256:test"}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(content).encode(), b"")
    return subprocess.CompletedProcess(argv, 0, argv[-1].encode() if argv[1] == "rm" else b"", b"")


class FinishedDocker:
    def __init__(self, output=b'{"total":6}', running=False):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO()
        self.returncode = 0
        self.running = running

    def poll(self):
        return None if self.running else self.returncode

    def kill(self):
        self.running = False

    def wait(self, timeout):
        return self.returncode


class PilotControlTests(unittest.TestCase):
    def test_response_requires_exact_json_integer_result(self):
        self.assertEqual(decode_response(b'{"total":6}\n'), 6)
        for data in (b'PASS', b'', b'{"total":true}', b'{"total":6,"pass":true}',
                     b'{"total":6}\n{"total":7}', b'{"total":1,"total":6}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_response(data)

    def test_invalid_inputs_are_rejected_before_transport(self):
        for image, values, timeout in (("python:latest", [], 20), (IMAGE, [True], 20),
                                        (IMAGE, [], float("inf")), (IMAGE, [], 0)):
            with self.subTest(image=image, values=values, timeout=timeout):
                with self.assertRaises(ValueError):
                    run_candidate(b"", image, values, timeout_seconds=timeout)

    def test_completed_transport_returns_data_and_removes_snapshot(self):
        def remove(argv, **kwargs):
            return docker_receipt(argv, **kwargs)

        with patch("subprocess.Popen", return_value=FinishedDocker()), patch("subprocess.run", side_effect=remove):
            result = run_candidate(b"print('data')", IMAGE, [1, 2, 3])
        self.assertEqual(result.status, "completed")
        self.assertEqual(decode_response(result.stdout), 6)
        self.assertTrue(result.cleanup_complete)
        self.assertFalse(Path(result.snapshot).exists())

    def test_failed_cleanup_overrides_success_and_retains_owned_snapshot(self):
        with patch("subprocess.Popen", return_value=FinishedDocker()), patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"daemon unavailable")
        ):
            result = run_candidate(b"pass", IMAGE, [])
        try:
            self.assertEqual(result.status, "cleanup_error")
            self.assertFalse(result.cleanup_complete)
            self.assertTrue(Path(result.snapshot).is_dir())
        finally:
            shutil.rmtree(result.snapshot)

    def test_timeout_is_not_completed_even_after_successful_cleanup(self):
        def remove(argv, **kwargs):
            return docker_receipt(argv, **kwargs)

        with patch("subprocess.Popen", return_value=FinishedDocker(running=True)), patch("subprocess.run", side_effect=remove):
            result = run_candidate(b"while True: pass", IMAGE, [], timeout_seconds=2)
        self.assertEqual(result.status, "timeout")
        self.assertTrue(result.cleanup_complete)

    def test_excess_output_is_bounded_and_rejected(self):
        def remove(argv, **kwargs):
            return docker_receipt(argv, **kwargs)

        with patch("subprocess.Popen", return_value=FinishedDocker(b"x" * 40_000)), patch("subprocess.run", side_effect=remove):
            result = run_candidate(b"pass", IMAGE, [])
        self.assertEqual(result.status, "output_limit")
        self.assertLessEqual(len(result.stdout), 16_384)
        self.assertTrue(result.cleanup_complete)

    def test_snapshot_disappearance_is_rejected_after_transport(self):
        def vanished(argv, **kwargs):
            mount = argv[argv.index("--mount") + 1]
            root = mount.split("source=", 1)[1].split(",target=", 1)[0]
            (Path(root) / "main.py").unlink()
            return FinishedDocker()

        with patch("subprocess.Popen", side_effect=vanished), patch("subprocess.run", side_effect=docker_receipt):
            result = run_candidate(b"pass", IMAGE, [])
        self.assertEqual(result.status, "snapshot_changed")
        self.assertTrue(result.cleanup_complete)

    def test_parent_cleans_registered_resources_after_observer_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / "leases.json"
            lease.write_text("[]", encoding="utf-8")

            def interrupted(argv, **kwargs):
                # The syscall boundary fails after registration, before a child
                # can return. Parent receives only the durable public lease.
                raise KeyboardInterrupt()

            with patch("subprocess.Popen", side_effect=interrupted), patch(
                "subprocess.run", side_effect=OSError("observer could not clean")
            ), self.assertRaises(KeyboardInterrupt):
                run_candidate(b"pass", IMAGE, [], lease_path=lease)
            resources = json.loads(lease.read_text(encoding="utf-8"))
            self.assertEqual(len(resources), 1)
            self.assertTrue(Path(resources[0]["snapshot"]).exists())
            with patch("subprocess.run", side_effect=docker_receipt):
                self.assertTrue(cleanup_leases(lease))
            self.assertFalse(Path(resources[0]["snapshot"]).exists())

    def test_parent_cleanup_rejects_forged_resource_outside_owned_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / "leases.json"
            lease.write_text(json.dumps([{"container_name": "user-container", "snapshot": directory}]), encoding="utf-8")
            self.assertFalse(cleanup_leases(lease))
            self.assertTrue(Path(directory).is_dir())

    def test_observer_has_separate_fixed_inputs_and_external_control(self):
        with tempfile.TemporaryDirectory() as directory:
            setup = build_observer(Path(directory) / "control", IMAGE)
            self.assertEqual({p.name for p in setup.observer_root.iterdir()},
                             {"test_oracle.py", "control_runtime.py", "config.json", "pytest.ini"})
            self.assertEqual(list(setup.generation_root.iterdir()), [])
            self.assertNotEqual(setup.lease_path.parent, setup.observer_root)
            self.assertNotEqual(setup.control_path.parent, setup.generation_root)

    def test_output_collection_failure_cannot_be_completed(self):
        class BrokenPipe(io.BytesIO):
            def read(self, size):
                raise OSError("fixed capture fault")

        process = FinishedDocker()
        process.stdout = BrokenPipe()
        with patch("subprocess.Popen", return_value=process), patch("subprocess.run", side_effect=docker_receipt):
            result = run_candidate(b"pass", IMAGE, [])
        self.assertEqual(result.status, "capture_error")
        self.assertTrue(result.cleanup_complete)
