"""Process-local filtering of configured secrets before durable state writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from typing import Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

REDACTION_MARKER = "[REDACTED_KNOWN_SECRET]"
SENSITIVE_DATA_MESSAGE = (
    "A configured secret was found in code-bearing content; the update was rejected."
)
_CODE_BEARING_STATE_KEYS = frozenset(
    {
        "current_file_content",
        "current_file_snapshot",
        "current_file_transaction",
        "edit_proposal",
        "file_content",
        "last_edit_result",
        "new_file_content",
        "new_text",
        "old_text",
        "patch",
        "proposal",
        "pending_write",
        "working_content",
    }
)


class SecretFilterResult:
    """Sanitized value plus non-secret detection facts."""

    __slots__ = ("value", "redacted", "code_bearing")

    def __init__(
        self, value: Any, *, redacted: bool = False, code_bearing: bool = False
    ) -> None:
        self.value = value
        self.redacted = redacted
        self.code_bearing = code_bearing


class KnownSecretStream:
    """Incremental bytes redactor with overlap across chunk boundaries."""

    def __init__(self, secret_filter: "KnownSecretFilter") -> None:
        self._secrets = tuple(
            secret.encode("utf-8") for secret in secret_filter._secrets
        )
        self._marker = secret_filter.marker.encode("utf-8")
        self._pending = b""
        self._redacted = False
        self._finished = False

    @property
    def redacted(self) -> bool:
        return self._redacted

    def feed(self, chunk: bytes) -> bytes:
        if self._finished:
            raise RuntimeError("Secret stream is already finished.")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("Secret stream chunks must be bytes-like.")
        if not self._secrets:
            return bytes(chunk)
        return self._process(bytes(chunk), final=False)

    def finish(self) -> bytes:
        if self._finished:
            return b""
        self._finished = True
        if not self._secrets:
            return b""
        return self._process(b"", final=True)

    def _process(self, chunk: bytes, *, final: bool) -> bytes:
        combined = self._pending + chunk
        if final:
            process = combined
            self._pending = b""
        else:
            # Keep a suffix that may still become the beginning of a secret.
            # Merely retaining ``max_secret_len - 1`` characters is not enough:
            # for ``SE`` + ``CRET`` the first character would otherwise be
            # emitted before the complete secret is visible.  Move the emit
            # boundary back when a complete secret crosses it.
            overlap = max(len(self._secrets[0]) - 1, 0)
            safe_end = max(len(combined) - overlap, 0)
            changed = True
            while changed:
                changed = False
                for secret in self._secrets:
                    start = combined.find(secret)
                    while start >= 0 and start < safe_end:
                        end = start + len(secret)
                        if end > safe_end:
                            safe_end = start
                            changed = True
                            break
                        start = combined.find(secret, start + 1)
                    if changed:
                        break
            if safe_end <= 0:
                self._pending = combined
                return b""
            process = combined[:safe_end]
            self._pending = combined[safe_end:]
        redacted = process
        for secret in self._secrets:
            redacted = redacted.replace(secret, self._marker)
        self._redacted = self._redacted or redacted != process
        return redacted


class KnownSecretFilter:
    """Replace only explicitly configured secrets in process-local values.

    The raw values are retained only by this short-lived filter instance. They
    are never returned in a result, state update, or checkpoint-safe object.
    """

    def __init__(
        self, secrets: Sequence[str] = (), *, marker: str = REDACTION_MARKER
    ) -> None:
        if not isinstance(marker, str) or not marker:
            raise ValueError("marker must be a non-empty string.")
        normalized = {
            secret
            for secret in secrets
            if isinstance(secret, str) and secret
        }
        self._secrets = tuple(sorted(normalized, key=lambda value: (-len(value), value)))
        self.marker = marker

    @classmethod
    def from_run_config(cls, config: Any) -> "KnownSecretFilter":
        settings = getattr(config, "model", None)
        api_key = getattr(settings, "api_key", None)
        if api_key is None:
            return cls()
        value = (
            api_key.get_secret_value()
            if hasattr(api_key, "get_secret_value")
            else None
        )
        return cls((value,) if isinstance(value, str) else ())

    @property
    def secret_count(self) -> int:
        """Expose only a count for diagnostics; never expose secret values."""
        return len(self._secrets)

    def redact_text(self, value: str) -> str:
        """Return deterministic text redaction without exposing the input secret."""
        if not isinstance(value, str):
            raise TypeError("value must be a string.")
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, self.marker)
        return redacted

    def contains_secret(self, value: str) -> bool:
        """Return whether a text value contains one of the known secrets."""
        if not isinstance(value, str):
            raise TypeError("value must be a string.")
        return bool(self._secrets) and self.redact_text(value) != value

    def stream(self) -> KnownSecretStream:
        """Create a process-local redactor for bytes read in chunks."""
        return KnownSecretStream(self)

    def sanitize(
        self, value: Any, *, code_bearing: bool = False
    ) -> SecretFilterResult:
        """Recursively sanitize checkpoint-safe values.

        ``code_bearing`` is deliberately supplied by the state boundary rather
        than inferred from arbitrary field names. This keeps tool and
        verification diagnostics redactable while making pending source and
        edit content rejectable.
        """
        # Preserve str-backed enum values while rebuilding dataclasses.
        if isinstance(value, Enum):
            return SecretFilterResult(value)
        if isinstance(value, str):
            redacted = self.redact_text(value)
            found = redacted != value
            return SecretFilterResult(
                redacted,
                redacted=found,
                code_bearing=found and code_bearing,
            )
        if isinstance(value, BaseMessage):
            return self._sanitize_model(value, code_bearing=code_bearing)
        if isinstance(value, BaseModel):
            return self._sanitize_model(value, code_bearing=code_bearing)
        if is_dataclass(value) and not isinstance(value, type):
            return self._sanitize_dataclass(value, code_bearing=code_bearing)
        if isinstance(value, Mapping):
            return self._sanitize_mapping(value, code_bearing=code_bearing)
        if isinstance(value, list):
            return self._sanitize_sequence(value, list, code_bearing=code_bearing)
        if isinstance(value, tuple):
            return self._sanitize_sequence(value, tuple, code_bearing=code_bearing)
        return SecretFilterResult(value)

    def sanitize_state_update(
        self, update: Mapping[str, Any]
    ) -> SecretFilterResult:
        """Sanitize a node update and report code-bearing detection."""
        if not isinstance(update, Mapping):
            raise TypeError("state update must be a mapping.")
        safe: dict[Any, Any] = {}
        redacted = False
        code_bearing = False
        for key, value in update.items():
            key_result = self.sanitize(key)
            value_result = self.sanitize(
                value,
                code_bearing=(
                    isinstance(key, str) and key in _CODE_BEARING_STATE_KEYS
                ),
            )
            safe[key_result.value] = value_result.value
            redacted = redacted or key_result.redacted or value_result.redacted
            code_bearing = code_bearing or value_result.code_bearing
        return SecretFilterResult(
            safe, redacted=redacted, code_bearing=code_bearing
        )

    def sanitize_node_update(self, update: Mapping[str, Any]) -> dict[str, Any]:
        """Return a safe graph update, stopping on code-bearing detection."""
        result = self.sanitize_state_update(update)
        safe = dict(result.value)
        if result.code_bearing:
            from agent.runtime.budget import BudgetErrorCode

            safe["runtime_error_code"] = BudgetErrorCode.SENSITIVE_DATA_DETECTED
            safe["runtime_message"] = SENSITIVE_DATA_MESSAGE
        return safe

    def wrap_node(self, node):
        """Wrap a public LangGraph node without touching ToolNode internals."""
        def wrapped(*args, **kwargs):
            try:
                if hasattr(node, "invoke"):
                    update = node.invoke(*args, **kwargs)
                else:
                    update = node(*args, **kwargs)
            except Exception as error:
                # LangGraph may persist an exception as a pending write before
                # propagating it.  Convert only known-secret exceptions here;
                # ordinary programming failures keep their original behavior.
                if self._exception_contains_secret(error):
                    from agent.runtime.budget import BudgetErrorCode

                    return {
                        "runtime_error_code": BudgetErrorCode.SENSITIVE_DATA_DETECTED,
                        "runtime_message": SENSITIVE_DATA_MESSAGE,
                    }
                raise
            if not isinstance(update, Mapping):
                return update
            return self.sanitize_node_update(update)

        return wrapped

    def _sanitize_model(
        self, value: BaseModel, *, code_bearing: bool
    ) -> SecretFilterResult:
        updates: dict[str, Any] = {}
        redacted = False
        detected = False
        for name in value.model_fields:
            child = self.sanitize(
                getattr(value, name), code_bearing=code_bearing
            )
            updates[name] = child.value
            redacted = redacted or child.redacted
            detected = detected or child.code_bearing
        return SecretFilterResult(
            value.model_copy(update=updates),
            redacted=redacted,
            code_bearing=detected,
        )

    def _sanitize_dataclass(
        self, value: Any, *, code_bearing: bool
    ) -> SecretFilterResult:
        updates: dict[str, Any] = {}
        redacted = False
        detected = False
        for field in fields(value):
            child = self.sanitize(
                getattr(value, field.name), code_bearing=code_bearing
            )
            updates[field.name] = child.value
            redacted = redacted or child.redacted
            detected = detected or child.code_bearing
        return SecretFilterResult(
            replace(value, **updates),
            redacted=redacted,
            code_bearing=detected,
        )

    def _sanitize_mapping(
        self, value: Mapping[Any, Any], *, code_bearing: bool
    ) -> SecretFilterResult:
        safe: dict[Any, Any] = {}
        redacted = False
        detected = False
        for key, item in value.items():
            safe_key = self.sanitize(key)
            safe_item = self.sanitize(item, code_bearing=code_bearing)
            safe[safe_key.value] = safe_item.value
            redacted = redacted or safe_key.redacted or safe_item.redacted
            detected = detected or safe_item.code_bearing
        return SecretFilterResult(
            safe, redacted=redacted, code_bearing=detected
        )

    def _sanitize_sequence(
        self, value: Sequence[Any], factory, *, code_bearing: bool
    ) -> SecretFilterResult:
        items = [
            self.sanitize(item, code_bearing=code_bearing)
            for item in value
        ]
        return SecretFilterResult(
            factory(item.value for item in items),
            redacted=any(item.redacted for item in items),
            code_bearing=any(item.code_bearing for item in items),
        )

    def _exception_contains_secret(self, error: Exception) -> bool:
        for formatter in (str, repr):
            try:
                if self.contains_secret(formatter(error)):
                    return True
            except Exception:
                continue
        return False


__all__ = [
    "KnownSecretFilter",
    "KnownSecretStream",
    "REDACTION_MARKER",
    "SENSITIVE_DATA_MESSAGE",
    "SecretFilterResult",
]
