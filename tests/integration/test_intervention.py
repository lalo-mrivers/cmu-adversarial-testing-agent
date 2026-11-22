import asyncio
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from testing_agent import cli
from testing_agent.agents.executor import create_executor
from testing_agent.agents.planner import create_planner
from testing_agent.agents.verifier import create_verifier
from testing_agent.domain.requirements import OperationReference, RequirementCatalog
from testing_agent.integrations.mcp.server import create_mcp_server
from testing_agent.integrations.target.http_client import SANDBOX_ORIGIN, open_http_client
from testing_agent.orchestration.graph import create_graph
from testing_agent.orchestration.policies import RunBudget
from testing_agent.persistence.evidence import save_evidence
from testing_agent.persistence.redaction import redact
from testing_agent.persistence.runs import create_run
from testing_agent.tools.application import create_application_request
from testing_agent.tools.requirements import create_requirements_lookup
from tests.integration.test_experiment_flow import (
    CREATE, GET, ScriptedModel, TargetClient, finding, initial_state, plan_data, request_message, tools_for,
)
from tests.integration.test_mvp_evaluation import CATALOG, PRODUCT, ambiguous_catalog, run_case


def test_followups_share_one_budget():
    async def run():
        state, client = initial_state(), TargetClient()
        budget = RunBudget(2, 60)
        verifier = ScriptedModel([lambda ctx: finding(ctx, "executor", "inconclusive")] * 2)
        graph = create_graph(create_planner(ScriptedModel([{"experiment": plan_data(), "planning_blocker": None}])),
                             create_executor(ScriptedModel([request_message(1, GET), AIMessage(content="Partial"),
                                                           request_message(2, GET), AIMessage(content="Partial")]),
                                             tools_for(state, client), budget=budget),
                             create_verifier(verifier), max_iterations=3, budget=budget)
        result = await graph.ainvoke(state)
        assert len(client.calls) == 2 and result["iteration"] == 2
        assert result["stop_reason"] == "tool_limit" and result["next_action"] == "end"
        assert result["findings"][0]["outcome"] == "inconclusive" and not result["review"]

    asyncio.run(run())


def test_interrupted_mutation_pauses_without_repeating():
    class Interrupted(TargetClient):
        async def request(self, method, path, **kwargs):
            await super().request(method, path, **kwargs)
            raise TimeoutError("Lost the response after applying the mutation.")

    async def run():
        state, client = initial_state(), Interrupted()
        model = ScriptedModel([finding])
        graph = create_graph(create_planner(ScriptedModel([{"experiment": plan_data(), "planning_blocker": None}])),
                             create_executor(ScriptedModel([request_message(0, CREATE), request_message(1, CREATE)]),
                                             tools_for(state, client)), create_verifier(model), max_iterations=1)
        result = await graph.ainvoke(state)
        assert result["stop_reason"] == "recovery_required" and result["next_action"] == "review"
        assert client.stock == 7 and len(client.calls) == 1 and not model.inputs
        assert result["findings"][0]["outcome"] == "inconclusive"

    asyncio.run(run())


def test_mcp_rejects_scope_arguments_before_coercion_and_charges_each_attempt():
    async def run():
        catalog = RequirementCatalog.model_validate_json(CATALOG.read_bytes())
        budget = RunBudget(3, 10)
        client = TargetClient()
        server = create_mcp_server(requirements_lookup=create_requirements_lookup(CATALOG, "v1"),
                                   application_request=create_application_request(
                                       client, catalog, [OperationReference.model_validate(GET)]), budget=budget)
        args = {"specification_version": "v1", **GET, "path_parameters": {"product_id": "product-001"}}

        for extra in ({"account": "other"}, {"base_url": "https://blocked.invalid"}):
            result = await server.call_tool("request_application", {**args, **extra})
            assert result["decision"] == "blocked"

        assert not client.calls
        await server.call_tool("request_application", args)
        result = await server.call_tool("request_application", args)
        assert result["reason"] == "tool_limit" and len(client.calls) == 1

    asyncio.run(run())


def test_detected_isolation_loss_pauses_real_controller(tmp_path):
    summary, trace, ingress = run_case("isolation", tmp_path)
    assert summary["status"] == "paused" and summary["stop_reason"] == "containment_lost"
    assert len(ingress) == len(trace) == 1
    assert summary["findings"][0]["outcome"] == "inconclusive"
    assert trace[0]["decisions"][0]["decision"] == "unknown"


def decision(parent, expected):
    return {"run_id": parent, "reviewer": "Fixture supervisor", "action": "continue",
            "resolution": "Use the request product identifier. The alias clause is withdrawn in the reviewed catalog.",
            "allowed_operations": [GET], "checks": [{
                "requirement_id": PRODUCT, "operation": GET, "arguments": {"path_parameters": {"product_id": "product-001"}},
                "check": "json_value", "selector": "/id", "expected_json": json.dumps(expected),
            }]}


