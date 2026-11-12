"""Four fixed S1 scenarios, with real results and owned temporary workspaces.

Importing this module uses only the standard library. Calling run_demo loads
the normal S1 package; review its transitive imports before execution.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import TextIO


_INITIAL = "label=初始\ncount=1\n".encode("utf-8")
_FINAL = "label=完成\ncount=2\n".encode("utf-8")
_AMBIGUOUS = "value=重复\nvalue=重复\n".encode("utf-8")
_SCENARIOS = ("two_stage_commit", "stale_hash", "ambiguous_match", "net_zero")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class ScenarioEvidence:
    name: str
    expected_status: str
    expected_error_code: str | None
    actual_status: str
    actual_error_code: str | None
    content_before: bytes
    content_after: bytes
    result_before_hash: str | None
    result_after_hash: str | None
    task_ids: tuple[str, ...]
    checks: dict[str, bool]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.name,
            "expected_status": self.expected_status,
            "expected_error_code": self.expected_error_code,
            "actual_status": self.actual_status,
            "actual_error_code": self.actual_error_code,
            "observed_before_sha256": _sha(self.content_before),
            "observed_after_sha256": _sha(self.content_after),
            "result_before_hash": self.result_before_hash,
            "result_after_hash": self.result_after_hash,
            "actual_bytes_hex": self.content_after.hex(),
            "task_ids": list(self.task_ids),
            "checks": self.checks,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class DemoReport:
    scenarios: tuple[ScenarioEvidence, ...]
    cleanup_complete: bool
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            tuple(item.name for item in self.scenarios) == _SCENARIOS
            and all(item.passed for item in self.scenarios)
            and self.cleanup_complete
            and not self.errors
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "cleanup_complete": self.cleanup_complete,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "errors": list(self.errors),
        }


def _exercise(root: Path, name: str) -> ScenarioEvidence:
    # Deliberately use the normal, real public interface, without module stubs.
    from agent.editing import EditOperation, EditProposal, WorkspaceEditor

    root.mkdir()
    target = root / "sample.txt"
    original = _AMBIGUOUS if name == "ambiguous_match" else _INITIAL
    target.write_bytes(original)
    editor = WorkspaceEditor(root)
    before_hash = _sha(original)
    expected_error = None
    staged_unchanged = True
    completed_stages = 0

    if name in {"stale_hash", "ambiguous_match"}:
        stale = name == "stale_hash"
        task_ids = ("reject.stale" if stale else "reject.ambiguous",)
        result = editor.apply(
            EditProposal(
                task_id=task_ids[0],
                path="sample.txt",
                operation=EditOperation.EDIT,
                base_hash=_sha("label=初始\ncount=0\n".encode("utf-8")) if stale else before_hash,
                old_text="count=1" if stale else "value=重复",
                new_text="count=2" if stale else "value=唯一",
            )
        )
        expected_status = "REJECTED"
        expected_error = "HASH_MISMATCH" if stale else "MATCH_AMBIGUOUS"
        expected_bytes = original
    else:
        changing = name == "two_stage_commit"
        steps = (
            (("commit.label", "label=初始", "label=完成"),
             ("commit.count", "count=1", "count=2"))
            if changing else
            (("noop.change", "count=1", "count=2"),
             ("noop.restore", "count=2", "count=1"))
        )
        task_ids = tuple(step[0] for step in steps)
        started = editor.begin("sample.txt")
        if not started.ok or started.transaction is None:
            raise RuntimeError("begin_failed")
        transaction = started.transaction
        for task_id, old_text, new_text in steps:
            staged = editor.stage(
                transaction,
                EditProposal(
                    task_id=task_id,
                    path="sample.txt",
                    operation=EditOperation.EDIT,
                    base_hash=before_hash,
                    old_text=old_text,
                    new_text=new_text,
                ),
            )
            staged_unchanged = staged_unchanged and target.read_bytes() == original
            if not staged.ok or staged.transaction is None:
                if staged.edit_result is None:
                    raise RuntimeError("stage_has_no_result")
                result = staged.edit_result
                break
            transaction = staged.transaction
            completed_stages += 1
        else:
            result = editor.commit(transaction)
        expected_status = "APPLIED" if changing else "NOOP"
        expected_bytes = _FINAL if changing else original

    actual = target.read_bytes()
    actual_error = result.error_code.name if result.error_code is not None else None
    checks = {
        "status_matches": result.status.name == expected_status,
        "error_matches": actual_error == expected_error,
        "bytes_match": actual == expected_bytes,
        "before_hash_matches": result.before_hash == before_hash,
        "after_hash_matches": result.after_hash == (
            None if expected_status == "REJECTED" else _sha(actual)
        ),
        "task_ids_match": result.task_ids == task_ids,
        "no_leftover_write_files": sorted(p.name for p in root.iterdir()) == ["sample.txt"],
    }
    if name in {"two_stage_commit", "net_zero"}:
        checks["staging_kept_original_bytes"] = staged_unchanged
        checks["two_stages_completed"] = completed_stages == 2
    return ScenarioEvidence(
        name, expected_status, expected_error, result.status.name, actual_error,
        original, actual, result.before_hash, result.after_hash, result.task_ids, checks,
    )


def run_demo(*, temporary_parent: Path | None = None) -> DemoReport:
    """Run fixed fixtures only inside a new, owned TemporaryDirectory.

    temporary_parent is a test-fixture boundary: existing files there are never
    edited. The returned report retains observed bytes, never temporary paths.
    """
    evidence: list[ScenarioEvidence] = []
    errors: list[str] = []
    try:
        directory = tempfile.TemporaryDirectory(prefix="s1-editor-demo-", dir=temporary_parent)
    except Exception as error:
        return DemoReport((), False, (f"temporary_workspace:{type(error).__name__}",))

    root = Path(directory.name)
    cleanup_complete = False
    try:
        for name in _SCENARIOS:
            try:
                evidence.append(_exercise(root / name, name))
            except Exception as error:
                # Exception text/tracebacks can contain private paths or data.
                errors.append(f"{name}:{type(error).__name__}")
                break
    finally:
        try:
            directory.cleanup()
            cleanup_complete = not root.exists()
            if not cleanup_complete:
                errors.append("cleanup:WorkspaceStillExists")
        except Exception as error:
            errors.append(f"cleanup:{type(error).__name__}")
    return DemoReport(tuple(evidence), cleanup_complete, tuple(errors))


def main(*, output: TextIO | None = None) -> int:
    """Print actual evidence as path-free ASCII JSON; any failed check exits 1."""
    report = run_demo()
    print(json.dumps(report.to_dict(), ensure_ascii=True, sort_keys=True), file=output or sys.stdout)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
