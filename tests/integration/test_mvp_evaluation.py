import json
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage

from testing_agent import cli
from testing_agent.domain.findings import Finding
from testing_agent.integrations.mcp.client import open_mcp_tools
from tests.integration.test_experiment_flow import GET, CREATE, ScriptedModel


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "specifications/target_app/v1/requirements.json"
PRODUCT = "REQ-PRODUCT-001"
INVALID = "REQ-RESERVATION-001"
ALIAS = "REQ-PRODUCT-ALIAS"
QUOTED = "id must match the product_id in the path"
ALTERNATE = "For an existing product the response id must equal catalog-alias."


def plan(context):
    operation = context["allowed_operations"][0]
    rule = PRODUCT if operation == GET else INVALID

    return {"planning_blocker": None, "experiment": {
        "hypothesis": "The selected response conforms to the specified field or validation rule.",
        "specification_version": context["specification_version"], "requirement_ids": [rule],
        "preconditions": [], "stop_conditions": ["One business response is available."],
        "steps": [{"id": "read", "instruction": "Record the response.", "operation": operation,
                   "depends_on": [], "expected_observation": "Response matches the selected requirement.",
                   "evidence_to_collect": ["HTTP status and response body"]}],
    }}


def assess(context):
    """Controlled model response based only on the supplied clauses and matched response."""
    record = context["evidence"][0]
    message = next(m for m in record["messages"] if m["type"] == "tool")
    result = json.loads(message["content"])
    response = result["response"]
    body = json.loads(response["body"])
    requirements = {rule["id"]: rule for rule in context["requirements"]}
    request = record["messages"][0]["tool_calls"][0]["args"]

    if request["method"] == "GET":
        rule = PRODUCT
        matches = response["status_code"] == 200 and body["id"] == request["path_parameters"]["product_id"]
        assessment = f"Returned identifier {body['id']} for the requested product."

    else:
        rule = INVALID
        matches = (response["status_code"], body["error"]["code"]) == (422, "INVALID_REQUEST")
        assessment = "Checked the validation status and error code for zero units."

    outcome = "no_violation_observed" if matches else "violation"
    review = None

    if ALIAS in requirements:
        outcome = "inconclusive"
        review = {"question": "Which identifier should the existing product return?",
                  "applicable_conditions": "The seed and the HTTP 200 identify an existing product.",
                  "exception_and_precedence_checks": "Both clauses govern this version. Missing-product exceptions do not apply. Neither clause gives precedence.",
                  "fragments": [{"requirement_id": PRODUCT, "quote": QUOTED},
                                {"requirement_id": ALIAS, "quote": ALTERNATE}]}

    return {"next_action": "review" if review else "end", "review": review, "evaluation": assessment,
            "findings": [{"requirement_id": rule, "scope": "One response in this inventory.",
                          "outcome": outcome, "assessment": assessment,
                          "limitations": [review["question"]] if review else [],
                          "evidence": [{"evidence_id": record["id"], "tool_call_id": message["tool_call_id"]}]}]}


def run_case(case, area, catalog_path=CATALOG, *, review_of=None):
    """CLI, graph, MCP stdio, inventory, checkpoints and files are the normal entry points."""
    version = json.loads(catalog_path.read_text(encoding="utf-8-sig"))["specification_version"]
    operation = CREATE if case == "scope" else GET
    arguments = {"specification_version": version, **operation}

    if operation == GET:
        arguments["path_parameters"] = {"product_id": "product-001"}

    else:
        arguments["json_body"] = {"product_id": "product-001", "quantity": 0}

    proposals = [arguments]

    if case == "scope":
        proposals.extend([{**arguments, "path": "https://blocked.invalid/reservations"},
                          {**arguments, "headers": {"Authorization": "Bearer fixture-proposal-value"}}])

    messages = [AIMessage(content="", tool_calls=[{
        "id": f"call-{i}", "name": "request_application", "args": args,
    }]) for i, args in enumerate(proposals)]
    models = {"planner": ScriptedModel([plan]), "executor": ScriptedModel([*messages, AIMessage(content="Done.")]),
              "verifier": ScriptedModel([assess])}
    ingress = area / f"{case}-{uuid4()}.jsonl"

    def instrument(args, **kwargs):
        return open_mcp_tools(["-m", "tests.integration.mvp_target", case, str(ingress), *args[2:]], **kwargs)

    objective = "Check the response for this synthetic product."
    argv = ["run", "--sandbox", "--requirements", str(catalog_path), "--version", version,
            "--objective", objective, "--operation", operation["method"], operation["path"],
            "--runs-path", str(area), "--checkpoint-path", str(area / "checkpoints.sqlite")]

    if review_of:
        argv.extend(["--review-of", review_of])

    output = StringIO()

    with (patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]),
          patch("testing_agent.bootstrap.open_mcp_tools", instrument), redirect_stdout(output)):
        assert cli.main(argv) == 0

    summary = json.loads(output.getvalue())
    trace = json.loads(Path(summary["trace_path"]).read_text(encoding="utf-8"))
    received = [json.loads(line) for line in ingress.read_text().splitlines()] if ingress.exists() else []

    return summary, trace, received


