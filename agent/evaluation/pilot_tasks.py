"""Three author-reviewed integer JSON tasks; no caller-supplied oracle code."""

from dataclasses import dataclass
import hashlib
import json


def _program(expression: str) -> bytes:
    return ('import json,sys\nvalues=json.load(sys.stdin)["values"]\n'
            'print(json.dumps({"total":' + expression + '}))\n').encode("utf-8")


@dataclass(frozen=True)
class PilotTask:
    task_id: str
    instruction: str
    original: bytes
    gold: bytes
    cases: tuple[tuple[tuple[int, ...], int], ...]

    @property
    def case_literal(self) -> str:
        return repr(tuple((list(values), expected) for values, expected in self.cases))

    @property
    def fingerprint(self) -> str:
        value = {"id": self.task_id, "instruction": self.instruction,
                 "original": self.original.hex(), "gold": self.gold.hex(), "cases": self.cases}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_TASKS = (
    PilotTask("json-sum", "Read JSON containing an integer list 'values'; output exactly JSON {'total': sum of every integer}. Modify only main.py.",
              _program("sum(values[:-1])"), _program("sum(values)"),
              (((), 0), ((1, 2, 3), 6), ((-2, 5, 7), 10))),
    PilotTask("json-sum-squares", "Read JSON containing an integer list 'values'; output exactly JSON with one integer 'total', the sum of the square of each integer. Modify only main.py.",
              _program("sum(values)"), _program("sum(value * value for value in values)"),
              (((), 0), ((1, 2, 3), 14), ((-2, 5, 7), 78))),
    PilotTask("json-weighted-sum", "Read JSON containing an integer list 'values'; output exactly JSON with one integer 'total', the sum of each value multiplied by its one-based position. Modify only main.py.",
              _program("sum(values)"), _program("sum(index * value for index, value in enumerate(values, 1))"),
              (((), 0), ((1, 2, 3), 14), ((-2, 5, 7), 29))),
)
PILOT_TASK_IDS = tuple(task.task_id for task in _TASKS)


def get_pilot_task(task_id: str) -> PilotTask:
    """Reject all identifiers outside the closed, reviewed task set."""
    for task in _TASKS:
        if type(task_id) is str and task.task_id == task_id:
            return task
    raise ValueError("Unknown fixed pilot task.")
