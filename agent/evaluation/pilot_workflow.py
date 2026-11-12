"""One-file isolated pilot assembly, reusing the production graph and SQLite.

This is a research adapter for the concrete Docker observer in pilot_control,
not a generic trust switch or a replacement runtime/checkpoint protocol.
"""

from dataclasses import asdict, replace
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.runtime.durable import DurableRunResult

from agent.evaluation import pilot_control
from agent.evaluation.pilot_control import IMAGE_PATTERN, MAX_SOURCE, ORACLE_SOURCE, PYTEST_CONFIG_BYTES, cleanup_leases
from agent.evaluation.pilot_control import oracle_source_for
from agent.verification.contracts import (
    VerificationCheckStatus, VerificationResult, VerificationSummary,
    VerificationSpec,
)
from agent.verification.runner import VerificationRunner
from agent.workspace import WorkspacePathResolver, has_single_regular_file_link


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def pilot_verification_spec(observer_root: str | Path, *, timeout_seconds: float = 90.0) -> VerificationSpec:
    root = Path(observer_root).resolve(strict=True)
    return VerificationSpec("sum-contract", (
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--assert=plain", "--noconftest", f"--confcutdir={root}", "-c", str(root / "pytest.ini"),
        f"--rootdir={root}", str(root / "test_oracle.py"), "--junitxml=report.xml",
    ), timeout_seconds=timeout_seconds, report_path="report.xml")


def _root_files(root: Path, limit: int) -> list[str]:
    """The pilot has a flat inventory; never traverse an unexpected subtree."""
    names = []
    with os.scandir(root) as entries:
        for entry in entries:
            if len(names) >= limit or not entry.is_file(follow_symlinks=False):
                raise ValueError("Pilot inventory is not a bounded flat directory.")
            names.append(entry.name)
    return sorted(names)


