import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from agent.config import ModelSettings, build_chat_model
from agent.runtime import (
    BudgetErrorCode,
    UsageStatus,
    capture_model_exception,
    classify_model_exception,
)


class _SlowCompletionHandler(BaseHTTPRequestHandler):
    requests: list[str] = []
    request_seen = threading.Event()
    delay_seconds = 0.35

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).requests.append(self.path)
        type(self).request_seen.set()
        time.sleep(type(self).delay_seconds)
        payload = json.dumps(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "late"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            # The client is expected to close the timed-out request.
            pass

    def log_message(self, *_args: object) -> None:
        return


class _ActiveDisconnectHandler(BaseHTTPRequestHandler):
    requests = 0
    request_seen = threading.Event()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).requests += 1
        type(self).request_seen.set()
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def log_message(self, *_args: object) -> None:
        return


class ModelRequestTimeoutTests(unittest.TestCase):
    def test_active_disconnect_is_one_attempt_with_unknown_usage(self) -> None:
        _ActiveDisconnectHandler.requests = 0
        _ActiveDisconnectHandler.request_seen = threading.Event()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ActiveDisconnectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            model = build_chat_model(
                ModelSettings(
                    provider="deepseek",
                    model="deepseek-chat",
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    api_key=SecretStr("test-key"),
                ),
                max_output_tokens=16,
                request_timeout_seconds=1.0,
                max_retries=0,
            )
            with self.assertRaises(Exception) as raised:
                model.invoke([HumanMessage(content="disconnect probe")])

            self.assertTrue(_ActiveDisconnectHandler.request_seen.wait(1.0))
            time.sleep(0.05)
            self.assertEqual(_ActiveDisconnectHandler.requests, 1)
            self.assertEqual(
                classify_model_exception(raised.exception),
                BudgetErrorCode.MODEL_TRANSPORT_ERROR,
            )
            failure = capture_model_exception(BudgetErrorCode.MODEL_TRANSPORT_ERROR)
            self.assertEqual(failure.usage.status, UsageStatus.UNKNOWN)
            self.assertIsNone(failure.usage.input_tokens)
            self.assertIsNone(failure.usage.output_tokens)
            self.assertIsNone(failure.usage.total_tokens)
            self.assertIsNone(failure.usage.cost_microusd)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_unexpected_local_errors_are_not_transport_classified(self) -> None:
        self.assertIsNone(classify_model_exception(OSError("local filesystem failure")))
        self.assertIsNone(classify_model_exception(RuntimeError("programming failure")))

    def test_deepseek_timeout_is_one_attempt_and_late_response_does_not_continue(self) -> None:
        _SlowCompletionHandler.requests = []
        _SlowCompletionHandler.request_seen = threading.Event()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowCompletionHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            model = build_chat_model(
                ModelSettings(
                    provider="deepseek",
                    model="deepseek-chat",
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    api_key=SecretStr("test-key"),
                ),
                max_output_tokens=16,
                request_timeout_seconds=0.05,
                max_retries=0,
            )
            started = time.monotonic()
            with self.assertRaises(Exception) as raised:
                model.invoke([HumanMessage(content="timeout probe")])
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.0)
            self.assertTrue(_SlowCompletionHandler.request_seen.wait(1.0))
            time.sleep(_SlowCompletionHandler.delay_seconds + 0.05)
            self.assertEqual(_SlowCompletionHandler.requests, ["/v1/chat/completions"])
            self.assertEqual(
                classify_model_exception(raised.exception),
                BudgetErrorCode.MODEL_REQUEST_TIMEOUT,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
