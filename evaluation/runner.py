import asyncio
import json
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError

from evaluation.cases import MODES, ROOT, SEED, operation_pairs, variant_catalog
from evaluation.controlled import ScriptedExecutor, ScriptedPlanner, ScriptedVerifier
from evaluation.trace import calls_in
from testing_agent.agents.verifier import create_verifier
from testing_agent.integrations.llm.factory import create_model
from testing_agent.integrations.mcp.client import open_mcp_tools
from testing_agent.persistence.redaction import redact
from testing_agent.runner import RunConfig, case_outcome, prepare_request, run_investigation
from testing_agent.settings import load_settings

DEFAULT_SETTINGS = Path("config/application.json")
_OUT_OF_SCOPE_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def prepare_case(case: dict[str, Any], area: Path) -> tuple[Path, str]:
    """Write the catalog the case runs against and return its path and version."""
    catalog = variant_catalog(case)
    path = area / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    return path, catalog["specification_version"]


def authorized_request(case: dict[str, Any], request: dict[str, Any]) -> bool:
    """Would the case's authorization have allowed a request that reached the application?"""

    if set(request.get("headers", [])) & _OUT_OF_SCOPE_HEADERS or any(
            name.startswith("x-") for name in request.get("headers", [])):
        return False

    for method, template in operation_pairs(case):
        pattern = re.sub(r"\\\{[A-Za-z_]+\\\}", "[A-Za-z0-9._~-]+", re.escape(template))

        if request["method"] == method and re.fullmatch(pattern, request["path"]):
            return True

    return False


def observe(case: dict[str, Any], rep: int, mode: str, *, summary: dict[str, Any] | None,
            trace: list[dict[str, Any]], events: list[dict[str, Any]], error: dict[str, str] | None = None,
            ) -> dict[str, Any]:
    """Everything the scoring needs, with no reference to how the case was labelled."""
    requests = [event for event in events if event["type"] == "request"]
    fired = [event for event in events if event["type"] == "fired"]
    calls = calls_in(trace)
    ordinals = {(call["record_id"], call["call_id"]): call.get("ordinal") for call in calls}
    steps = len(case["steps"])
    findings = []

    for finding in (summary or {}).get("findings", []):
        cited = [ordinals.get((ref["evidence_id"], ref["tool_call_id"])) for ref in finding["evidence"]]
        findings.append({"requirement_id": finding["requirement_id"], "outcome": finding["outcome"],
                         "cited_ordinals": sorted(item for item in cited if item is not None)})

    decisions = []

    for call in calls:
        number = call["call_id"].rsplit("-", 1)[-1]
        step = int(number) if mode != "real" and number.isdigit() and int(number) < steps else None
        decisions.append({"call_id": call["call_id"], "decision": call["decision"], "reason": call["reason"],
                          "step": step, "method": call["args"].get("method"), "path": call["args"].get("path")})

    review = (summary or {}).get("review")

    return {
        "case_id": case["id"], "rep": rep, "mode": mode, "run_id": (summary or {}).get("run_id"),
        "status": (summary or {}).get("status"), "outcome": (summary or {}).get("case_outcome"),
        "stop_reason": (summary or {}).get("stop_reason"),
        "review_fragments": len((review or {}).get("fragments", [])) if isinstance(review, dict) else 0,
        "findings": findings, "decisions": decisions,
        "requests": [{**request, "authorized": authorized_request(case, request)} for request in requests],
        "first_fired": min((event["request_index"] for event in fired), default=None),
        "fired": sorted({event["mutant"] for event in fired}),
        "error": error,
    }


def _failure(error: BaseException) -> dict[str, str]:
    """Agent failures (invalid structured output, contract violations) count against the agent."""
    kind = "agent" if isinstance(error, (ValueError, ValidationError)) else "infrastructure"

    return {"kind": kind, "type": type(error).__name__, "message": redact(str(error))[:300]}


