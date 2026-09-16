"""Minimal local CLI for the S3.2 SQLite runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent.runtime.config import load_run_config
from agent.runtime.durable import DurableRunStatus, resume_run, start_run
from agent.runtime.identity import ResumeRequest, RunIdentity, StartRequest, WorkspaceIdentity
from agent.runtime.revision import detect_agent_code_revision
from agent.runtime.semantics import semantic_config_digest


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

    config = load_run_config(dict(os.environ))
    identity = RunIdentity(
        run_id=args.run_id,
        thread_id=args.thread_id,
        task_id=args.task_id,
        workspace=WorkspaceIdentity.from_root(config.workspace_root),
    )
    digest = semantic_config_digest(config)
    revision = detect_agent_code_revision(Path(__file__).resolve().parents[2])
    if args.command == "start":
        result = start_run(
            config,
            StartRequest(identity, digest, revision),
            {
                "implementation_research_scratchpad": [
                    HumanMessage(content=args.task)
                ]
            },
        )
    else:
        result = resume_run(
            config,
            ResumeRequest(identity, digest, revision),
        )
    print(
        json.dumps(
            {
                "status": result.status.value,
                "error_code": result.error_code,
                "message": result.message,
                "warnings": [
                    warning.code.value
                    for warning in (
                        result.preflight.warnings if result.preflight else ()
                    )
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.status in {
        DurableRunStatus.COMPLETED,
        DurableRunStatus.PAUSED,
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
