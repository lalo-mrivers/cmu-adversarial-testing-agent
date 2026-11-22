import json
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from uuid import UUID, uuid4


def create_run(
    runs_path: str | Path,
    objective: str,
    specification_version: str,
) -> dict[str, str]:
    """Creates a run and its directories; returns its metadata.

    The run_id is also used as the thread_id in checkpoints.
    Relative paths are resolved from the execution directory.

    The data is stored in a directory named after the run_id, which contains:
    - run.json: metadata of the run.
    - evidence/: directory for storing evidence files.
    - reports/: directory for storing report files.
    """

    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective must be a non-empty string.")
    
    if not isinstance(specification_version, str) or not specification_version.strip():
        raise ValueError("specification_version must be a non-empty string.")

    run = {
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "objective": objective.strip(),
        "specification_version": specification_version.strip(),
    }

    run_directory = Path(runs_path).resolve() / run["run_id"]
    run_directory.mkdir(parents=True)

    try:
        (run_directory / "evidence").mkdir()
        (run_directory / "reports").mkdir()

        temporary_path = run_directory / "run.json.tmp"
        temporary_path.write_text(
            json.dumps(run, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(run_directory / "run.json")

    except Exception:
        rmtree(run_directory, ignore_errors=True)
        raise

    return run


def load_run(runs_path: str | Path, run_id: str) -> dict[str, str]:
    """Reads an existing run without creating a new identifier."""
    
    if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
        raise ValueError("run_id must be a UUID in canonical format.")

    path = Path(runs_path).resolve() / run_id / "run.json"

    with path.open(encoding="utf-8-sig") as file:
        run = json.load(file)

    required_fields = {"run_id", "created_at", "objective", "specification_version"}

    if (
        not isinstance(run, dict)
        or set(run) != required_fields
        or any(not isinstance(value, str) or not value.strip() for value in run.values())
        or run["run_id"] != run_id
    ):
        raise ValueError("run.json contains invalid metadata or a different run_id.")

    return run