class PilotRunner:
    """Execute truthful pytest specs in O; candidate main.py stays in G.

    observer_files are the exact, author-reviewed build_observer output bytes.
    Its runtime uses the real Docker transport, never imports candidate code,
    and writes a unique trusted receipt outside both roots.
    """

    def __init__(self, generation_root: str | Path, observer_root: str | Path,
                 spec: VerificationSpec, observer_files: dict[str, bytes],
                 receipt_path: str | Path, image: str, *,
                 runtime_root: str | Path | None = None, task_id: str = "json-sum") -> None:
        self.generation_root = Path(generation_root).resolve(strict=True)
        self.execution_root = Path(observer_root).resolve(strict=True)
        self.receipt_path = Path(receipt_path).absolute()
        self.control_path = self.receipt_path.parent / "control.json"
        self.lease_path = self.receipt_path.parent / "leases.json"
        self.runtime_root = Path(runtime_root or self.receipt_path.parent / "private-runtime").resolve()
        roots = (self.generation_root, self.execution_root)
        if (roots[0] == roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents
                or any(self.receipt_path == r or r in self.receipt_path.parents for r in roots)
                or not IMAGE_PATTERN.fullmatch(image)):
            raise ValueError("Pilot generation, observer and receipts must be separate.")
        self.spec, self.image, self.task_id = spec, image, task_id
        if (not 2 <= spec.timeout_seconds <= 120
                or spec != pilot_verification_spec(self.execution_root, timeout_seconds=spec.timeout_seconds)
                or os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1"
                or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
                or os.environ.get("PYTEST_ADDOPTS") or os.environ.get("PYTEST_PLUGINS")
                or os.environ.get("PYTHONPATH") or os.environ.get("PYTHONHOME")):
            raise ValueError("Pilot requires the fixed observer command and controlled pytest environment.")
        self._observer_files = dict(observer_files)
        fixed = {
            "control_runtime.py": Path(pilot_control.__file__).read_bytes(),
            "test_oracle.py": oracle_source_for(task_id).encode("utf-8"),
            "pytest.ini": PYTEST_CONFIG_BYTES,
        }
        if (set(self._observer_files) != {*fixed, "config.json"}
                or any(self._observer_files[name] != data for name, data in fixed.items())
                or json.loads(self._observer_files["config.json"]) != {
                    "source_path": str(self.generation_root / "main.py"),
                    "image": image, "task_id": task_id, "receipt_path": str(self.receipt_path),
                    "control_path": str(self.control_path), "lease_path": str(self.lease_path),
                }):
            raise ValueError("Pilot observer is not the fixed reviewed implementation.")
        self._environment = _digest(sorted(os.environ.items()))
        if any(self.runtime_root == root or root in self.runtime_root.parents
               or self.runtime_root in root.parents for root in roots):
            raise ValueError("Pilot runtime must be separate from generation and observer.")
        self._runner = VerificationRunner(self.execution_root, runtime_root=self.runtime_root)
        self._fixed_boundary = (self.generation_root, self.execution_root, self.receipt_path, self.control_path, self.lease_path, self.image, self.runtime_root, self.task_id)
        self._fixed_runner = self._runner
        self._fixed_run = self.run
        self.receipts = []

    def _source(self) -> bytes:
        resolver = WorkspacePathResolver(self.generation_root)
        if _root_files(self.generation_root, 1) != ["main.py"]:
            raise ValueError("Pilot generation must contain only main.py.")
        source = resolver.resolve_file("main.py")
        if not source.ok or not has_single_regular_file_link(source.path):
            raise ValueError("Pilot source path is unsafe.")
        with source.path.open("rb") as stream:
            data = stream.read(MAX_SOURCE + 1)
        if len(data) > MAX_SOURCE:
            raise ValueError("Pilot source exceeds its bound.")
        data.decode("utf-8")
        return data

    def permits(self, spec: VerificationSpec, workspace_root: str | Path) -> bool:
        try:
            if (Path(workspace_root).resolve(strict=True) != self.generation_root
                    or (self.generation_root, self.execution_root, self.receipt_path, self.control_path, self.lease_path, self.image, self.runtime_root, self.task_id) != self._fixed_boundary
                    or self._runner is not self._fixed_runner or type(self._runner) is not VerificationRunner
                    or self._runner.execution_root != self.execution_root or self.run != self._fixed_run
                    or _digest(sorted(os.environ.items())) != self._environment
                    or not 0 < spec.timeout_seconds <= self.spec.timeout_seconds
                    or replace(spec, timeout_seconds=self.spec.timeout_seconds) != self.spec
                    or not self._observer_files or len(self._observer_files) > 8):
                return False
            resolver = WorkspacePathResolver(self.execution_root)
            actual = _root_files(self.execution_root, 4)
            if actual != sorted(self._observer_files):
                return False
            total = 0
            for name, expected in self._observer_files.items():
                resolved = resolver.resolve_file(name)
                if not resolved.ok or not has_single_regular_file_link(resolved.path):
                    return False
                total += len(expected)
                if total > 256 * 1024 or resolved.path.stat().st_size != len(expected):
                    return False
                if resolved.path.read_bytes() != expected:
                    return False
            self._source()
            return True
        except (OSError, ValueError, TypeError):
            return False

    def run(self, spec: VerificationSpec) -> VerificationResult:
        started = time.monotonic()
        deadline = started + spec.timeout_seconds
        if not self.permits(spec, self.generation_root):
            return self._error(spec, "Pilot inputs changed before dispatch.")
        before = self._source()
        source_hash = hashlib.sha256(before).hexdigest()
        try:
            if any(path.is_symlink() for path in (self.receipt_path, self.control_path, self.lease_path)):
                raise ValueError("Receipt path is unsafe.")
            self.receipt_path.unlink(missing_ok=True)
            reserve = min(8.0, spec.timeout_seconds / 3)
            execution_deadline = deadline - reserve
            self.control_path.write_text(json.dumps({"deadline": execution_deadline}), encoding="utf-8")
            self.lease_path.write_text("[]", encoding="utf-8")
            remaining = execution_deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Pilot preparation exhausted the effective deadline.")
            try:
                result = self._runner.run(replace(spec, timeout_seconds=remaining))
            finally:
                cleaned = cleanup_leases(self.lease_path, timeout_seconds=max(0.01, deadline - time.monotonic()))
            if not cleaned:
                raise ValueError("Pilot parent cleanup was not confirmed.")
            if (not self.receipt_path.is_file()
                    or not has_single_regular_file_link(self.receipt_path)
                    or self.receipt_path.stat().st_size > 128 * 1024):
                raise ValueError("Trusted observer receipt is absent or oversized.")
            receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
            receipt_id = receipt["receipt_id"]
            if (not isinstance(receipt_id, str) or len(receipt_id) != 32
                    or any(r["receipt_id"] == receipt_id for r in self.receipts)):
                raise ValueError("Pilot receipt identity is missing or duplicated.")
            if receipt["cleanup_complete"] is not True:
                raise ValueError("Pilot cleanup was not confirmed; inspect the private receipt.")
            if receipt.get("status") != "completed":
                raise ValueError("Pilot transport did not complete; functional evidence is unavailable.")
            if receipt["source_hash"] != source_hash or receipt["image"] != self.image:
                raise ValueError("Pilot source or image receipt identity mismatch.")
            if self._source() != before or not self.permits(spec, self.generation_root):
                raise ValueError("Pilot inputs changed during verification.")
            result = replace(result, stdout="Trusted isolated observer completed; inspect structured cases.",
                             stderr="", message=f"pilot_receipt={receipt_id}")
            receipt["result_digest"] = _digest(asdict(VerificationSummary.from_result(result)))
            self.receipts.append(receipt)
            return result
        except (OSError, ValueError, KeyError, TypeError) as error:
            return self._error(spec, str(error))

    @staticmethod
    def _error(spec, message):
        return VerificationResult.create(
            name=spec.name, argv=spec.argv, cwd=spec.cwd,
            status=VerificationCheckStatus.EXECUTION_ERROR, exit_code=None,
            message=message, allowed_failure_case_ids=spec.allowed_failure_case_ids,
        )

    def bind_attempts(self, result: "DurableRunResult", *, runtime_root: str | Path) -> list[dict]:
        """Associate original SQLite results, never infer phase from call order."""
        from agent.runtime.identity import RunIdentity
        from agent.runtime.trajectory import RunRecord, AuditStatus, record_ref_for

        state = result.state or {}
        if Path(runtime_root).resolve(strict=True) != self.runtime_root:
            raise ValueError("Pilot records and artifacts must share the configured runtime root.")
        identity = state.get("run_identity")
        if (not isinstance(identity, RunIdentity) or identity.run_id != result.run_id
                or identity.workspace.canonical_root != str(self.generation_root)
                or result.audit_incomplete or not result.record_ref
                or result.record_ref != record_ref_for(run_id=identity.run_id,
                    task_id=identity.task_id, thread_id=identity.thread_id)):
            raise ValueError("Pilot run identity or audit reference is inconsistent.")
        resolved = WorkspacePathResolver(runtime_root).resolve_file(result.record_ref)
        if (not resolved.ok or not has_single_regular_file_link(resolved.path)
                or resolved.path.stat().st_size > 256 * 1024):
            raise ValueError("Pilot original RunRecord is unavailable.")
        record = RunRecord.from_dict(json.loads(resolved.path.read_text(encoding="utf-8")))
        if (record.identity != {"run_id": identity.run_id, "thread_id": identity.thread_id,
                "task_id": identity.task_id, "workspace": {"canonical_root": identity.workspace.canonical_root,
                "root_digest": identity.workspace.root_digest}}
                or record.audit_status is not AuditStatus.COMPLETE
                or record.runtime_status != result.status.value):
            raise ValueError("Pilot RunRecord does not belong to the same run/task/thread.")
        attempts = state.get("verification_attempts", ())
        if any(a.run_id != identity.run_id or a.task_id != identity.task_id for a in attempts):
            raise ValueError("Pilot verification attempts belong to another identity.")
        bound = []
        for receipt in self.receipts:
            token = "pilot_receipt=" + receipt["receipt_id"]
            matches = [a for a in attempts if a.result is not None and a.run_id == result.run_id
                       and token in a.result.message.splitlines()]
            if len(matches) != 1 or _digest(asdict(matches[0].result)) != receipt["result_digest"]:
                raise ValueError("Pilot receipt does not bind exactly one original attempt.")
            attempt = matches[0]
            bound.append({"receipt": receipt, "attempt_id": attempt.attempt_id,
                          "phase": attempt.phase, "run_id": result.run_id,
                          "task_id": attempt.task_id, "repair_attempt": attempt.repair_attempt,
                          "record_ref": result.record_ref})
        if len(bound) != len(attempts) or result.audit_incomplete or not result.record_ref:
            raise ValueError("Pilot audit is incomplete.")
        if state.get("acceptance") is not None and state["acceptance"].accepted:
            post = [item for item in bound if item["phase"] == "post"]
            final = max(post, key=lambda item: item["repair_attempt"]) if post else None
            if final is None or hashlib.sha256(self._source()).hexdigest() != final["receipt"]["source_hash"]:
                raise ValueError("Final source differs from the last valid post check.")
        return bound


