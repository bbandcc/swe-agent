"""One-trial HTTP spending admission under the frozen DeepSeek CNY contract.

This is a non-refundable request allowance, not a resumable financial ledger.
Assumes the provider follows its published V4.1 template, token and CNY prices;
the derivation and scope are in research/remaining-20261111/BUDGET_RESEARCH.md.
"""

import hashlib
import json
import os
from pathlib import Path
import threading

import httpx

MAX_REQUESTS = 8
INPUT_TOKENS = 49152
OUTPUT_TOKENS = 2048
RESERVE_MICROCNY = 114688
MAX_RESPONSE_BYTES = 128 * 1024


def _reject(code: str) -> None:
    raise httpx.TransportError(code) from None


def _constant(value: str) -> None:
    raise ValueError("nonfinite")


def _object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _schema(value: object, depth: int = 0) -> bool:
    if depth > 24:
        return False
    if type(value) is int:
        return -(2**63) <= value < 2**63
    if value is None or type(value) in (str, bool):
        return True
    if type(value) is list:
        return all(_schema(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _schema(item, depth + 1) for key, item in value.items())
    return False


def _admit(request: httpx.Request) -> tuple[httpx.Request, str]:
    url = request.url
    if (request.method != "POST" or url.scheme != "https" or url.host != "api.deepseek.com"
            or url.port not in (None, 443) or url.raw_path != b"/chat/completions" or url.userinfo):
        _reject("PILOT_REQUEST_REJECTED")
    try:
        raw = request.content
        if len(raw) > 64 * 1024:
            raise ValueError("oversized")
        body = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        allowed = {"model", "messages", "max_tokens", "stream", "thinking", "n", "temperature",
                   "tools", "tool_choice", "parallel_tool_calls", "response_format"}
        if type(body) is not dict or set(body) - allowed:
            raise ValueError("fields")
        if (body.get("model") != "deepseek-flash" or body.get("stream") is not False
                or body.get("thinking") != {"type": "disabled"}
                or type(body.get("max_tokens")) is not int or body["max_tokens"] != OUTPUT_TOKENS
                or type(body.get("n", 1)) is not int or body.get("n", 1) != 1):
            raise ValueError("policy")
        if ("temperature" in body and (type(body["temperature"]) not in (int, float)
                                      or body["temperature"] not in (0, 1))):
            raise ValueError("temperature")
        if "parallel_tool_calls" in body and body["parallel_tool_calls"] is not False:
            raise ValueError("parallel")
        messages = body.get("messages")
        if type(messages) is not list or not 1 <= len(messages) <= 32:
            raise ValueError("messages")
        for message in messages:
            if (type(message) is not dict or set(message) != {"role", "content"}
                    or message["role"] not in ("system", "user", "assistant")
                    or type(message["content"]) is not str):
                raise ValueError("message")
        tools = body.get("tools", [])
        if type(tools) is not list or len(tools) > 8:
            raise ValueError("tools")
        names = []
        for tool in tools:
            if type(tool) is not dict or set(tool) != {"type", "function"} or tool["type"] != "function":
                raise ValueError("tool")
            function = tool["function"]
            if (type(function) is not dict or set(function) - {"name", "description", "parameters", "strict"}
                    or type(function.get("name")) is not str or not function["name"]
                    or type(function.get("description")) is not str
                    or type(function.get("parameters")) is not dict
                    or not _schema(function["parameters"])
                    or ("strict" in function and type(function["strict"]) is not bool)):
                raise ValueError("function")
            names.append(function["name"])
        if len(set(names)) != len(names):
            raise ValueError("duplicate tools")
        choice = body.get("tool_choice", "auto")
        if type(choice) is dict:
            if (set(choice) != {"type", "function"} or choice["type"] != "function"
                    or type(choice["function"]) is not dict or set(choice["function"]) != {"name"}
                    or len(names) != 1 or choice["function"]["name"] != names[0]):
                raise ValueError("choice")
            body["tool_choice"] = "required"
        elif choice not in ("auto", "none", "required"):
            raise ValueError("choice")
        if "response_format" in body and body["response_format"] not in ({"type": "text"}, {"type": "json_object"}):
            raise ValueError("format")
        canonical = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        # Frozen renderer: spaced JSON <=2B; role/normalization <=256/message;
        # fixed tool instructions and format headers <4096 bytes. No raw calls.
        if len(canonical) > 16384 or 2 * len(canonical) + 256 * (len(messages) + 1) + 4096 > INPUT_TOKENS:
            raise ValueError("input bound")
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, httpx.RequestNotRead):
        _reject("PILOT_REQUEST_REJECTED")
    headers = [(key, value) for key, value in request.headers.raw if key.lower() != b"content-length"]
    normalized = httpx.Request(request.method, request.url, content=canonical,
                               headers=headers, extensions=request.extensions)
    return normalized, hashlib.sha256(canonical).hexdigest()


