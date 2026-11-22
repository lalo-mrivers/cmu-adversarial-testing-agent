import json
from pathlib import Path
from typing import Any
from uuid import UUID

from testing_agent.agents.verifier import evidence_references
from testing_agent.persistence.evidence import load_evidence
from testing_agent.persistence.runs import load_run
from testing_agent.persistence.status import (
    TERMINAL_STATES,
    execution_from_summary,
    read_status,
)


def _read(path: Path) -> Any:
    """Read an optional JSON artifact; None when it does not exist."""

    try:
        with path.open(encoding="utf-8-sig") as file:
            return json.load(file)

    except FileNotFoundError:
        return None


def _pretty(text: Any) -> Any:
    """Indent JSON bodies for reading; any other text is returned unchanged."""

    if not isinstance(text, str):
        return text

    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)

    except ValueError:
        return text


def execution_state(
    runs_path: str | Path, run_id: str, *, active_run_id: str | None = None,
) -> dict[str, Any]:
    """Execution state of a run, apart from its business outcome.

    A run that says "running" but is not the one this process executes was
    interrupted: nobody is advancing it. Runs that predate `status.json` are
    classified from their final summary, or as interrupted without one.
    """
    directory = Path(runs_path).resolve() / run_id
    status = read_status(runs_path, run_id)
    outcome = _read(directory / "reports" / "outcome.json")

    if outcome is not None:
        state = execution_from_summary(outcome)

        if status and status.get("execution") in TERMINAL_STATES:
            state = status["execution"]

    elif status and status.get("execution") in TERMINAL_STATES:
        state = status["execution"]

    elif status and status.get("execution") == "running" and run_id == active_run_id:
        state = "running"

    else:
        state = "interrupted"

    return {"state": state, "status": status, "outcome": outcome}


def list_runs(runs_path: str | Path, *, active_run_id: str | None = None) -> list[dict[str, Any]]:
    """Summaries of stored runs, newest first. Directories that are not runs are skipped."""
    root = Path(runs_path).resolve()

    if not root.is_dir():
        return []

    rows: list[dict[str, Any]] = []

    for directory in root.iterdir():
        try:
            run = load_run(root, directory.name)

        except (ValueError, OSError):
            continue

        info = execution_state(root, run["run_id"], active_run_id=active_run_id)
        outcome = info["outcome"] or {}
        rows.append({
            **run,
            "investigation": _read(directory / "investigation.json"),
            "execution": info["state"],
            "case_outcome": outcome.get("case_outcome"),
            "stop_reason": outcome.get("stop_reason") or (info["status"] or {}).get("stop_reason"),
        })

    return sorted(rows, key=lambda row: row["created_at"], reverse=True)


def _calls(trace: list[dict[str, Any]], valid: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Every tool call in order, with the controller's decision and the response, if any."""
    calls: list[dict[str, Any]] = []

    for record_index, record in enumerate(trace):
        decisions = {item["tool_call_id"]: item for item in record.get("decisions", [])}
        results = {
            message["tool_call_id"]: message
            for message in record.get("messages", []) if message.get("type") == "tool"
        }

        for message in record.get("messages", []):
            if message.get("type") != "ai":
                continue

            for call in message.get("tool_calls", []):
                decision = decisions.get(call["id"], {})
                item: dict[str, Any] = {
                    "evidence_id": record["id"],
                    "tool_call_id": call["id"],
                    "record_index": record_index,
                    "tool": call["name"],
                    "arguments": call.get("args", {}),
                    "decision": decision.get("decision"),
                    "reason": decision.get("reason"),
                    "citable": (record["id"], call["id"]) in valid,
                    "status_code": None,
                    "resolved_path": None,
                    "response_body": None,
                    "result_text": None,
                }
                result = results.get(call["id"])

                if result is not None:
                    try:
                        content = json.loads(result.get("content"))

                    except (TypeError, ValueError):
                        content = None

                    if isinstance(content, dict) and isinstance(content.get("response"), dict):
                        response = content["response"]
                        item["status_code"] = response.get("status_code")
                        item["response_body"] = _pretty(response.get("body"))
                        item["resolved_path"] = (content.get("operation") or {}).get("resolved_path")

                    else:
                        item["result_text"] = _pretty(result.get("content"))

                calls.append(item)

        if record.get("stop_reason") and record["stop_reason"] != "model_finished":
            calls.append({"record_index": record_index, "evidence_id": record["id"],
                          "stop_reason": record["stop_reason"], "tool_call_id": None})

    return calls


def load_view(
    runs_path: str | Path, run_id: str, *, active_run_id: str | None = None,
) -> dict[str, Any]:
    """Everything the interface shows about one run, read from its directory."""
    root = Path(runs_path).resolve()
    run = load_run(root, run_id)
    directory = root / run_id
    info = execution_state(root, run_id, active_run_id=active_run_id)

    try:
        trace = load_evidence(root, run_id)

    except (FileNotFoundError, ValueError):
        trace = []

    try:
        valid = evidence_references({"evidence": trace, "specification_version": run["specification_version"]})

    except ValueError:
        valid = set()  # An inconsistent trace cannot support any citation.

    catalog = _read(directory / "requirements.json") or {"requirements": []}
    rules = {rule["id"]: rule for rule in catalog["requirements"]}
    calls = _calls(trace, valid)
    by_pair = {(call["evidence_id"], call["tool_call_id"]): call for call in calls if call["tool_call_id"]}

    outcome = info["outcome"] or {}
    findings = []

    for finding in outcome.get("findings", []):
        references = []

        for reference in finding.get("evidence", []):
            pair = (reference["evidence_id"], reference["tool_call_id"])
            call = by_pair.get(pair)
            references.append({**reference, "call": call, "resolved": bool(call and call["citable"])})

        findings.append({**finding, "requirement": rules.get(finding["requirement_id"]), "references": references})

    continuation = _read(directory / "continuation.json")
    parent = _read(directory / "parent.json")

    return {
        "run": run,
        "investigation": _read(directory / "investigation.json"),
        "execution": info["state"],
        "status": info["status"],
        "case_outcome": outcome.get("case_outcome"),
        "stop_reason": outcome.get("stop_reason") or (info["status"] or {}).get("stop_reason"),
        "evaluation": outcome.get("evaluation"),
        "planning_blocker": outcome.get("planning_blocker"),
        "findings": findings,
        "scope": _read(directory / "scope.json"),
        "experiment": _read(directory / "experiment.json"),
        "execution_context": _read(directory / "execution_context.json"),
        "review": _read(directory / "review.json"),
        "decision": _read(directory / "decision.json"),
        "validation": _read(directory / "validation.json"),
        "parent_run_id": (parent or {}).get("run_id"),
        "continuation_run_id": (continuation or {}).get("run_id"),
        "calls": calls,
        "requirements": rules,
    }


def valid_run_id(value: str) -> bool:
    """True for canonical UUIDs, the only form of run ID the system issues."""

    try:
        return str(UUID(value)) == value

    except ValueError:
        return False