@pytest.mark.parametrize("expected,passes", [("product-001", True), ("fixture-other", False)])
def test_decision_validates_before_new_investigation(tmp_path, expected, passes):
    conflict = ambiguous_catalog(tmp_path)
    prior, _, _ = run_case("ambiguity", tmp_path, conflict)
    parent = prior["run_id"]
    assert prior["status"] == "paused"
    decision_path = tmp_path / "decision-input.json"
    decision_path.write_text(json.dumps(decision(parent, expected)), encoding="utf-8")
    args = ["review", "--run-id", parent, "--runs-path", str(tmp_path),
            "--decision-file", str(decision_path), "--requirements", str(CATALOG)]
    assert cli.main(args) == 0
    assert cli.main(args) == 1  # immutable decision
    original = Path(prior["trace_path"]).read_bytes()
    summary, trace, ingress = run_case("valid", tmp_path, review_of=parent)
    directory = tmp_path / summary["run_id"]
    validation = json.loads((directory / "validation.json").read_text(encoding="utf-8"))
    assert validation["passed"] == passes
    assert json.loads((directory / "parent.json").read_text())["run_id"] == parent
    assert Path(prior["trace_path"]).read_bytes() == original

    if passes:
        assert summary["status"] == "closed" and len(ingress) == 1
        assert trace[0]["instance_id"] != validation["execution_context"]["instance_id"]
        assert summary["findings"][0]["outcome"] == "no_violation_observed"

    else:
        assert summary["status"] == "paused" and summary["stop_reason"] == "validation_failed"
        assert not trace and not ingress


def test_silence_or_stop_does_not_authorize_continuation(tmp_path):
    summary, _, _ = run_case("ambiguity", tmp_path, ambiguous_catalog(tmp_path))
    parent = summary["run_id"]
    args = ["run", "--review-of", parent, "--sandbox", "--operation", "GET", GET["path"],
            "--version", "v1", "--requirements", str(CATALOG), "--runs-path", str(tmp_path),
            "--objective", "Check the response for this synthetic product."]

    with patch("testing_agent.runner.open_application") as graph:
        assert cli.main(args) == 1
        path = tmp_path / "stop.json"
        path.write_text(json.dumps({"run_id": parent, "reviewer": "Fixture supervisor",
                                    "action": "stop", "resolution": "Close this investigation."}))
        assert cli.main(["review", "--run-id", parent, "--decision-file", str(path),
                         "--runs-path", str(tmp_path)]) == 0
        assert cli.main(args) == 1
        graph.assert_not_called()

    assert not (tmp_path / parent / "continuation.json").exists()


def test_enforcement_errors_and_pending_calls_cannot_confirm_a_defect():
    async def run():
        state, client = initial_state(), TargetClient()
        plan = plan_data()
        state.update(create_planner(ScriptedModel([{"experiment": plan, "planning_blocker": None}]))(state))
        # A valid plan can still yield a different, unauthorized runtime proposal.
        proposed = request_message(1, GET)
        proposed.tool_calls[0]["args"]["path"] = "/ungranted"
        update = await create_executor(ScriptedModel([proposed, AIMessage(content="Done")]), tools_for(state, client))(state)
        state.update(update)

        with pytest.raises(ValueError, match="observed response"):
            create_verifier(ScriptedModel([lambda ctx: finding(ctx, outcome="violation")]))(state)

        assert not client.calls and update["evidence"][0]["decisions"][0]["decision"] == "blocked"

    asyncio.run(run())


def test_redaction_preserves_duplicate_keys_and_original_records(tmp_path):
    raw = '{ "quantity":1.00, "quantity":2, "password":"fixture-value" }'
    masked = raw.replace("fixture-value", "[REDACTED]")
    assert redact(raw) == masked
    assert json.loads(redact(json.dumps({"body": raw})))["body"] == masked
    run_id = create_run(tmp_path, "Synthetic test", "v1")["run_id"]
    record = {"id": str(uuid4()), "specification_version": "v1", "messages": [{"content": raw}]}
    trace = save_evidence(tmp_path, run_id, [record])
    previous = trace.read_bytes()
    assert b"fixture-value" not in previous

    with pytest.raises(ValueError, match="cannot be removed"):
        save_evidence(tmp_path, run_id, [])

    assert trace.read_bytes() == previous


def test_redirect_is_recorded_without_a_second_dispatch():
    async def run():
        calls = []

        async def redirect(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 307,
                        "headers": [(b"location", b"https://blocked.invalid")]})
            await send({"type": "http.response.body", "body": b""})

        catalog = RequirementCatalog.model_validate_json(CATALOG.read_bytes())

        async with open_http_client(SANDBOX_ORIGIN, application=redirect) as client:
            request = create_application_request(client, catalog, [OperationReference.model_validate(GET)])
            result = await request("v1", "GET", GET["path"], {"product_id": "product-001"})

        assert result["response"]["status_code"] == 307 and calls == ["/products/product-001"]

    asyncio.run(run())


def test_review_takes_precedence_at_the_last_call_and_iteration():
    async def run():
        state, client = initial_state(), TargetClient()
        alternate = {**state["requirements"][0], "id": "REQ-ALT",
                     "expected_behavior": "Repeating a cancellation must be rejected."}
        state["requirements"].append(alternate)
        budget = RunBudget(1, 60)

        def review(context):
            result = finding(context, "review", "inconclusive")
            result["review"] = {
                "question": "Which response applies on repeated cancellation?",
                "applicable_conditions": "Both rules concern the same repeated operation.",
                "exception_and_precedence_checks": "No exceptions or precedence are documented in either rule.",
                "fragments": [{"requirement_id": rule["id"], "quote": rule["expected_behavior"]}
                              for rule in context["requirements"]],
            }

            return result

        graph = create_graph(create_planner(ScriptedModel([{"experiment": plan_data(), "planning_blocker": None}])),
                             create_executor(ScriptedModel([request_message(1, GET), AIMessage(content="Done")]),
                                             tools_for(state, client), budget=budget),
                             create_verifier(ScriptedModel([review])), max_iterations=1, budget=budget)
        result = await graph.ainvoke(state)
        assert budget.remaining == 0 and result["next_action"] == "review"
        assert result["stop_reason"] == "interpretation_review" and result["iteration"] == 1

    asyncio.run(run())
