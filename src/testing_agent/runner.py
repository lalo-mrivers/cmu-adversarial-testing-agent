import asyncio
import json
import threading
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from testing_agent.bootstrap import open_application
from testing_agent.domain.requirements import OperationReference, RequirementCatalog
from testing_agent.orchestration.policies import RunBudget
from testing_agent.orchestration.state import AgentState
from testing_agent.persistence.evidence import save_evidence
from testing_agent.persistence.redaction import redact
from testing_agent.persistence.review import approved_decision, validate_continuation, write_json
from testing_agent.persistence.runs import create_run
from testing_agent.persistence.status import StatusWriter, execution_from_summary
from testing_agent.tools.application import ApplicationScope
from testing_agent.tools.sandbox import SandboxUnavailable


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rejects duplicate JSON keys."""
    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}.")

        result[key] = value

    return result


def loads_json(text: str) -> Any:
    """Parses JSON text, rejecting duplicate keys."""

    return json.loads(text, object_pairs_hook=_unique_keys)


def read_json(path: Path) -> Any:
    """Reads JSON, including UTF-8 files with a BOM."""

    with path.open(encoding="utf-8-sig") as file:
        return json.load(file, object_pairs_hook=_unique_keys)


def prepare_request(
    requirements: Path, version: str, operation_pairs: Sequence[Sequence[str]],
) -> tuple[RequirementCatalog, list[OperationReference]]:
    """Load the catalog and check the authorized operations against it.

    Raises ValueError with the datum that can be corrected. It does not touch
    the target, the settings or any credential.
    """
    catalog = RequirementCatalog.model_validate(read_json(requirements))

    if catalog.specification_version != version:
        raise ValueError("The requested version does not match the catalog version.")

    operations = [
        OperationReference.model_validate({"method": method, "path": path})
        for method, path in operation_pairs
    ]
    allowed = {(operation.method, operation.path) for operation in operations}
    documented = {
        (operation.method, operation.path)
        for requirement in catalog.requirements
        for operation in requirement.operations
    }

    if not allowed:
        raise ValueError("Authorize at least one operation.")

    if len(allowed) != len(operations):
        raise ValueError("There are duplicate authorized operations.")

    if not allowed.issubset(documented):
        raise ValueError("All authorized operations must exist in the catalog.")

    if any(method == "CONNECT" for method, _ in allowed):
        raise ValueError("CONNECT is not allowed.")

    return catalog, operations


@dataclass(frozen=True)
class RunConfig:
    """Everything one investigation needs; paths are resolved against `working_directory`."""

    working_directory: Path
    objective: str
    settings: Path = Path("config/application.json")
    runs_path: Path = Path("var/runs")
    checkpoint_path: Path = Path("var/checkpoints/checkpoints.sqlite")
    sandbox: bool = True
    seed_file: Path | None = None
    headers_file: Path | None = None
    base_url: str | None = None
    review_of: str | None = None
    # Who asked for this run and why (system, endpoints, requirements). Stored beside the run
    # as investigation.json for the interface; the agents never read it.
    context: dict[str, Any] | None = None


class RunControl:
    """Lets the caller learn the run ID and ask for a stop while the run is active."""

    def __init__(self, on_created: Callable[[str], None] | None = None) -> None:
        self.run_id: str | None = None
        self.created = threading.Event()  # Set from the run's loop, awaited from the caller's thread.
        self.stop_requested = False
        self._budget: RunBudget | None = None
        self._status: StatusWriter | None = None
        self._on_created = on_created

    def request_stop(self) -> None:
        """Refuse further operations. An operation in flight is not interrupted."""

        if self.stop_requested:
            return

        self.stop_requested = True

        if self._budget is not None:
            self._budget.request_stop()

        if self._status is not None:
            self._status.update(stop_requested=True)
            self._status.milestone("stop_requested")

    def _attach_budget(self, budget: RunBudget) -> None:
        self._budget = budget

        if self.stop_requested:
            budget.request_stop()

    def _attach_run(self, run_id: str, status: StatusWriter) -> None:
        self.run_id, self._status = run_id, status
        self.created.set()

        if self._on_created is not None:
            self._on_created(run_id)


def case_outcome(state: dict[str, Any]) -> str:
    """Overall business outcome of a finished or paused investigation."""
    outcomes = {finding["outcome"] for finding in state.get("findings", [])}

    if state.get("review") or state.get("case_outcome"):
        return "inconclusive"

    if "violation" in outcomes:
        return "violation"

    if outcomes == {"no_violation_observed"}:
        return "no_violation_observed"

    return "inconclusive"


def _milestone_for(node: str, update: dict[str, Any]) -> tuple[str, str | None] | None:
    """Translate a finished graph node into a confirmed milestone."""

    if node == "planner":
        if update.get("experiment"):
            return "plan_ready", update["experiment"]["id"]

        return "plan_blocked", update.get("planning_blocker")

    if node == "executor":
        record = (update.get("evidence") or [{}])[-1]
        executed = sum(item.get("decision") == "executed" for item in record.get("decisions", []))

        return "execution_recorded", f"{executed} call(s) executed; stopped by: {record.get('stop_reason')}"

    if node == "verifier":
        return "evaluation_done", update.get("next_action")

    return None


async def run_investigation(
    config: RunConfig,
    catalog: RequirementCatalog,
    operations: list[OperationReference],
    *,
    control: RunControl | None = None,
) -> dict[str, Any]:
    """Runs a new investigation, exports its artifacts and returns its summary."""
    control = control or RunControl()
    directory: Path = config.working_directory
    runs_path = (directory / config.runs_path).resolve()

    if redact(catalog.model_dump()) != catalog.model_dump():
        raise ValueError("The specification contains sensitive values. Supply a sanitized version for testing.")

    scope = {"specification_version": catalog.specification_version,
             "allowed_operations": [op.model_dump(mode="json") for op in operations], "account": "anonymous",
             "target": "managed_inventory", "objective": redact(config.objective).strip()}
    parent = config.review_of
    decision = approved_decision(runs_path, parent, scope, catalog, config) if parent else None

    run = create_run(
        runs_path,
        redact(config.objective),
        catalog.specification_version,
    )
    run_id = run["run_id"]
    run_directory = runs_path / run_id

    if config.context:
        write_json(run_directory / "investigation.json", config.context)

    status = StatusWriter(runs_path, run_id)
    control._attach_run(run_id, status)
    status.milestone("run_started")

    try:
        return await _execute(config, catalog, operations, control, status, run, scope, decision,
                              parent, runs_path, run_directory)

    except Exception as error:
        status.finish("failed", error=f"{type(error).__name__}: {error}")
        raise


async def _execute(config, catalog, operations, control, status, run, scope, decision,
                   parent, runs_path, run_directory) -> dict[str, Any]:
    directory: Path = config.working_directory
    run_id = run["run_id"]

    # Both processes will use a copy of this investigation.
    catalog_path = run_directory / "requirements.json"
    catalog_path.write_text(
        catalog.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    allowed = {(operation.method, operation.path) for operation in operations}

    initial_state: AgentState = {
        "objective": run["objective"],
        "specification_version": run["specification_version"],
        "requirements": [
            requirement.model_dump(mode="json")
            for requirement in catalog.requirements
            if any(
                (operation.method, operation.path) in allowed
                for operation in requirement.operations
            )
        ],
        "allowed_operations": scope["allowed_operations"],
        "experiment": None,
        "planning_blocker": None,
        "evidence": [],
        "evaluation": "",
        "findings": [],
        "next_action": "planner",
        "iteration": 0,
    }

    trace_path = save_evidence(runs_path, run_id, [])
    evidence_count = 0
    last_state = initial_state
    experiment_path = run_directory / "experiment.json"
    experiment_path.write_text("null\n", encoding="utf-8")
    last_experiment: dict[str, Any] | None = None

    write_json(run_directory / "scope.json", scope)
    execution_context = {}
    initial_state["execution_context"] = execution_context

    if decision:
        write_json(run_directory / "parent.json", {"run_id": parent, "decision": decision})
        write_json(runs_path / parent / "continuation.json", {"run_id": run_id})

    def pause(reason, details):
        last_state.update(next_action="review", stop_reason=reason, evaluation="Inconclusive. " + details,
                          review={"reason": reason, "details": details})

    if not config.sandbox or config.headers_file is not None:
        pause("scope_change", "Only the managed inventory and anonymous account are authorized.")
        last_state["review"]["requested_environment"] = "external" if not config.sandbox else "managed_inventory"
        last_state["review"]["requested_account"] = "external_headers" if config.headers_file else "anonymous"

        if not config.sandbox:
            try:
                origin = urlsplit(config.base_url)
                last_state["review"]["requested_origin"] = {
                    "scheme": origin.scheme, "host": origin.hostname, "port": origin.port,
                }

            except ValueError:
                last_state["review"]["requested_origin"] = "Invalid origin"

    else:
        server_args = [
            "-m", "testing_agent.cli", "serve", "--working-directory", str(directory),
            "--requirements", str(catalog_path), "--version", catalog.specification_version,
            "--sandbox", "--seed-file", str(config.seed_file), "--settings", str(config.settings),
        ]

        for operation in operations:
            server_args.extend(["--operation", operation.method, operation.path])

    if not last_state.get("review") and decision:
        validation = await validate_continuation(directory, server_args, decision, catalog)
        write_json(run_directory / "validation.json", validation)

        if not validation["passed"]:
            pause("validation_failed", "The revised case did not pass its supervised checks.")
            last_state["review"]["validation_requirements"] = sorted({c["requirement_id"] for c in decision["checks"]})

    async with AsyncExitStack() as stack:
        if not last_state.get("review"):
            try:
                graph = await stack.enter_async_context(open_application(
                    server_args, working_directory=directory, settings_path=config.settings,
                    checkpoint_path=config.checkpoint_path, execution_context=execution_context,
                    application_scope=ApplicationScope(catalog, operations),
                    on_budget=control._attach_budget,
                ))

            except SandboxUnavailable as error:
                pause("startup_unverified", str(error))

        if decision and not last_state.get("review"):
            validated = validation["execution_context"]
            fields = ("initial_state", "transport", "account", "base_url", "network_restricted",
                      "limits", "specification_version", "allowed_operations")

            if (execution_context["instance_id"] == validated["instance_id"]
                or any(execution_context[key] != validated[key] for key in fields)):
                pause("validation_failed", "The fresh target differs from the validated conditions.")

        if not last_state.get("review") and control.stop_requested:
            # Nothing was dispatched: close without opening the graph.
            last_state.update(next_action="end", stop_reason="user_stopped", case_outcome="inconclusive",
                              evaluation="Inconclusive. The investigation was stopped before any operation.")

        elif not last_state.get("review"):
            if decision:
                execution_context["supervisor_decision"] = decision["resolution"]
                execution_context["previous_run_id"] = parent

            write_json(run_directory / "execution_context.json", execution_context)
            status.milestone("target_ready", execution_context.get("instance_id"))

            async for mode, chunk in graph.astream(
                initial_state,
                config={"configurable": {"thread_id": run_id}},
                stream_mode=["values", "updates"],
            ):
                if mode == "updates":
                    for node, update in chunk.items():
                        milestone = _milestone_for(node, update or {})

                        if milestone:
                            status.milestone(*milestone)

                    continue

                snapshot = chunk
                last_state = snapshot
                evidence = snapshot["evidence"]

                # Each evidence record retains its own complete plan. This file
                # exposes the current plan even before the Executor finishes.
                experiment = snapshot.get("experiment")

                if experiment != last_experiment:
                    experiment_path.write_text(
                        json.dumps(experiment, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                        encoding="utf-8",
                    )
                    last_experiment = experiment

                # The state accumulates records through the evidence reducer.
                if len(evidence) > evidence_count:
                    trace_path = save_evidence(runs_path, run_id, evidence)
                    evidence_count = len(evidence)

                status.update(iteration=snapshot["iteration"], evidence_records=evidence_count)

    if last_state.get("review"):
        write_json(run_directory / "review.json", {
            "run_id": run_id, "reason": last_state["stop_reason"], "request": last_state["review"],
            "scope": scope, "requirements": initial_state["requirements"], "trace": "evidence/trace.json",
            "execution_context": execution_context, "findings": last_state.get("findings", []),
            "created_at": datetime.now(UTC).isoformat(),
        })

    summary = {
        "status": "paused" if last_state.get("review") else "closed",
        "case_outcome": case_outcome(last_state),
        "stop_reason": last_state.get("stop_reason"),
        "review": last_state.get("review"),
        "specification_version": catalog.specification_version,
        "run_id": run_id,
        "evaluation": last_state["evaluation"],
        "findings": last_state.get("findings", []),
        "iterations": last_state["iteration"],
        "evidence_records": evidence_count,
        "experiment_id": (last_state.get("experiment") or {}).get("id"),
        "experiment_path": str(experiment_path),
        "planning_blocker": last_state.get("planning_blocker"),
        "trace_path": str(trace_path),
    }
    write_json(run_directory / "reports" / "outcome.json", summary)
    status.finish(execution_from_summary(summary), stop_reason=summary["stop_reason"])

    return summary
