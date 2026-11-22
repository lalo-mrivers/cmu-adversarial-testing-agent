import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import UUID

from testing_agent.persistence.runs import load_run
from testing_agent.persistence.redaction import redact


def _validate_evidence(
    evidence: list[dict[str, Any]],
    specification_version: str,
) -> None:
    """Checks for unique identifiers and the run's specification version."""

    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list of records.")

    seen_ids: set[str] = set()

    for record in evidence:
        if not isinstance(record, dict):
            raise ValueError("Each evidence record must be a JSON object.")

        record_id = record.get("id")
        
        if not isinstance(record_id, str) or str(UUID(record_id)) != record_id:
            raise ValueError("Each record must have a UUID id in canonical format.")
        
        if record_id in seen_ids:
            raise ValueError(f"Duplicate evidence identifier: {record_id}.")
        
        if record.get("specification_version") != specification_version:
            raise ValueError("The evidence belongs to a different specification version.")

        seen_ids.add(record_id)


def save_evidence(
    runs_path: str | Path,
    run_id: str,
    evidence: list[dict[str, Any]],
) -> Path:
    """Exports the complete accumulated list of AgentState.evidence.
    
    Replaces the previous snapshot after writing the temporary file,
    then `evidence` should contins all cumulated records.
    Preserves the identifiers and content of the received records.
    """
    run = load_run(runs_path, run_id)
    _validate_evidence(evidence, run["specification_version"])
    evidence = redact(evidence)
    content = json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n"

    path = Path(runs_path).resolve() / run_id / "evidence" / "trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        previous = load_evidence(runs_path, run_id)

        if evidence[:len(previous)] != previous:
            raise ValueError("Previously exported evidence cannot be removed or revised.")

    # The temporary file is closed before writing and replacing it, also on Windows.
    with NamedTemporaryFile(
        dir=path.parent, prefix="trace-", suffix=".tmp", delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)

    try:
        temporary_path.write_text(content, encoding="utf-8")
        temporary_path.replace(path)

    finally:
        temporary_path.unlink(missing_ok=True)

    return path


def load_evidence(runs_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    """Reads the last exported snapshot and validates its correspondence with the run."""
    run = load_run(runs_path, run_id)
    path = Path(runs_path).resolve() / run_id / "evidence" / "trace.json"

    with path.open(encoding="utf-8-sig") as file:
        evidence = json.load(file)

    _validate_evidence(evidence, run["specification_version"])

    return evidence
