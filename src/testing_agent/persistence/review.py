import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from testing_agent.domain.requirements import NonEmptyText, OperationReference, RequirementCatalog
from testing_agent.integrations.mcp.client import open_mcp_tools
from testing_agent.orchestration.policies import RunBudget
from testing_agent.persistence.redaction import redact
from testing_agent.persistence.runs import load_run
from testing_agent.tools.assertions import Check, check_response
from testing_agent.tools.application import ApplicationScope


def write_json(path: Path, data: Any) -> None:
    content = json.dumps(redact(data), indent=2, ensure_ascii=False, allow_nan=False)

    with path.open("x", encoding="utf-8") as file:
        file.write(content + "\n")


class AcceptanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement_id: NonEmptyText
    operation: OperationReference
    arguments: dict[str, Any]
    check: Check
    expected_json: str
    selector: str | None = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: NonEmptyText
    reviewer: NonEmptyText
    action: Literal["stop", "continue"]
    resolution: NonEmptyText
    allowed_operations: list[OperationReference] = Field(default_factory=list)
    checks: list[AcceptanceCheck] = Field(default_factory=list)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def register_decision(runs_path, run_id, data, *, requirements=None,
                      seed=Path("sandbox/target_app/seed/products.json"),
                      settings=Path("config/application.json")):
    load_run(runs_path, run_id)
    directory = Path(runs_path) / run_id
    pause = json.loads((directory / "review.json").read_text(encoding="utf-8"))
    decision = Decision.model_validate(data)

    if pause["run_id"] != run_id or decision.run_id != run_id:
        raise ValueError("The decision must identify the paused run.")

    record = decision.model_dump(mode="json")

    if decision.action == "continue":
        if not decision.checks or not decision.allowed_operations:
            raise ValueError("Continuation requires an explicit scope and acceptance checks.")

        original = RequirementCatalog.model_validate_json((directory / "requirements.json").read_bytes())
        revised = RequirementCatalog.model_validate_json(
            Path(requirements or directory / "requirements.json").read_bytes())

        if original != revised and original.specification_version == revised.specification_version:
            raise ValueError("A changed specification requires a distinct version.")

        operations = {(op.method, op.path) for op in decision.allowed_operations}
        checked = set()
        rules = {rule.id: rule for rule in revised.requirements}

        for case in decision.checks:
            rule = rules.get(case.requirement_id)
            operation = (case.operation.method, case.operation.path)

            if operation not in operations or not rule or case.operation not in rule.operations:
                raise ValueError("Validation case outside approved operations or requirement.")

            if case.arguments.keys() - {"path_parameters", "params", "body", "json_body", "headers"}:
                raise ValueError("Invalid validation arguments.")

            checked.add(operation)

        previous = {(op["method"], op["path"]) for op in pause["scope"]["allowed_operations"]}

        if (operations - previous) - checked:
            raise ValueError("Include a validation case for each newly authorized operation.")

        if previous & operations and not checked & previous:
            raise ValueError("Include a regression for a previously authorized operation.")

        affected = {f["requirement_id"] for f in pause["request"].get("fragments", [])}
        affected.update(pause["request"].get("validation_requirements", []))
        affected.update(r.id for r in revised.requirements if r not in original.requirements
                        and any((op.method, op.path) in operations for op in r.operations))
        checked_ids = {case.requirement_id for case in decision.checks}

        for rule_id in affected - checked_ids:
            old = next((rule for rule in original.requirements if rule.id == rule_id), None)

            if rule_id in rules or old is None or not any((op.method, op.path) in checked for op in old.operations):
                raise ValueError("Include a check for each revised or disputed requirement.")

        record.update(catalog_sha256=hashlib.sha256(revised.model_dump_json().encode()).hexdigest(),
                      seed_sha256=_digest(seed), settings_sha256=_digest(settings))

    record["recorded_at"] = datetime.now(UTC).isoformat()
    write_json(directory / "decision.json", record)


def approved_decision(runs_path, parent, scope, catalog, args):
    load_run(runs_path, parent)
    directory = Path(runs_path) / parent
    decision = json.loads((directory / "decision.json").read_text(encoding="utf-8"))
    original = json.loads((directory / "scope.json").read_text(encoding="utf-8"))

    if (decision["run_id"] != parent or decision["action"] != "continue"
        or scope["objective"] != original["objective"] or not args.sandbox or args.headers_file
        or scope["allowed_operations"] != decision["allowed_operations"]
        or hashlib.sha256(catalog.model_dump_json().encode()).hexdigest() != decision["catalog_sha256"]
        or _digest(args.seed_file) != decision["seed_sha256"]
        or _digest(args.working_directory / args.settings) != decision["settings_sha256"]):
        raise PermissionError("The decision does not authorize this continuation.")

    return decision


async def validate_continuation(directory, server_args, decision, catalog):
    """Exercise approved expectations on a disposable instance before opening the investigation."""
    results = []
    context = {}

    try:
        async with open_mcp_tools(server_args, working_directory=directory, execution_context=context) as tools:
            request = next(tool for tool in tools if tool.name == "request_application")
            limits = RunBudget(context["limits"]["max_tool_calls"], context["limits"]["timeout_seconds"])
            scope = ApplicationScope(catalog, [OperationReference.model_validate(op)
                                              for op in decision["allowed_operations"]])

            for case in decision["checks"]:
                arguments = {**case["arguments"], **case["operation"],
                             "specification_version": catalog.specification_version}
                call_id = str(uuid4())
                item = {"tool_call_id": call_id, "arguments": arguments, "requirement_id": case["requirement_id"]}
                results.append(item)
                reason = limits.charge()

                if reason is None:
                    try:
                        scope.resolve(**arguments)

                    except (TypeError, ValueError, PermissionError) as error:
                        reason = str(error)

                if reason:
                    item.update(decision="blocked", reason=reason)

                    return {"passed": False, "execution_context": context, "checks": results}

                item["decision"] = "dispatched"

                async with asyncio.timeout(limits.seconds_left):
                    message = await request.ainvoke({"type": "tool_call", "name": request.name,
                                                     "id": call_id, "args": arguments})

                item["result"] = json.loads(message.content)

                if message.tool_call_id != call_id or item["result"].get("decision") != "executed":
                    return {"passed": False, "execution_context": context, "checks": results}

                item["comparison"] = check_response(item["result"]["response"], case["check"],
                                                    case["expected_json"], case["selector"])

                if item["comparison"]["outcome"] != "passed":
                    return {"passed": False, "execution_context": context, "checks": results}

    except Exception as error:
        return {"passed": False, "execution_context": context, "checks": results, "error": type(error).__name__}

    return {"passed": bool(results), "execution_context": context, "checks": results}