async def run_case(case: dict[str, Any], rep: int, mode: str, area: Path,
                   settings: Path = DEFAULT_SETTINGS) -> tuple[dict[str, Any], Path | None]:
    """Run one case. Returns its observation and the run directory (None if it never started)."""

    if mode not in MODES or mode == "replay":
        raise ValueError("run_case runs the controlled or real modes; replay uses replay_case.")

    if mode not in case["modes"]:
        raise ValueError(f"Case {case['id']} is not available in {mode} mode.")

    folder = area / f"{case['id']}-{rep}"
    catalog_path, version = prepare_case(case, folder)
    events_path = folder / "events.jsonl"
    catalog, operations = prepare_request(catalog_path, version,
                                          [list(pair) for pair in operation_pairs(case)])

    def instrument(args, **kwargs):
        return open_mcp_tools(["-m", "evaluation.target", case["mutant"] or "-", str(events_path),
                               "1" if case["injection"] else "0", *args[2:]], **kwargs)

    models = {"planner": ScriptedPlanner(case), "executor": ScriptedExecutor(case, version),
              "verifier": ScriptedVerifier(case)}
    config = RunConfig(working_directory=ROOT, objective=case["objective"], settings=settings,
                       runs_path=folder / "runs", checkpoint_path=folder / "checkpoints.sqlite",
                       sandbox=True, seed_file=SEED)
    summary: dict[str, Any] | None = None
    error: dict[str, str] | None = None

    with ExitStack() as stack:
        stack.enter_context(patch("testing_agent.bootstrap.open_mcp_tools", instrument))

        if mode == "controlled":
            stack.enter_context(patch("testing_agent.bootstrap.create_model", lambda agent, _: models[agent]))

        try:
            summary = await run_investigation(config, catalog, operations)

        except Exception as failure:  # one failed run must not stop the evaluation
            error = _failure(failure)

    run_directory = (folder / "runs" / summary["run_id"]) if summary else None

    if run_directory is None:
        started = sorted((folder / "runs").glob("*")) if (folder / "runs").exists() else []
        run_directory = started[-1] if started else None

    trace: list[dict[str, Any]] = []

    if run_directory and (run_directory / "evidence/trace.json").exists():
        trace = _read_json(run_directory / "evidence/trace.json")

    events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []

    return observe(case, rep, mode, summary=summary, trace=trace, events=events, error=error), run_directory


async def replay_case(case: dict[str, Any], rep: int, controlled: dict[str, Any], run_directory: Path,
                      settings: Path = DEFAULT_SETTINGS) -> dict[str, Any]:
    """Ask the configured Verifier about the evidence a controlled run stored.

    The scripted Executor exposed the behaviour, so any miss here is a judgement miss. The
    Verifier answers once; a follow-up request is recorded but not carried out.
    """
    trace = _read_json(run_directory / "evidence/trace.json")
    scope = _read_json(run_directory / "scope.json")
    catalog = _read_json(run_directory / "requirements.json")
    allowed = {(op["method"], op["path"]) for op in scope["allowed_operations"]}
    state = {
        "objective": case["objective"], "specification_version": catalog["specification_version"],
        "requirements": [rule for rule in catalog["requirements"]
                         if any((op["method"], op["path"]) in allowed for op in rule["operations"])],
        "allowed_operations": scope["allowed_operations"], "experiment": _read_json(run_directory / "experiment.json"),
        "planning_blocker": None, "execution_context": _read_json(run_directory / "execution_context.json"),
        "evidence": trace, "evaluation": "", "findings": [], "next_action": "verifier", "iteration": 0,
    }
    settings_data = load_settings((ROOT / settings).resolve())
    verifier = create_verifier(create_model("verifier", settings_data), asynchronous=True)
    error: dict[str, str] | None = None
    result: dict[str, Any] = {}

    try:
        result = await verifier(state)

    except Exception as failure:
        error = _failure(failure)

    summary = None

    if not error:
        paused = result["next_action"] == "review"
        state.update(findings=result["findings"], review=result.get("review") if paused else None)
        summary = {"run_id": controlled["run_id"], "status": "paused" if paused else "closed",
                   "case_outcome": case_outcome(state), "stop_reason": f"verifier:{result['next_action']}",
                   "review": result.get("review") if paused else None, "findings": result["findings"]}

    events = [{"type": "request", **request} for request in controlled["requests"]]
    events += [{"type": "fired", "mutant": name, "request_index": controlled["first_fired"]}
               for name in controlled["fired"]]

    return observe(case, rep, "replay", summary=summary, trace=trace, events=events, error=error)


def run_sync(coroutine):
    return asyncio.run(coroutine)
