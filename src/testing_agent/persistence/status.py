import json
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from testing_agent.persistence.redaction import redact
from testing_agent.persistence.runs import load_run


# running: the engine is working on it.
# closed: finished, including limits reached; the business outcome is separate.
# stopped: closed after a user request, with no operation of uncertain effect.
# pending_review: paused for a human decision (interpretation, scope or recovery).
# failed: the engine raised an error. interrupted: no terminal record was written.
ExecutionState = Literal[
    "running", "closed", "stopped", "pending_review", "failed", "interrupted",
]
TERMINAL_STATES = frozenset({"closed", "stopped", "pending_review", "failed"})

# Milestones are confirmed events; they are never inferred from a plan.
MILESTONES = {
    "run_started": "Investigation started",
    "target_ready": "Target ready",
    "plan_ready": "Plan ready",
    "plan_blocked": "Plan blocked",
    "execution_recorded": "Execution recorded",
    "evaluation_done": "Evaluation finished",
    "stop_requested": "Stop requested",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def replace_file(source: Path, target: Path, *, attempts: int = 20) -> None:
    """Atomically replace `target`, waiting out the short locks Windows places on open files.

    A page reading the file, an editor or a cloud-sync client can hold it for a moment, and
    `replace` then raises PermissionError. Retrying with a short backoff avoids losing a status
    update (or a run's final state) to such a lock.
    """

    for attempt in range(attempts):
        try:
            source.replace(target)

            return

        except PermissionError:
            if attempt == attempts - 1:
                raise

            time.sleep(0.05 * (attempt + 1))


def status_path(runs_path: str | Path, run_id: str) -> Path:
    return Path(runs_path).resolve() / run_id / "status.json"


class StatusWriter:
    """Keeps the status of one run and replaces its file atomically."""

    def __init__(self, runs_path: str | Path, run_id: str) -> None:
        load_run(runs_path, run_id)
        self.path = status_path(runs_path, run_id)
        self.state: dict[str, Any] = {
            "run_id": run_id,
            "execution": "running",
            "milestones": [],
            "iteration": 0,
            "evidence_records": 0,
            "stop_requested": False,
            "stop_reason": None,
            "error": None,
            "updated_at": _now(),
        }
        self._write()

    def milestone(self, event: str, detail: str | None = None) -> None:
        if event not in MILESTONES:
            raise ValueError(f"Unknown milestone: {event!r}.")

        self.state["milestones"].append({"event": event, "at": _now(), "detail": detail})
        self._write()

    def update(self, **fields: Any) -> None:
        unknown = fields.keys() - self.state.keys()

        if unknown:
            raise ValueError(f"Unknown status fields: {sorted(unknown)}.")

        self.state.update(fields)
        self._write()

    def finish(self, execution: ExecutionState, *, stop_reason: str | None = None,
               error: str | None = None) -> None:
        if execution not in TERMINAL_STATES:
            raise ValueError(f"{execution!r} is not a terminal execution state.")

        self.update(execution=execution, stop_reason=stop_reason, error=error)

    def _write(self) -> None:
        self.state["updated_at"] = _now()
        content = json.dumps(redact(self.state), ensure_ascii=False, indent=2, allow_nan=False) + "\n"

        with NamedTemporaryFile(dir=self.path.parent, prefix="status-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)

        try:
            temporary.write_text(content, encoding="utf-8")
            replace_file(temporary, self.path)

        finally:
            temporary.unlink(missing_ok=True)


def read_status(runs_path: str | Path, run_id: str) -> dict[str, Any] | None:
    """Return the last status written, or None for runs that predate it."""
    path = status_path(runs_path, run_id)

    try:
        with path.open(encoding="utf-8-sig") as file:
            value = json.load(file)

    except FileNotFoundError:
        return None

    except json.JSONDecodeError:
        # A reader can only see this if the file was damaged outside the writer.
        return None

    return value if isinstance(value, dict) else None


def execution_from_summary(summary: dict[str, Any]) -> ExecutionState:
    """Map the engine's final summary to an execution state."""

    if summary.get("status") == "paused":
        return "pending_review"

    if summary.get("stop_reason") == "user_stopped":
        return "stopped"

    return "closed"
