"""One bounded model trial through the existing graph, SQLite and observer."""

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import httpx
from langchain_core.messages import HumanMessage
from langchain_deepseek import ChatDeepSeek

from agent.architect.runtime import durable_architect_runtime
from agent.config import model_settings
from agent.developer.editing import canonical_plan_path
from agent.developer.runtime import durable_developer_runtime
from agent.evaluation.model_pilot import load_trial, MODEL, REPOSITORY
from agent.evaluation.pilot_tasks import get_pilot_task
from agent.evaluation.pilot_control import cleanup_leases
from agent.evaluation.pilot_spend import CappedDeepSeekTransport
from agent.evaluation.pilot_workflow import PilotRunner, pilot_graph_factory, pilot_verification_spec
from agent.runtime import RunConfig, TokenPricing, RunIdentity, WorkspaceIdentity, StartRequest, semantic_config_digest
from agent.runtime.budget import BudgetErrorCode
from agent.runtime.durable import start_run, run_exit_code
from agent.runtime.revision import detect_agent_code_revision, AgentRevisionStatus
from agent.runtime.secrets import KnownSecretFilter
from agent.runtime.trajectory import JsonlEventSink
from agent.tools.codemap import get_raw_file_content


@contextmanager
def _controlled_environment():
    values = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
              "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "", "PYTHONPATH": "", "PYTHONHOME": "",
              "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false"}
    original = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _docker_ready(image: str) -> bool:
    for argv, expected in ((["docker", "info", "--format", "{{.OSType}}"], "linux"),
                           (["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], image)):
        result = subprocess.run(argv, capture_output=True, timeout=10, shell=False, check=False)
        if result.returncode or len(result.stdout) > 8192:
            return False
        text = result.stdout.decode("utf-8").strip()
        if (text != expected if expected == "linux" else expected not in json.loads(text)):
            return False
    return True


def run_trial(output: str | Path, *, environ=None, transport: httpx.BaseTransport | None = None) -> dict:
    """Consume a fresh trial exactly once. An injected transport is a control."""
    if (Path(output).resolve(strict=True) / "execution.json").exists():
        raise FileExistsError("A trial attempt already exists; resume is not supported.")
    root, plan = load_trial(output)
    selected_task = get_pilot_task(plan["task_id"])
    actual = transport is None
    row = {"kind": "REAL_MODEL_TRIAL_RESULT" if actual else "OFFLINE_MODEL_PROTOCOL_CONTROL_RESULT",
           "task_id": selected_task.task_id, "task_fingerprint": selected_task.fingerprint,
           "plan_digest": plan["plan_digest"], "status": "NOT_STARTED", "scheduled": 1,
           "started": 0, "finished": 0, "resolved": False, "paid_requests": 0}
    # Independent from checkpointing: no later invocation obtains fresh call slots.
    with (root / "execution.json").open("x", encoding="utf-8") as writer:
        json.dump({**row, "status": "CLAIMED", "outcome_unknown": True}, writer)
        writer.flush()
        os.fsync(writer.fileno())
    cap = None
    source = os.environ if environ is None else environ
    secrets = KnownSecretFilter(tuple(value for key, value in source.items() if key.endswith("_API_KEY")))
    try:
        settings = model_settings(source)
        if settings.api_key is None:
            row["not_started_reason"] = "MISSING_CREDENTIAL"
            return row
        if (settings.provider != MODEL["provider"]
                or settings.model not in {MODEL["model"], "deepseek-v4-flash"}
                or (settings.base_url or "").rstrip("/") != MODEL["base_url"]):
            row["not_started_reason"] = "MODEL_CONFIGURATION_MISMATCH"
            return row
        revision = detect_agent_code_revision(REPOSITORY)
        if actual and revision.status is not AgentRevisionStatus.KNOWN:
            row["not_started_reason"] = "AGENT_REVISION_NOT_CLEAN"
            return row
        if actual and datetime.now(timezone.utc).date().isoformat() != plan["model_policy"]["pricing_verified_on"]:
            row["not_started_reason"] = "PRICING_RECHECK_REQUIRED"
            return row
        if not _docker_ready(plan["image"]):
            row["not_started_reason"] = "DOCKER_CONTROL_UNAVAILABLE"
            return row
        settings = replace(settings, model=MODEL["model"])
        policy = plan["model_policy"]
        cap = CappedDeepSeekTransport(transport if transport is not None else httpx.HTTPTransport(retries=0),
                                     root / "spend.jsonl")
        with httpx.Client(transport=cap, follow_redirects=False, trust_env=False) as client, _controlled_environment():
            config = RunConfig(root / "G", root / "runtime", settings, policy["max_output_tokens"],
                (pilot_verification_spec(root / "observer" / "O"),),
                timeout_seconds=plan["timeout_seconds"], max_steps=plan["max_steps"],
                model_request_timeout_seconds=45,
                pricing=TokenPricing(Decimal("0.3"), Decimal("1.2"),
                    "https://api-docs.deepseek.com/quick_start/pricing/"))

            def factory(settings, *, max_output_tokens, request_timeout_seconds, max_retries):
                if max_retries != 0 or max_output_tokens != policy["max_output_tokens"]:
                    raise ValueError("Pilot model limits changed.")
                return ChatDeepSeek(model=settings.model, base_url=settings.base_url, api_key=settings.api_key,
                    max_tokens=max_output_tokens, timeout=request_timeout_seconds, max_retries=0,
                    extra_body={"thinking": {"type": "disabled"}}, http_client=client)

            tools = [get_raw_file_content]
            dependencies = {"model_factory": factory, "research_tools": tools,
                            "load_codebase_structure": lambda: "main.py"}
            architect = durable_architect_runtime(settings, config.model_max_output_tokens, config.pricing, **dependencies)
            extract = architect.extract_implementation_plan

            def scoped_plan(values):
                response = extract(values)
                value = response.value
                if value is not None and (len(value.tasks) != 1 or any(
                    canonical_plan_path(task.file_path) != "main.py" or len(task.atomic_tasks) != 1
                    for task in value.tasks)):
                    return replace(response, value=None, error="Pilot plan exceeds the fixed edit scope.",
                                   error_code=BudgetErrorCode.MODEL_OUTPUT_INVALID)
                return response

            architect = replace(architect, extract_implementation_plan=scoped_plan)
            developer = durable_developer_runtime(settings, config.model_max_output_tokens, config.pricing,
                workspace_root=config.workspace_root, secret_filter=KnownSecretFilter.from_run_config(config), **dependencies)
            observer = root / "observer" / "O"
            runner = PilotRunner(config.workspace_root, observer, config.verification_specs[0],
                {p.name: p.read_bytes() for p in observer.iterdir()}, root / "observer" / "receipt.json",
                plan["image"], runtime_root=config.runtime_root, task_id=selected_task.task_id)
            identity = RunIdentity(uuid.uuid4().hex, uuid.uuid4().hex, selected_task.task_id, WorkspaceIdentity.from_root(config.workspace_root))
            request = StartRequest(identity, semantic_config_digest(config), revision)
            sink = JsonlEventSink(config.runtime_root)
            row.update(status="RUNNING", started=1, run_id=identity.run_id, agent_revision=asdict(revision))
            task = plan["task"] + " Use exactly one file task and one atomic edit; keep all paths workspace-relative."
            result = start_run(config, request, {"implementation_research_scratchpad": [HumanMessage(task)]},
                graph_factory=pilot_graph_factory(runner, identity, sink, architect, developer, research_tools=tools), event_sink=sink)
            row.update(run_summary=asdict(result.summary), record_ref=result.record_ref)
            bound = runner.bind_attempts(result, runtime_root=config.runtime_root)
            final = (root / "G" / "main.py").read_bytes()
            source_hash = hashlib.sha256(final).hexdigest()
            resolved = run_exit_code(result.summary) == 0
            posts = [item for item in bound if item["phase"] == "post"]
            if resolved and (not posts or source_hash != posts[-1]["receipt"]["source_hash"]):
                raise ValueError("Candidate changed after the accepted check.")
            patch = "".join(difflib.unified_diff(selected_task.original.decode().splitlines(keepends=True),
                final.decode("utf-8").splitlines(keepends=True), fromfile="a/main.py", tofile="b/main.py")).encode("utf-8")
            (root / "candidate.patch").write_bytes(patch)
            row.update(status="FINISHED", finished=1, resolved=run_exit_code(result.summary) == 0,
                bound_attempts=bound,
                final_source_hash=source_hash,
                patch={"path": "candidate.patch", "sha256": hashlib.sha256(patch).hexdigest(), "bytes": len(patch)})
    except Exception as error:
        row.update(status="FAILED", finished=int(bool(row["started"])), error_type=type(error).__name__)
        if not actual:
            row["control_error"] = secrets.redact_text(str(error))[:2048]
    except BaseException:
        row.update(status="UNKNOWN", resolved=False, finished=0)
        raise
    finally:
        row["cleanup_complete"] = cleanup_leases(root / "observer" / "leases.json")
        if not row["cleanup_complete"]:
            row.update(status="FAILED", resolved=False)
        if cap is not None:
            row["spend"] = cap.summary()
            row["paid_requests"] = row["spend"]["requests"] if actual else 0
        temporary = root / "execution.final.json"
        temporary.write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
        temporary.replace(root / "execution.json")
    return row
