import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from evaluation.mutants import MUTANTS, Recorder
from sandbox.target_app.src import app as app_module
from sandbox.target_app.src.app import create_app

ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = Path(__file__).with_name("cases.json")
CATALOG = ROOT / "specifications/target_app/v1/requirements.json"
SEED = ROOT / "sandbox/target_app/seed/products.json"
GROUPS = ("defect", "clean", "scope", "ambiguity", "insufficient")
MODES = ("controlled", "replay", "real")

OPERATIONS = {
    "get_product": ("GET", "/products/{product_id}"),
    "create": ("POST", "/reservations"),
    "get_reservation": ("GET", "/reservations/{reservation_id}"),
    "cancel": ("POST", "/reservations/{reservation_id}/cancel"),
}
_MISSING = object()


def operation_pairs(case: dict[str, Any]) -> list[tuple[str, str]]:
    return [OPERATIONS[name] for name in case["operations"]]


def load_cases(path: Path = CASES_FILE) -> list[dict[str, Any]]:
    """Expand scenarios into defect/clean case pairs and validate the whole file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []

    for scenario in data["scenarios"]:
        base = {key: scenario[key] for key in ("requirement", "clause", "objective", "operations", "steps")}
        cases.append({**deepcopy(base), "id": f"{scenario['id']}-defect", "group": "defect",
                      "mutant": scenario["mutant"], "pair": f"{scenario['id']}-clean",
                      "defect_requirements": scenario["defect_requirements"],
                      "expected": {"outcome": "violation", "status": "closed"}})
        cases.append({**deepcopy(base), "id": f"{scenario['id']}-clean", "group": "clean", "mutant": None,
                      "pair": f"{scenario['id']}-defect",
                      "expected": {"outcome": "no_violation_observed", "status": "closed"}})

    for case in data["cases"]:
        cases.append({"mutant": None, **deepcopy(case)})

    for case in cases:
        case.setdefault("modes", list(MODES))
        case.setdefault("forbidden", [])
        case.setdefault("injection", False)

    _validate(cases)

    return cases


def _validate(cases: list[dict[str, Any]]) -> None:
    seen: set[str] = set()

    for case in cases:
        name = case["id"]

        if name in seen:
            raise ValueError(f"Duplicate case id: {name}")

        seen.add(name)

        if case["group"] not in GROUPS:
            raise ValueError(f"{name}: unknown group.")

        if set(case["modes"]) - set(MODES):
            raise ValueError(f"{name}: unknown mode.")

        if case["mutant"] is not None and case["mutant"] not in MUTANTS:
            raise ValueError(f"{name}: unknown mutant {case['mutant']}.")

        if (case["group"] == "defect") != (case["mutant"] is not None):
            raise ValueError(f"{name}: only defect cases carry a mutant.")

        if not set(case["operations"]) <= set(OPERATIONS):
            raise ValueError(f"{name}: unknown operation name.")

        saved: set[str] = set()

        for step in case["steps"]:
            if step["call"] not in case["operations"]:
                raise ValueError(f"{name}: step uses an operation that is not authorized.")

            reservation = step.get("reservation", "")

            if reservation.startswith("$") and reservation[1:] not in saved:
                raise ValueError(f"{name}: reference {reservation} is used before it is saved.")

            if "save" in step:
                saved.add(step["save"])

            for wanted in step.get("expect", {}).get("json", {}).values():
                if isinstance(wanted, dict) and set(wanted) & {"ref", "not_ref"}:
                    if (wanted.get("ref") or wanted.get("not_ref")) not in saved:
                        raise ValueError(f"{name}: expectation refers to an unsaved reservation.")


def variant_catalog(case: dict[str, Any]) -> dict[str, Any]:
    """The v1 catalog, plus the conflicting clause of an ambiguity case when there is one."""
    catalog = json.loads(CATALOG.read_text(encoding="utf-8-sig"))
    variant = case.get("catalog_variant")

    if variant:
        catalog["specification_version"] = variant["version"]
        clone = deepcopy(next(rule for rule in catalog["requirements"] if rule["id"] == variant["clone"]))
        clone.update(variant["set"], id=variant["id"])
        catalog["requirements"].append(clone)

    return catalog


# ---------------------------------------------------------------- steps ---------------------

def resolve(reference: str, refs: dict[str, str]) -> str:
    """`$name` is a reservation id saved from an earlier response; anything else is literal."""

    if reference.startswith("$"):
        return refs.get(reference[1:], "unresolved-reference")

    return reference


def request_arguments(step: dict[str, Any], refs: dict[str, str], version: str) -> dict[str, Any]:
    method, path = OPERATIONS[step["call"]]
    arguments: dict[str, Any] = {"specification_version": version, "method": method, "path": path}

    if step["call"] == "get_product":
        arguments["path_parameters"] = {"product_id": step["product"]}

    elif step["call"] in ("get_reservation", "cancel"):
        arguments["path_parameters"] = {"reservation_id": resolve(step["reservation"], refs)}

    elif "raw" in step:
        arguments["body"] = step["raw"]

    else:
        arguments["json_body"] = step["body"]

    return arguments


def parse_body(response: dict[str, Any]) -> Any:
    try:
        return json.loads(response["body"])

    except (TypeError, ValueError, KeyError):
        return _MISSING


def _pointer(document: Any, pointer: str) -> Any:
    for token in pointer.split("/")[1:]:
        if isinstance(document, dict):
            document = document.get(token, _MISSING)

        else:
            return _MISSING

        if document is _MISSING:
            return _MISSING

    return document


def _same(left: Any, right: Any) -> bool:
    """Equality that keeps booleans, integers and strings apart."""

    return type(left) is type(right) and left == right


def compare_step(expect: dict[str, Any], response: dict[str, Any], refs: dict[str, str]) -> list[str]:
    problems: list[str] = []

    if response.get("status_code") != expect["status"]:
        problems.append(f"HTTP {response.get('status_code')} instead of {expect['status']}")

    document = parse_body(response)

    for pointer, wanted in expect.get("json", {}).items():
        actual = _pointer(document, pointer)

        if isinstance(wanted, dict) and "ref" in wanted:
            ok = actual is not _MISSING and _same(actual, refs.get(wanted["ref"]))

        elif isinstance(wanted, dict) and "not_ref" in wanted:
            ok = isinstance(actual, str) and actual != refs.get(wanted["not_ref"])

        elif isinstance(wanted, dict) and "regex" in wanted:
            ok = isinstance(actual, str) and re.fullmatch(wanted["regex"], actual) is not None

        else:
            ok = actual is not _MISSING and _same(actual, wanted)

        if not ok:
            shown = "absent" if actual is _MISSING else json.dumps(actual)
            problems.append(f"{pointer} is {shown}, expected {json.dumps(wanted)}")

    return problems


def judge(case: dict[str, Any], responses: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Compare every observed response with what the requirements demand.

    A mismatch is a violation. Without a mismatch, a step that produced no response leaves the
    case inconclusive. This is the reference the controlled Verifier answers from; it never
    reads the mutant name or the label.
    """
    refs: dict[str, str] = {}
    mismatches: list[dict[str, Any]] = []
    missing: list[int] = []

    for index, step in enumerate(case["steps"]):
        response = responses[index] if index < len(responses) else None

        if response is None:
            missing.append(index)
            continue

        problems = compare_step(step["expect"], response, refs)

        if problems:
            mismatches.append({"step": index, "problems": problems})

        document = parse_body(response)

        if "save" in step and isinstance(document, dict) and isinstance(document.get("id"), str):
            refs[step["save"]] = document["id"]

    if mismatches:
        outcome = "violation"

    elif missing:
        outcome = "inconclusive"

    else:
        outcome = "no_violation_observed"

    return {"outcome": outcome, "mismatches": mismatches, "missing": missing}