class PilotAdmission:
    """Admission only for this actual isolated adapter, with two honest roots."""

    def __init__(self, runner):
        if type(runner) is not PilotRunner:
            raise ValueError("Only the concrete pilot runner is supported.")
        self.runner = runner

    def permits(self, spec, runner, workspace_root):
        return runner is self.runner and type(runner) is PilotRunner and runner.permits(spec, workspace_root)


def pilot_graph_factory(runner, identity, event_sink, architect_runtime, developer_runtime,
                        *, research_tools=()):
    """Close over one sink/identity and inject runtimes into the existing graph.

    Callers use start_run(..., graph_factory=factory, event_sink=the_same_sink).
    The pilot has no resume API; core recovery semantics remain unchanged.
    Real runtimes must explicitly supply their allowed read tools; the default
    preserves the original no-tool authored controls.
    """
    from agent.architect.graph import create_architect_workflow
    from agent.developer.graph import create_developer_workflow
    from agent.graph import create_workflow_graph
    from agent.runtime.boundary import DurableBudgetBoundary
    from agent.runtime.secrets import KnownSecretFilter
    from agent.runtime.trajectory import EventRecorder

    tools = tuple(research_tools)

    def factory(config, run_id, saver, clock):
        if (run_id != identity.run_id or config.workspace_root != runner.generation_root
                or config.runtime_root != runner.runtime_root
                or tuple(config.verification_specs) != (runner.spec,)):
            raise ValueError("Pilot graph identity/configuration mismatch.")
        boundary = DurableBudgetBoundary(run_id, clock=clock,
                                        model_request_timeout_seconds=config.model_request_timeout_seconds)
        secrets = KnownSecretFilter.from_run_config(config)
        recorder = EventRecorder(event_sink, run_id=run_id, thread_id=identity.thread_id,
                                 task_id=identity.task_id, secret_filter=secrets, clock=clock)
        architect = create_architect_workflow(
            architect_runtime, research_tools=list(tools), budget_boundary=boundary,
            secret_filter=secrets, event_recorder=recorder, access_policy=config.access_policy)
        developer = create_developer_workflow(
            developer_runtime, research_tools=list(tools), budget_boundary=boundary,
            secret_filter=secrets, event_recorder=recorder, access_policy=config.access_policy)
        return create_workflow_graph(
            architect=architect, developer=developer, verification_specs=config.verification_specs,
            verification_runner=runner, workspace_root=config.workspace_root,
            runtime_root=config.runtime_root, durable_runtime=True, clock=clock,
            secret_filter=secrets, event_recorder=recorder, access_policy=config.access_policy,
            reviewed_inputs=PilotAdmission(runner),
        ).compile(checkpointer=saver)
    return factory
