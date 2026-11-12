"""Prepare, check or run one request-capped DeepSeek trial."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.evaluation.model_pilot import prepare_trial, execute_trial
from agent.evaluation.pilot_tasks import PILOT_TASK_IDS
from dotenv import dotenv_values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", choices=PILOT_TASK_IDS,
                        help="Select a fixed task during preparation only; execution uses the frozen plan")
    parser.add_argument("--env-file", type=Path,
                        help="Read configuration from this existing file; process environment takes precedence")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", "--execute", dest="check", action="store_true",
                      help="Check only; consumes this prepared trial without paid dispatch")
    mode.add_argument("--run", action="store_true",
                      help="Run one fresh trial through the fixed request cap and Docker observer")
    args = parser.parse_args(argv)
    try:
        if args.task is not None and (args.run or args.check):
            raise ValueError("Task selection is only allowed when preparing a fresh trial.")
        # Explicit local files are only configuration data, never model input.
        config = args.env_file if args.env_file is not None else Path(".env.local")
        if args.env_file is not None and not config.is_file():
            raise FileNotFoundError("Explicit configuration file does not exist.")
        environment = {key: value for key, value in {**dotenv_values(config, interpolate=False), **os.environ}.items()
                       if value is not None}
        if args.run:
            from agent.evaluation.pilot_execution import run_trial
            result = run_trial(args.output, environ=environment)
        else:
            result = (execute_trial(args.output, environ=environment) if args.check
                      else prepare_trial(args.output, task_id=args.task or "json-sum"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "REJECTED", "reason": type(error).__name__}))
        return 2
    visible = {key: result[key] for key in (
        "status", "scheduled", "started", "finished", "model", "budget", "not_started_reason",
        "hard_budget_status", "task_id", "resolved", "paid_requests", "spend", "run_id",
        "cleanup_complete", "error_type", "record_ref") if key in result}
    print(json.dumps(visible, ensure_ascii=False))
    if result["status"] == "PREPARED" or result.get("resolved") is True:
        return 0
    return 1 if result["status"] == "FINISHED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