class CappedDeepSeekTransport(httpx.BaseTransport):
    """Gate every final HTTP dispatch; each instance owns a new trial ledger."""

    def __init__(self, inner_transport: httpx.BaseTransport, ledger_path: str | Path):
        self._inner = inner_transport
        self._lock = threading.Lock()
        self._requests = self._settled = 0
        self._unknown = self._blocked = self._closed = False
        path = Path(ledger_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._writer = path.open("x", encoding="utf-8")
            self._record("POLICY", max_requests=MAX_REQUESTS, reserve_microcny=RESERVE_MICROCNY,
                         max_input_tokens=INPUT_TOKENS, max_output_tokens=OUTPUT_TOKENS)
        except OSError:
            if hasattr(self, "_writer"):
                self._writer.close()
            _reject("PILOT_LEDGER_UNAVAILABLE")

    def _record(self, event: str, **metadata) -> None:
        self._writer.write(json.dumps({"event": event, **metadata}, separators=(",", ":")) + "\n")
        self._writer.flush()
        os.fsync(self._writer.fileno())

    def summary(self) -> dict:
        """Return metadata only; settled cost is a conservative known subtotal."""
        return {"currency": "CNY", "requests": self._requests,
                "reserved_microcny": self._requests * RESERVE_MICROCNY,
                "settled_microcny": self._settled, "unknown": self._unknown,
                "blocked": self._blocked, "max_requests": MAX_REQUESTS,
                "hard_limit_microcny": 1000000}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not self._lock.acquire(blocking=False):
            _reject("PILOT_REQUEST_IN_FLIGHT")
        try:
            if self._closed or self._blocked:
                _reject("PILOT_SPEND_BLOCKED")
            if self._requests >= MAX_REQUESTS:
                _reject("PILOT_SPEND_LIMIT")
            request, digest = _admit(request)
            self._requests += 1
            try:
                self._record("RESERVED", ordinal=self._requests, digest=digest,
                             reserve_microcny=RESERVE_MICROCNY)
                response = self._inner.handle_request(request)
                try:
                    if not 200 <= response.status_code < 300:
                        raise ValueError("status")
                    if response.is_stream_consumed:
                        content = response.content
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise ValueError("size")
                    else:
                        chunks, size = [], 0
                        for chunk in response.iter_bytes(chunk_size=16384):
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise ValueError("size")
                            chunks.append(chunk)
                        content = b"".join(chunks)
                    data = json.loads(content, object_pairs_hook=_object, parse_constant=_constant)
                    usage = data.get("usage")
                    if data.get("model") != "deepseek-flash" or type(usage) is not dict:
                        raise ValueError("usage")
                    incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
                    if (type(incoming) is not int or not 0 <= incoming <= INPUT_TOKENS
                            or type(outgoing) is not int or not 0 <= outgoing <= OUTPUT_TOKENS):
                        raise ValueError("usage bounds")
                    if "total_tokens" in usage and (type(usage["total_tokens"]) is not int
                                                    or usage["total_tokens"] != incoming + outgoing):
                        raise ValueError("usage total")
                    cost = incoming * 2 + outgoing * 8
                    self._record("SETTLED", ordinal=self._requests, prompt_tokens=incoming,
                                 completion_tokens=outgoing, cost_microcny=cost)
                    self._settled += cost
                    headers = [(key, value) for key, value in response.headers.raw
                               if key.lower() not in (b"content-encoding", b"content-length")]
                    return httpx.Response(response.status_code, headers=headers,
                                          content=content, request=request, extensions=response.extensions)
                finally:
                    response.close()
            except BaseException as error:
                self._unknown = self._blocked = True
                try:
                    self._record("UNKNOWN", ordinal=self._requests)
                except OSError:
                    pass  # Memory remains blocked even when the disk can no longer record.
                if not isinstance(error, Exception):
                    raise
                _reject("PILOT_DISPATCH_UNKNOWN")
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._inner.close()
                finally:
                    self._writer.close()
