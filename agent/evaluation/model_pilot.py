"""Freeze one trial's inputs; checks remain separate from paid execution."""

import hashlib
import json
import math
import os
from pathlib import Path
from collections.abc import Mapping

from agent.evaluation.pilot_control import build_observer, oracle_source_for, PYTEST_CONFIG_BYTES
from agent.evaluation.pilot_tasks import get_pilot_task

REPOSITORY = Path(__file__).resolve().parents[2]
IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
MODEL = {"provider": "deepseek", "model": "deepseek-flash", "base_url": "https://api.deepseek.com"}
TASK = get_pilot_task("json-sum").instruction
ORIGINAL = get_pilot_task("json-sum").original
MODEL_POLICY = {
    "model_version": "DeepSeek-V4.1-Flash-1017", "thinking": "disabled",
    "max_output_tokens": 2048, "max_requests": 8, "input_token_upper_bound": 49152,
    "input_cny_per_million": 2, "output_cny_per_million": 8,
    "reserved_upper_bound_cny": "0.917504",
    "pricing_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
    "pricing_verified_on": "2026-11-12",
    "template_commit": "93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170",
    "tools": ["get_raw_file_content"], "max_atomic_tasks": 1,
    "assumptions": "Provider follows published text template, tokenizer, prices and max_tokens protocol.",
}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: dict) -> str:
    return _hash(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _source_pins() -> dict[str, str]:
    files = sorted(p for name in ("agent", "helpers", "scripts")
                   for p in (REPOSITORY / name).rglob("*")
                   if p.is_file() and p.suffix in {".py", ".md"})
    files += [REPOSITORY / "pyproject.toml", REPOSITORY / "uv.lock"]
    return {p.relative_to(REPOSITORY).as_posix(): _hash(p.read_bytes()) for p in files}


def prepare_trial(output: str | Path, *, timeout_seconds: float = 1800,
                  max_steps: int = 100, task_id: str = "json-sum") -> dict:
    """Create an immutable-input one-trial plan, without any network calls."""
    _validate_limits(timeout_seconds, max_steps)
    task = get_pilot_task(task_id)
    root = Path(output).absolute()
    root.mkdir(parents=True, exist_ok=False)
    setup = build_observer(root / "observer", IMAGE, generation_root=root / "G", task_id=task_id)
    (setup.generation_root / "main.py").write_bytes(task.original)
    frozen = {p.relative_to(root).as_posix(): _hash(p.read_bytes())
              for p in (setup.generation_root / "main.py", *setup.observer_root.iterdir())}
    plan = {"schema_version": 1, "kind": "REAL_MODEL_TRIAL_PLAN", "status": "PREPARED",
            "scheduled": 1, "started": 0, "finished": 0, "model": MODEL,
            "model_policy": MODEL_POLICY,
            "budget": {"currency": "CNY", "hard_limit": "1"}, "task": task.instruction,
            "task_id": task_id, "task_fingerprint": task.fingerprint,
            "image": IMAGE, "timeout_seconds": timeout_seconds, "max_steps": max_steps,
            "allowed_edit_scope": ["main.py"], "frozen_inputs": frozen,
            "source_pins": _source_pins(), "pricing_status": "MUST_VERIFY_BEFORE_EXECUTION",
            "hard_budget_status": "SOFTWARE_BOUND_PUBLISHED_PROTOCOL", "automatic_resume": False,
            "automatic_retry": False}
    plan["plan_digest"] = _digest(plan)
    (root / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def _validate_limits(timeout_seconds: float, max_steps: int) -> None:
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 1800):
        raise ValueError("timeout_seconds must be finite, positive and at most 1800.")
    if type(max_steps) is not int or not 0 < max_steps <= 100:
        raise ValueError("max_steps must be an integer between 1 and 100.")


def load_trial(output: str | Path) -> tuple[Path, dict]:
    """Validate the complete frozen identity before any execution attempt."""
    root = Path(output).resolve(strict=True)
    path = root / "plan.json"
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError("Prepared plan is unsafe or oversized.")
    plan = json.loads(path.read_text(encoding="utf-8"))
    digest = plan.pop("plan_digest")
    task = get_pilot_task(plan.get("task_id"))
    _validate_limits(plan.get("timeout_seconds"), plan.get("max_steps"))
    observer = root / "observer"
    expected = {"G/main.py": _hash(task.original),
                "observer/O/control_runtime.py": _hash(Path(__file__).with_name("pilot_control.py").read_bytes()),
                "observer/O/test_oracle.py": _hash(oracle_source_for(task.task_id).encode("utf-8")),
                "observer/O/pytest.ini": _hash(PYTEST_CONFIG_BYTES),
                "observer/O/config.json": _hash(json.dumps({
                    "source_path": str(root / "G" / "main.py"), "image": IMAGE, "task_id": task.task_id,
                    "receipt_path": str(observer / "receipt.json"), "control_path": str(observer / "control.json"),
                    "lease_path": str(observer / "leases.json")}, sort_keys=True).encode("utf-8"))}
    if (digest != _digest(plan) or plan.get("model") != MODEL
            or plan.get("budget") != {"currency": "CNY", "hard_limit": "1"}
            or plan.get("source_pins") != _source_pins()
            or plan.get("model_policy") != MODEL_POLICY
            or plan.get("task_fingerprint") != task.fingerprint
            or plan.get("task") != task.instruction or plan.get("frozen_inputs") != expected
            or plan.get("image") != IMAGE or plan.get("allowed_edit_scope") != ["main.py"]
            or plan.get("automatic_retry") is not False or plan.get("automatic_resume") is not False
            or plan.get("schema_version") != 1 or plan.get("kind") != "REAL_MODEL_TRIAL_PLAN"
            or plan.get("status") != "PREPARED" or plan.get("scheduled") != 1
            or plan.get("started") != 0 or plan.get("finished") != 0):
        raise ValueError("Prepared plan or source identity changed.")
    for name, expected in plan["frozen_inputs"].items():
        candidate = root / name
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            raise ValueError("Prepared input path changed.")
        if _hash(candidate.read_bytes()) != expected:
            raise ValueError("Prepared input bytes changed.")
    plan["plan_digest"] = digest
    return root, plan


def execute_trial(output: str | Path, *, environ: Mapping[str, str] | None = None) -> dict:
    """Historical check-only seam; never dispatch a model or rewrite its row."""
    root, plan = load_trial(output)
    digest = plan["plan_digest"]
    source = os.environ if environ is None else environ
    reason = ("MISSING_CREDENTIAL" if not source.get("DEEPSEEK_API_KEY", "").strip()
              else "CHECK_ONLY_NO_PAID_DISPATCH")
    result = {"kind": "REAL_MODEL_TRIAL_RESULT", "plan_digest": digest,
              "status": "NOT_STARTED", "scheduled": 1, "started": 0, "finished": 0,
              "resolved": False, "not_started_reason": reason,
              "hard_budget_status": "SOFTWARE_BOUND_PUBLISHED_PROTOCOL", "usage_coverage": None,
              "cost_usd": None, "cost_cny": None}
    with (root / "execution.json").open("x", encoding="utf-8") as writer:
        json.dump(result, writer, indent=2)
    return result
