"""Public one-shot admission plus opt-in SDK/graph/Docker protocol control."""

import json
import os
from pathlib import Path
import tempfile
import unittest

import httpx

from agent.evaluation.model_pilot import prepare_trial
from agent.evaluation.pilot_execution import run_trial


ENVIRONMENT = {"AGENT_MODEL_PROVIDER": "deepseek", "AGENT_MODEL": "deepseek-flash",
               "AGENT_MODEL_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_API_KEY": "local-protocol-fixture"}


class PilotExecutionAdmissionTests(unittest.TestCase):
    def test_missing_credential_consumes_trial_without_starting_or_creating_a_charge_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            prepare_trial(root)
            result = run_trial(root, environ={})
            self.assertEqual(result["not_started_reason"], "MISSING_CREDENTIAL")
            self.assertEqual(result["paid_requests"], 0)
            self.assertFalse((root / "spend.jsonl").exists())
            with self.assertRaises(FileExistsError):
                run_trial(root, environ={})

    def test_model_mismatch_refuses_before_any_network_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            prepare_trial(root)
            result = run_trial(root, environ={**ENVIRONMENT, "AGENT_MODEL": "other-model"},
                               transport=httpx.MockTransport(lambda _: self.fail("must not send")))
            self.assertEqual(result["not_started_reason"], "MODEL_CONFIGURATION_MISMATCH")
            self.assertEqual(result["started"], 0)
            self.assertNotIn("local-protocol-fixture", (root / "execution.json").read_text())


@unittest.skipUnless(os.environ.get("SWE_AGENT_RUN_DOCKER_PILOT") == "1", "requires explicit Docker pilot environment")
class PilotSDKDockerControlTests(unittest.TestCase):
    def test_sdk_tool_editor_sqlite_observer_and_request_cap_form_one_control(self):
        requests = []
        plan = {"tasks": [{"file_path": "main.py", "logical_task": "Sum all values",
                           "atomic_tasks": [{"atomic_task": "Include the final value"}]}]}
        replies = [
            ("ResearchStep", {"reasoning": "Inspect the implementation", "hypothesis": "Read main.py"}),
            ("ResearchEvaluation", {"reasoning": "Relevant inspection", "is_valid": True}),
            ("get_raw_file_content", {"file_path": "main.py"}),
            "The implementation drops the last value; include every input integer.",
            json.dumps(plan), ("get_raw_file_content", {"file_path": "main.py"}),
            "Change the sum to include every value.",
            "<<<<<<< SEARCH\nsum(values[:-1])\n=======\nsum(values)\n>>>>>>> REPLACE",
        ]

        def provider(request):
            requests.append(json.loads(request.content))
            reply = replies[len(requests) - 1]
            message = {"role": "assistant", "content": reply if isinstance(reply, str) else None}
            if not isinstance(reply, str):
                name, arguments = reply
                message["tool_calls"] = [{"id": "fixture-" + str(len(requests)), "type": "function",
                                          "function": {"name": name, "arguments": json.dumps(arguments)}}]
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "deepseek-flash", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if not isinstance(reply, str) else "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            prepare_trial(root)
            result = run_trial(root, environ=ENVIRONMENT, transport=httpx.MockTransport(provider))
            self.assertEqual(result["status"], "FINISHED", result)
            self.assertTrue(result["resolved"], result)
            self.assertEqual(result["kind"], "OFFLINE_MODEL_PROTOCOL_CONTROL_RESULT")
            self.assertEqual(result["paid_requests"], 0)
            self.assertEqual(result["spend"]["requests"], 8)
            self.assertLessEqual(result["spend"]["reserved_microcny"], 1_000_000)
            self.assertEqual([item["phase"] for item in result["bound_attempts"]], ["baseline", "post"])
            self.assertTrue(result["cleanup_complete"])
            self.assertGreater(result["patch"]["bytes"], 0)
            self.assertEqual((root / "G" / "main.py").read_text().count("sum(values)"), 1)
            evidence = json.dumps(requests[3]["messages"])
            self.assertIn("UNTRUSTED EVIDENCE", evidence)
            self.assertIn("fixture-3", evidence)
            self.assertNotIn("local-protocol-fixture", (root / "execution.json").read_text())
            with self.assertRaises(FileExistsError):
                run_trial(root, environ=ENVIRONMENT, transport=httpx.MockTransport(provider))


if __name__ == "__main__":
    unittest.main()