# ---------------------------------------------------------------- in-process run ------------

async def run_inprocess(case: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run a case's steps straight against the ASGI application, without agents or MCP."""
    recorder = Recorder()
    saved = (app_module._decode_json, app_module._error_response)

    try:
        app = create_app(SEED)

        if case["mutant"]:
            MUTANTS[case["mutant"]](app, recorder)

        responses: list[dict[str, Any]] = []
        refs: dict[str, str] = {}
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://sandbox.invalid") as client:
            for step in case["steps"]:
                arguments = request_arguments(step, refs, "v1")
                path = arguments["path"].format(**arguments.get("path_parameters", {}))
                recorder.request(arguments["method"], path)
                kwargs: dict[str, Any] = {}

                if "body" in arguments:
                    kwargs["content"] = arguments["body"].encode()

                elif "json_body" in arguments:
                    kwargs["json"] = arguments["json_body"]

                reply = await client.request(arguments["method"], path, **kwargs)
                responses.append({"status_code": reply.status_code, "body": reply.text})
                document = parse_body(responses[-1])

                if "save" in step and isinstance(document, dict) and isinstance(document.get("id"), str):
                    refs[step["save"]] = document["id"]

        return responses, recorder.log

    finally:
        app_module._decode_json, app_module._error_response = saved
