"""Minimal local CLI for the S3.2 SQLite runtime."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent.runtime.config import RunConfigError, load_run_config
from agent.runtime.durable import (
    DurableRunStatus,
    RunSummary,
    resume_run,
    run_exit_code,
    start_run,
)
from agent.runtime.identity import ResumeRequest, RunIdentity, StartRequest, WorkspaceIdentity
from agent.runtime.revision import detect_agent_code_revision
from agent.runtime.semantics import semantic_config_digest
from agent.workspace import WorkspaceRootError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local durable SWE Agent runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "resume"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--run-id", required=True)
        subparser.add_argument("--thread-id", required=True)
        subparser.add_argument("--task-id", required=True)
        if command == "start":
            subparser.add_argument("--task", required=True)
    args = parser.parse_args(argv)

    try:
        config = load_run_config(dict(os.environ))
    except RunConfigError as error:
        return _emit_summary(
            RunSummary(
                schema_version=1,
                runtime_status=DurableRunStatus.REJECTED,
                workflow_outcome=None,
                verification_status=None,
                error_code=error.code.value,
                run_id=args.run_id,
            )
        )

    try:
        identity = RunIdentity(
            run_id=args.run_id,
            thread_id=args.thread_id,
            task_id=args.task_id,
            workspace=WorkspaceIdentity.from_root(config.workspace_root),
        )
    except WorkspaceRootError:
        return _emit_summary(
            _entry_error(args.run_id, DurableRunStatus.REJECTED, "workspace_error")
        )
    except ValueError:
        return _emit_summary(
            _entry_error(args.run_id, DurableRunStatus.REJECTED, "invalid_request")
        )

    digest = semantic_config_digest(config)
    revision = detect_agent_code_revision(Path(__file__).resolve().parents[2])
    try:
        if args.command == "start":
            request = StartRequest(identity, digest, revision)
        else:
            request = ResumeRequest(identity, digest, revision)
    except ValueError:
        return _emit_summary(
            _entry_error(args.run_id, DurableRunStatus.REJECTED, "invalid_request")
        )

    try:
        if args.command == "start":
            result = start_run(
                config,
                request,
                {
                    "implementation_research_scratchpad": [
                        HumanMessage(content=args.task)
                    ]
                },
            )
        else:
            result = resume_run(config, request)
    except sqlite3.Error:
        return _emit_summary(
            _entry_error(args.run_id, DurableRunStatus.FAILED, "sqlite_error")
        )
    except OSError:
        return _emit_summary(
            _entry_error(
                args.run_id, DurableRunStatus.FAILED, "runtime_io_error"
            )
        )
    except WorkspaceRootError:
        return _emit_summary(
            _entry_error(args.run_id, DurableRunStatus.REJECTED, "workspace_error")
        )
    except RunConfigError as error:
        return _emit_summary(
            RunSummary(
                schema_version=1,
                runtime_status=DurableRunStatus.REJECTED,
                workflow_outcome=None,
                verification_status=None,
                error_code=error.code.value,
                run_id=args.run_id,
            )
        )

    return _emit_summary(result.summary)


def _entry_error(
    run_id: str,
    status: DurableRunStatus,
    error_code: str,
) -> RunSummary:
    return RunSummary(
        schema_version=1,
        runtime_status=status,
        workflow_outcome=None,
        verification_status=None,
        error_code=error_code,
        run_id=run_id,
    )


def _emit_summary(summary: RunSummary) -> int:
    print(json.dumps(summary.to_dict(), ensure_ascii=False))
    return run_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
