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
        "last_edit_result",
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
            if hasattr(node, "invoke"):
                update = node.invoke(*args, **kwargs)
            else:
                update = node(*args, **kwargs)
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


__all__ = [
    "KnownSecretFilter",
    "REDACTION_MARKER",
    "SENSITIVE_DATA_MESSAGE",
    "SecretFilterResult",
]