def ambiguous_catalog(area):
    value = json.loads(CATALOG.read_text(encoding="utf-8-sig"))
    value["specification_version"] = "v1-eval-conflict"
    alias = deepcopy(next(rule for rule in value["requirements"] if rule["id"] == PRODUCT))
    alias.update(id=ALIAS, expected_behavior=ALTERNATE)
    value["requirements"].append(alias)
    path = area / "ambiguous.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    return path


def test_evaluate_local_cases():
    oracle = json.loads(Path(__file__).with_name("mvp_cases.json").read_text(encoding="utf-8"))
    area = ROOT / "var/evaluations" / str(uuid4())
    area.mkdir(parents=True)
    conflict = ambiguous_catalog(area)
    known = set()
    reported = set()
    forbidden_executed = false_blocks = authorized = proposed_outside = 0
    cases = {}

    for case, expected in oracle["cases"].items():
        summary, trace, ingress = run_case(case, area, conflict if case == "ambiguity" else CATALOG)
        rule = expected["requirement"]

        if expected["outcome"] == "violation":
            known.add((case, rule))

        findings = summary["findings"]

        for finding in findings:
            assert Finding.model_validate(finding).specification_version == summary["specification_version"]

            if finding["outcome"] == "violation":
                # Independent scoring requires a cited observation of the seeded mismatch.
                confirmed = False

                for ref in finding["evidence"]:
                    for record in trace:
                        for msg in record["messages"]:
                            if (record["id"] == ref["evidence_id"] and msg.get("type") == "tool"
                                    and msg["tool_call_id"] == ref["tool_call_id"]):
                                obs = json.loads(msg["content"])
                                confirmed |= (obs.get("operation", {}).get("resolved_path") == "/products/product-001"
                                              and json.loads(obs["response"]["body"]).get("id") == "fixture-other")

                reported.add((case, finding["requirement_id"] if confirmed else "unsupported"))

        events = [event for record in trace for event in record["decisions"]]
        permitted = set()

        for record in trace:
            for message in record["messages"]:
                for call in message.get("tool_calls", []):
                    args = call["args"]
                    operation = CREATE if case == "scope" else GET

                    if (args.get("path") == operation["path"] and args.get("method") == operation["method"]
                            and not args.get("headers") and args["specification_version"] == summary["specification_version"]):
                        authorized += 1
                        permitted.add((record["id"], call["id"]))

                    else:
                        proposed_outside += 1

        allowed_path = ["POST", "/reservations"] if case == "scope" else ["GET", "/products/product-001"]
        # Each scenario authorizes exactly the first proposal. Ingress independently
        # catches the same path being dispatched a second time with another identity.
        forbidden_executed += sum(item != allowed_path for item in ingress)
        forbidden_executed += max(0, ingress.count(allowed_path) - expected["authorized_requests"])
        false_blocks += sum((record["id"], e["tool_call_id"]) in permitted and e["decision"] == "blocked"
                            for record in trace for e in record["decisions"])
        cases[case] = {"run_id": summary["run_id"], "status": summary["status"], "ingress": ingress,
                       "decisions": events, "outcomes": [f["outcome"] for f in findings],
                       "review": summary["review"], "expected": expected}
        assert "fixture-private-value" not in json.dumps(trace)
        assert "fixture-proposal-value" not in json.dumps(trace)

        for secret in (b"fixture-private-value", b"fixture-proposal-value"):
            assert secret not in (area / "checkpoints.sqlite").read_bytes()

    tp, fp, fn = len(known & reported), len(reported - known), len(known - reported)
    metrics = {"tp": tp, "fp": fp, "fn": fn, "missed": sorted(known - reported),
               "precision": tp / (tp + fp) if tp + fp else None,
               "recall": tp / (tp + fn) if tp + fn else None,
               "authorized_proposed": authorized, "forbidden_proposed": proposed_outside,
               "forbidden_executed": forbidden_executed, "authorized_blocked": false_blocks}
    report = area / "report.json"
    report.write_text(json.dumps({"unit": oracle["unit"], "cases": cases, "metrics": metrics}, indent=2),
                      encoding="utf-8")
    print(report, json.dumps(metrics))

    for case, data in cases.items():
        assert data["status"] == data["expected"]["status"]
        assert data["outcomes"] == [data["expected"]["outcome"]]
        assert len(data["ingress"]) == data["expected"]["authorized_requests"]

    assert cases["ambiguity"]["review"]["fragments"]
    assert [e["decision"] for e in cases["scope"]["decisions"]] == ["executed", "blocked", "blocked"]
    assert authorized == sum(item["authorized_requests"] for item in oracle["cases"].values())
    assert proposed_outside == sum(item.get("forbidden_proposals", 0) for item in oracle["cases"].values())
    assert forbidden_executed == false_blocks == 0
