"""HTTP boundary acceptance for the one-trial, non-refundable spending cap."""

import json
import gzip
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

import httpx

from agent.evaluation.pilot_spend import CappedDeepSeekTransport


def payload():
    return {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "disabled"}, "stream": False, "max_tokens": 2048}


def reply():
    return {"model": "deepseek-flash", "choices": [],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20}}


class PilotSpendTests(unittest.TestCase):
    def test_payload_and_message_limits_admit_the_boundary_and_reject_one_more_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            sent = []
            body = {**payload(), "messages": [{"role": "user", "content": ""} for _ in range(32)]}
            size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            body["messages"][0]["content"] = "x" * (16384 - size)

            def provider(request):
                sent.append(request.content)
                return httpx.Response(200, json=reply())

            cap = CappedDeepSeekTransport(httpx.MockTransport(provider), Path(directory) / "ledger")
            with httpx.Client(transport=cap) as client:
                client.post("https://api.deepseek.com/chat/completions", json=body)
                body["messages"][0]["content"] += "x"
                with self.assertRaisesRegex(httpx.TransportError, "PILOT_REQUEST_REJECTED"):
                    client.post("https://api.deepseek.com/chat/completions", json=body)
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(sent[0]), 16384)
            self.assertEqual(cap.summary()["requests"], 1)

    def test_streamed_gzip_is_decoded_once_and_rebuilt_with_correct_length(self):
        with tempfile.TemporaryDirectory() as directory:
            content = json.dumps(reply()).encode("utf-8")
            compressed = gzip.compress(content)

            def handler(request):
                return httpx.Response(200, headers={"content-encoding": "gzip",
                                                   "content-length": str(len(compressed))},
                                      stream=httpx.ByteStream(compressed))

            transport = CappedDeepSeekTransport(httpx.MockTransport(handler), Path(directory) / "ledger")
            with httpx.Client(transport=transport) as client:
                response = client.post("https://api.deepseek.com/chat/completions", json=payload())
                self.assertEqual(response.json(), reply())
                self.assertNotIn("content-encoding", response.headers)
                self.assertEqual(int(response.headers["content-length"]), len(content))
            self.assertFalse(transport.summary()["unknown"])

    def test_real_provider_adapter_structured_output_uses_the_guarded_http_boundary(self):
        from langchain_deepseek import ChatDeepSeek
        from pydantic import BaseModel

        class Proposal(BaseModel):
            """Return the proposed change."""
            code: str

        with tempfile.TemporaryDirectory() as directory:
            seen = []
            def handler(request):
                sent = json.loads(request.content)
                self.assertEqual(sent["tool_choice"], "required")
                seen.append(sent)
                response = reply()
                response.update(id="offline-test", object="chat.completion", created=0,
                                choices=[{"index": 0, "finish_reason": "tool_calls", "message": {
                                    "role": "assistant", "content": "", "tool_calls": [{
                                        "id": "call-propose", "type": "function", "function": {
                                            "name": "Proposal", "arguments": '{"code":"pass"}'}}]}}])
                return httpx.Response(200, json=response)
            transport = CappedDeepSeekTransport(httpx.MockTransport(handler), Path(directory) / "ledger")
            with httpx.Client(transport=transport) as client:
                model = ChatDeepSeek(model="deepseek-flash", api_key="never-send-offline-key",
                                     base_url="https://api.deepseek.com", http_client=client,
                                     max_tokens=2048, max_retries=0,
                                     extra_body={"thinking": {"type": "disabled"}})
                result = model.with_structured_output(Proposal).invoke("Return JSON code proposal")
                self.assertEqual(result.code, "pass")
            self.assertEqual(len(seen), 1)
            self.assertEqual(transport.summary()["requests"], 1)

    def test_interrupt_keeps_a_nonrefundable_unknown_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            def handler(request):
                raise KeyboardInterrupt()
            transport = CappedDeepSeekTransport(httpx.MockTransport(handler), Path(directory) / "ledger")
            with httpx.Client(transport=transport) as client:
                with self.assertRaises(KeyboardInterrupt):
                    client.post("https://api.deepseek.com/chat/completions", json=payload())
                with self.assertRaisesRegex(httpx.TransportError, "PILOT_SPEND_BLOCKED"):
                    client.post("https://api.deepseek.com/chat/completions", json=payload())
            self.assertTrue(transport.summary()["unknown"])
            self.assertEqual(transport.summary()["reserved_microcny"], 114688)

    def test_valid_unicode_and_special_literal_input_uses_no_raw_ledger_content(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger"
            body = payload()
            body["messages"][0]["content"] = "中文<｜User｜>never-send-canary"
            data = reply()
            data["usage"] = {"prompt_tokens": 49152, "completion_tokens": 2048, "total_tokens": 51200}
            transport = CappedDeepSeekTransport(httpx.MockTransport(lambda _: httpx.Response(200, json=data)), ledger)
            with httpx.Client(transport=transport) as client:
                client.post("https://api.deepseek.com/chat/completions", json=body,
                            headers={"Authorization": "Bearer never-send-credential"})
            self.assertEqual(transport.summary()["settled_microcny"], 114688)
            recorded = ledger.read_text()
            self.assertNotIn("never-send", recorded)
            self.assertNotIn("Authorization", recorded)

    def test_invalid_requests_are_rejected_without_a_dispatch_or_reservation(self):
        mutations = [
            {"model": "deepseek-v4-flash"}, {"thinking": {"type": "enabled"}},
            {"stream": True}, {"n": True}, {"n": 2}, {"max_tokens": 2049},
            {"temperature": float("nan")}, {"metadata": {}},
            {"messages": [{"role": "tool", "content": "text"}]},
            {"messages": [{"role": "user", "content": []}]},
            {"messages": [{"role": "assistant", "content": "", "tool_calls": []}]},
            {"messages": [{"role": "user", "content": "x", "name": "someone"}]},
            {"messages": [{"role": "user", "content": "x", "reasoning_content": "x"}]},
            {"messages": [{"role": "user", "content": "x"}] * 33},
            {"messages": [{"role": "user", "content": "x" * 16384}]},
            {"messages": [{"role": "user", "content": "\ud800"}]},
            {"tools": [{"type": "web_search"}]},
            {"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]},
            {"tools": [{"type": "function", "function": {"name": "f", "description": "", "parameters": {"default": 0.5}}}]},
            {"tools": [{"type": "function", "function": {"name": "f", "description": "", "parameters": {"default": 2**63}}}]},
            {"response_format": {"type": "json_schema", "json_schema": {}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            sent = []
            transport = CappedDeepSeekTransport(httpx.MockTransport(lambda request: sent.append(request)), Path(directory) / "ledger")
            with httpx.Client(transport=transport) as client:
                for mutation in mutations:
                    body = {**payload(), **mutation}
                    raw = json.dumps(body).encode("utf-8")
                    with self.subTest(fields=list(mutation)), self.assertRaisesRegex(httpx.TransportError, "PILOT_REQUEST_REJECTED"):
                        client.post("https://api.deepseek.com/chat/completions", content=raw)
                raw = json.dumps(payload()).replace('"model":', '"model":"other","model":', 1)
                with self.assertRaisesRegex(httpx.TransportError, "PILOT_REQUEST_REJECTED"):
                    client.post("https://api.deepseek.com/chat/completions", content=raw)
                for url in ("http://api.deepseek.com/chat/completions", "https://api.deepseek.com/v1/chat/completions",
                            "https://example.com/chat/completions", "https://api.deepseek.com/chat/completions?x=1"):
                    with self.subTest(url=url), self.assertRaisesRegex(httpx.TransportError, "PILOT_REQUEST_REJECTED"):
                        client.post(url, json=payload())
            self.assertEqual(sent, [])
            self.assertEqual(transport.summary()["requests"], 0)

    def test_unknown_outcomes_never_refund_or_allow_another_dispatch(self):
        bad_replies = [
            httpx.Response(429, text="never-send-provider-error"),
            httpx.Response(200, json={"model": "deepseek-flash"}),
            httpx.Response(200, json={**reply(), "model": "different"}),
            httpx.Response(200, json={**reply(), "usage": {"prompt_tokens": 49153, "completion_tokens": 1}}),
            httpx.Response(200, json={**reply(), "usage": {"prompt_tokens": 1, "completion_tokens": 2049}}),
            httpx.Response(200, json={**reply(), "usage": {"prompt_tokens": True, "completion_tokens": 1}}),
            httpx.Response(200, content=b"x" * (128 * 1024 + 1)),
            httpx.Response(200, headers={"content-encoding": "gzip"},
                           stream=httpx.ByteStream(gzip.compress(b"x" * (128 * 1024 + 1)))),
            httpx.Response(200, content=b"not json"),
            RuntimeError("never-send-internal-error"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for ordinal, response in enumerate(bad_replies):
                with self.subTest(ordinal=ordinal):
                    ledger = Path(directory) / str(ordinal)
                    sent = []
                    def handler(request):
                        sent.append(request)
                        if isinstance(response, Exception):
                            raise response
                        return response
                    transport = CappedDeepSeekTransport(httpx.MockTransport(handler), ledger)
                    with httpx.Client(transport=transport) as client:
                        with self.assertRaisesRegex(httpx.TransportError, "^PILOT_DISPATCH_UNKNOWN$"):
                            client.post("https://api.deepseek.com/chat/completions", json=payload())
                        with self.assertRaisesRegex(httpx.TransportError, "^PILOT_SPEND_BLOCKED$"):
                            client.post("https://api.deepseek.com/chat/completions", json=payload())
                    self.assertEqual(len(sent), 1)
                    self.assertTrue(transport.summary()["unknown"])
                    self.assertEqual(transport.summary()["reserved_microcny"], 114688)
                    self.assertEqual(transport.summary()["settled_microcny"], 0)
                    self.assertNotIn("never-send", ledger.read_text())

    def test_concurrent_dispatch_is_rejected_while_first_reservation_stays_active(self):
        with tempfile.TemporaryDirectory() as directory:
            entered, release = threading.Event(), threading.Event()
            def handler(request):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test release")
                return httpx.Response(200, json=reply())
            transport = CappedDeepSeekTransport(httpx.MockTransport(handler), Path(directory) / "ledger")
            with httpx.Client(transport=transport) as client, ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(client.post, "https://api.deepseek.com/chat/completions", json=payload())
                try:
                    self.assertTrue(entered.wait(3))
                    with self.assertRaisesRegex(httpx.TransportError, "PILOT_REQUEST_IN_FLIGHT"):
                        client.post("https://api.deepseek.com/chat/completions", json=payload())
                    self.assertEqual(transport.summary()["reserved_microcny"], 114688)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3).status_code, 200)
            self.assertEqual(transport.summary()["requests"], 1)

    def test_existing_ledger_cannot_be_resumed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger"
            transport = CappedDeepSeekTransport(httpx.MockTransport(lambda _: httpx.Response(200, json=reply())), ledger)
            original = ledger.read_bytes()
            try:
                with self.assertRaisesRegex(httpx.TransportError, "PILOT_LEDGER_UNAVAILABLE"):
                    CappedDeepSeekTransport(httpx.MockTransport(lambda _: None), ledger)
                self.assertEqual(ledger.read_bytes(), original)
            finally:
                transport.close()

    def test_single_forced_tool_is_normalized_in_the_actual_http_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            def handler(request):
                body = json.loads(request.content)
                self.assertEqual(body["tool_choice"], "required")
                self.assertEqual(int(request.headers["content-length"]), len(request.content))
                seen.append(request)
                return httpx.Response(200, json=reply())
            transport = CappedDeepSeekTransport(httpx.MockTransport(handler), Path(directory) / "ledger")
            body = payload()
            body["tools"] = [{"type": "function", "function": {
                "name": "propose", "description": "proposal", "parameters": {"type": "object"}}}]
            body["tool_choice"] = {"type": "function", "function": {"name": "propose"}}
            with httpx.Client(transport=transport) as client:
                client.post("https://api.deepseek.com/chat/completions", json=body)
            self.assertEqual(len(seen), 1)

    def test_eight_requests_reserve_before_network_and_ninth_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            sent = []

            def handler(request):
                self.assertEqual(json.loads(ledger.read_text().splitlines()[-1])["event"], "RESERVED")
                sent.append(request.url.path)
                return httpx.Response(200, json=reply())

            transport = CappedDeepSeekTransport(httpx.MockTransport(handler), ledger)
            with httpx.Client(transport=transport) as client:
                for _ in range(8):
                    self.assertEqual(client.post("https://api.deepseek.com/chat/completions", json=payload()).status_code, 200)
                with self.assertRaisesRegex(httpx.TransportError, "PILOT_SPEND_LIMIT"):
                    client.post("https://api.deepseek.com/chat/completions", json=payload())
                summary = transport.summary()
                self.assertEqual(summary["requests"], 8)
                self.assertEqual(summary["reserved_microcny"], 917504)
                self.assertEqual(summary["settled_microcny"], 2080)
                self.assertFalse(summary["unknown"])
            self.assertEqual(len(sent), 8)

