import asyncio
import json
import sys
import tempfile
import unittest
from collections import deque
from contextlib import asynccontextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.checkpoint.memory import InMemorySaver

from testing_agent.agents.executor import create_executor
from testing_agent.agents.planner import PlannerOutput, create_planner
from testing_agent.agents.verifier import (
    VerifierOutput,
    _evidence_for_verifier,
    evidence_references,
    create_verifier,
)
from testing_agent.domain.experiments import Experiment
from testing_agent.domain.requirements import OperationReference, RequirementCatalog
from testing_agent.orchestration.graph import create_graph
from testing_agent.tools.application import create_application_request


GET = {"method": "GET", "path": "/products/{product_id}"}
CREATE = {"method": "POST", "path": "/reservations"}
CANCEL = {"method": "POST", "path": "/reservations/{reservation_id}/cancel"}


def initial_state():
    return {
        "objective": "Check repeated cancellation for product-001.",
        "specification_version": "v1",
        "requirements": [{
            "id": "REQ-CANCEL", "description": "Cancellation is idempotent.",
            "operations": [GET, CREATE, CANCEL], "preconditions": [],
            "expected_behavior": "Repeating cancellation returns the same reservation without increasing stock.",
            "exceptions": [],
        }],
        "allowed_operations": [GET, CREATE, CANCEL],
        "execution_context": {
            "mode": "sandbox", "instance_id": "test-instance",
            "initial_state": {"products": [{"id": "product-001", "available_quantity": 10}], "reservations": []},
            "external_modifications_excluded": False,
        },
        "experiment": None, "planning_blocker": None,
        "evidence": [], "evaluation": "", "findings": [],
        "next_action": "planner", "iteration": 0,
    }


def plan_data():
    operations = [GET, CREATE, CANCEL, GET, CANCEL, GET]
    instructions = [
        "Read stock for product-001.", "Reserve 3 units of product-001; retain the returned ID.",
        "Cancel the returned reservation.", "Read stock after the first cancellation.",
        "Cancel that same reservation again.", "Read stock after the repeated cancellation.",
    ]
    expectations = ["Stock 10.", "ACTIVE reservation, quantity 3.", "CANCELLED reservation.",
                    "Stock 10.", "The same CANCELLED reservation.", "Stock still 10."]

    return {
        "specification_version": "v1",
        "hypothesis": "Repeating cancellation might restore stock twice.",
        "requirement_ids": ["REQ-CANCEL"], "preconditions": [],
        "steps": [{
            "id": f"s{i + 1}", "instruction": instruction, "operation": operation,
            "depends_on": [] if i == 0 else [f"s{i}"],
            "expected_observation": expected,
            "evidence_to_collect": ["HTTP status and returned state."],
        } for i, (operation, instruction, expected) in enumerate(zip(operations, instructions, expectations))],
        "stop_conditions": ["Both cancellation responses and stock reads around the repeat are recorded, or execution is blocked."],
    }


class ScriptedModel:
    def __init__(self, outputs):
        self.outputs = deque(outputs)
        self.inputs = []

    def with_structured_output(self, schema):
        self.schema = schema

        return self

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        context = json.loads(messages[-1][1])
        self.inputs.append(context)
        output = self.outputs.popleft()

        if callable(output):
            output = output(context)

        return self.schema.model_validate(output)

    async def ainvoke(self, messages):
        if hasattr(self, "schema"):
            return self.invoke(messages)

        self.inputs.append(deepcopy(messages))

        return self.outputs.popleft()


class TargetClient:
    def __init__(self):
        self.stock = 10
        self.reservation = None
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        status = 200

        if method == "GET" and path == "/products/product-001":
            body = {"id": "product-001", "available_quantity": self.stock}

        elif method == "POST" and path == "/reservations":
            quantity = kwargs["json_body"]["quantity"]
            self.stock -= quantity
            self.reservation = {"id": "r-1", "product_id": "product-001", "quantity": quantity, "status": "ACTIVE"}
            body = self.reservation
            status = 201

        elif method == "POST" and path == "/reservations/r-1/cancel":
            if self.reservation["status"] == "ACTIVE":
                self.stock += self.reservation["quantity"]
                self.reservation["status"] = "CANCELLED"

            body = self.reservation

        else:
            raise AssertionError(f"Unexpected request: {method} {path}")

        return {"status_code": status, "body": deepcopy(body)}


def request_message(index, operation):
    args = {"specification_version": "v1", **operation}

    if operation == GET:
        args["path_parameters"] = {"product_id": "product-001"}

    elif operation == CREATE:
        args["json_body"] = {"product_id": "product-001", "quantity": 3}

    else:
        args["path_parameters"] = {"reservation_id": "r-1"}

    return AIMessage(content="", tool_calls=[{
        "name": "request_application", "args": args, "id": f"call-{index}", "type": "tool_call",
    }])


def tools_for(state, client):
    catalog = RequirementCatalog.model_validate({
        "specification_version": state["specification_version"], "requirements": state["requirements"],
    })
    request = create_application_request(client, catalog, [
        OperationReference.model_validate(operation) for operation in state["allowed_operations"]
    ])

    return [StructuredTool.from_function(coroutine=request)]


def finding(context, next_action="end", outcome="no_violation_observed"):
    references = context["valid_evidence_references"]
    inconclusive = outcome == "inconclusive"

    return {
        "evaluation": "Inconclusive. Obtain the missing read." if inconclusive else "No violation observed in the recorded sequence.",
        "findings": [{
            "requirement_id": "REQ-CANCEL", "outcome": outcome,
            "scope": "The recorded product and reservation sequence.",
            "assessment": "Assess only matched observations in the controlled test.",
            "evidence": references,
            "limitations": ["The required observation is unavailable."] if inconclusive else [],
        }],
        "next_action": next_action,
    }


class ExperimentFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = initial_state()
        self.client = TargetClient()
        self.tools = tools_for(self.state, self.client)

    def graph(self, planner_model, executor_model, verifier_model, max_iterations=3):
        return create_graph(
            create_planner(planner_model), create_executor(executor_model, self.tools),
            create_verifier(verifier_model), max_iterations=max_iterations,
            checkpointer=InMemorySaver(),
        )

    async def test_full_graph_preserves_plan_evidence_context_and_checkpoint(self):
        planner = ScriptedModel([{"experiment": plan_data(), "planning_blocker": None}])
        executor = ScriptedModel([
            *[request_message(i, operation) for i, operation in enumerate([GET, CREATE, CANCEL, GET, CANCEL, GET])],
            AIMessage(content="Done."),
        ])
        verifier = ScriptedModel([finding])
        graph = self.graph(planner, executor, verifier)
        config = {"configurable": {"thread_id": str(uuid4())}}
        result = await graph.ainvoke(self.state, config=config)
        record = result["evidence"][0]
        self.assertEqual(self.client.stock, 10)
        self.assertEqual(len(self.client.calls), 6)
        self.assertEqual(result["iteration"], 1)
        self.assertEqual(record["experiment"], result["experiment"])
        self.assertEqual(record["experiment_id"], result["experiment"]["id"])
        self.assertEqual(record["instance_id"], "test-instance")
        self.assertEqual(record["stop_reason"], "model_finished")
        self.assertEqual(len(evidence_references(result)), 6)
        self.assertEqual(len(verifier.inputs[0]["valid_evidence_references"]), 6)
        self.assertEqual(planner.inputs[0]["execution_context"], self.state["execution_context"])
        self.assertEqual(planner.inputs[0]["allowed_operations"], self.state["allowed_operations"])
        snapshot = await graph.aget_state(config)
        self.assertEqual(snapshot.values["experiment"], result["experiment"])
        json.dumps(snapshot.values, allow_nan=False)
        result["experiment"]["steps"][0]["instruction"] = "Changed after execution."
        self.assertNotEqual(result["experiment"], record["experiment"])

    async def test_focused_followup_keeps_plan_identity_and_accumulates_evidence(self):
        planner = ScriptedModel([{"experiment": plan_data(), "planning_blocker": None}])
        executor = ScriptedModel([
            *[request_message(i, operation) for i, operation in enumerate([GET, CREATE, CANCEL, GET, CANCEL])],
            AIMessage(content="A final observation is still needed."),
            request_message(5, GET), AIMessage(content="Missing observation recorded."),
        ])
        verifier = ScriptedModel([
            lambda context: finding(context, "executor", "inconclusive"), finding,
        ])
        result = await self.graph(planner, executor, verifier).ainvoke(
            self.state, config={"configurable": {"thread_id": str(uuid4())}},
        )
        self.assertEqual(len(planner.inputs), 1)
        self.assertEqual(result["iteration"], 2)
        self.assertEqual(len(result["evidence"]), 2)
        first, second = result["evidence"]
        self.assertEqual(first["experiment_id"], second["experiment_id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(evidence_references(result)), 6)
        self.assertEqual(len(self.client.calls), 6)
        followup_context = json.loads(executor.inputs[-2][1].content)
        self.assertEqual(followup_context["evidence"][0]["id"], first["id"])
        self.assertIn("missing read", followup_context["evaluation"])

    async def test_replanning_gets_new_identity_without_rewriting_prior_evidence(self):
        second_plan = plan_data()
        second_plan["hypothesis"] = "A later read might reveal changed stock."
        planner = ScriptedModel([
            {"experiment": plan_data(), "planning_blocker": None},
            {"experiment": second_plan, "planning_blocker": None},
        ])
        executor = ScriptedModel([
            request_message(1, GET), AIMessage(content="Partial."),
            request_message(2, GET), AIMessage(content="Partial."),
        ])
        verifier = ScriptedModel([
            lambda context: finding(context, "planner", "inconclusive"),
            lambda context: finding(context, "end", "inconclusive"),
        ])
        result = await self.graph(planner, executor, verifier).ainvoke(
            self.state, config={"configurable": {"thread_id": str(uuid4())}},
        )
        first, second = result["evidence"]
        self.assertNotEqual(first["experiment_id"], second["experiment_id"])
        self.assertNotEqual(first["experiment"]["hypothesis"], second["experiment"]["hypothesis"])
        self.assertEqual(first["experiment"]["hypothesis"], plan_data()["hypothesis"])

    async def test_blocked_plan_calls_no_tools_and_reports_inconclusive(self):
        planner = ScriptedModel([{"experiment": None, "planning_blocker": "Required identity is unavailable."}])
        executor = ScriptedModel([])
        verifier = ScriptedModel([lambda context: finding(context, "end", "inconclusive")])
        result = await self.graph(planner, executor, verifier).ainvoke(
            self.state, config={"configurable": {"thread_id": str(uuid4())}},
        )
        self.assertFalse(executor.inputs)
        self.assertFalse(self.client.calls)
        self.assertIsNone(result["experiment"])
        self.assertEqual(result["evidence"][0]["stop_reason"], "empty_experiment")
        self.assertEqual(verifier.inputs[0]["planning_blocker"], result["planning_blocker"])

    async def test_unauthorized_checkpoint_fails_before_any_execution(self):
        self.state["experiment"] = Experiment(id=str(uuid4()), **plan_data()).model_dump(mode="json")
        self.state["allowed_operations"] = [GET]
        model = ScriptedModel([])
        executor = create_executor(model, self.tools)

        with self.assertRaisesRegex(ValueError, "unauthorized operation"):
            await executor(self.state)

        self.assertFalse(model.inputs)
        self.assertFalse(self.client.calls)

    async def test_old_live_plan_is_rejected_without_migrating_historical_evidence(self):
        self.state["experiment"] = ["Old textual step."]
        model = ScriptedModel([])

        with self.assertRaisesRegex(ValueError, "Legacy textual experiments"):
            await create_executor(model, self.tools)(self.state)

        old_record = {"id": str(uuid4()), "experiment": ["Old textual step."], "messages": []}
        original = deepcopy(old_record)
        projected = _evidence_for_verifier([old_record])[0]
        self.assertEqual(old_record, original)
        self.assertEqual(projected["experiment"], old_record["experiment"])
        self.assertNotIn("experiment_id", projected)

    async def test_call_limit_retains_unmatched_proposal_without_counting_it_as_evidence(self):
        self.state["experiment"] = Experiment(id=str(uuid4()), **plan_data()).model_dump(mode="json")
        model = ScriptedModel([request_message(1, GET), request_message(2, CREATE)])
        update = await create_executor(model, self.tools, max_tool_calls=1)(self.state)
        self.state["evidence"] = update["evidence"]
        self.assertEqual(update["evidence"][0]["stop_reason"], "tool_limit")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(evidence_references(self.state)), 1)
        self.assertEqual(update["evidence"][0]["messages"][-1]["tool_calls"][0]["id"], "call-2")

    async def test_timeout_retains_plan_without_inventing_completed_observation(self):
        self.state["experiment"] = Experiment(id=str(uuid4()), **plan_data()).model_dump(mode="json")

        class SlowModel(ScriptedModel):
            async def ainvoke(self, messages):
                await asyncio.Event().wait()

        update = await create_executor(SlowModel([]), self.tools, timeout_seconds=0.05)(self.state)
        record = update["evidence"][0]
        self.assertEqual(record["stop_reason"], "timeout")
        self.assertEqual(record["experiment_id"], self.state["experiment"]["id"])
        self.assertEqual(record["messages"], [])

    async def test_wrong_instance_is_rejected_before_verifier_model(self):
        self.state["evidence"] = [{
            "id": str(uuid4()), "source": "executor", "specification_version": "v1",
            "instance_id": "different-instance", "messages": [],
        }]
        model = ScriptedModel([])

        with self.assertRaisesRegex(ValueError, "current sandbox instance"):
            create_verifier(model)(self.state)

        self.assertFalse(model.inputs)

    async def test_verifier_rejects_fabricated_evidence_reference(self):
        def fabricated(context):
            result = finding(context)
            result["findings"][0]["evidence"] = [{"evidence_id": str(uuid4()), "tool_call_id": "invented"}]

            return result

        with self.assertRaisesRegex(ValueError, "no matching tool result"):
            create_verifier(ScriptedModel([fabricated]))(self.state)

    async def test_verifier_cannot_retry_an_absent_plan(self):
        model = ScriptedModel([lambda context: finding(context, "executor", "inconclusive")])

        with self.assertRaisesRegex(ValueError, "without an experiment"):
            create_verifier(model)(self.state)


class PlannerContractTests(unittest.TestCase):
    def test_plan_or_blocker_exclusivity(self):
        for value in [
            {"experiment": None, "planning_blocker": None},
            {"experiment": plan_data(), "planning_blocker": "Blocked."},
            {"experiment": None, "planning_blocker": "  "},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                PlannerOutput.model_validate(value)

    def test_planner_validates_scope_before_publishing_a_plan(self):
        state = initial_state()
        state["allowed_operations"] = [GET]
        model = ScriptedModel([{"experiment": plan_data(), "planning_blocker": None}])

        with self.assertRaisesRegex(ValueError, "unauthorized operation"):
            create_planner(model)(state)

        self.assertIsNone(state["experiment"])

    def test_structured_output_schema_has_no_model_assigned_uuid(self):
        schema = convert_to_openai_function(PlannerOutput, strict=True)["parameters"]
        plan_schema = next(part for part in schema["properties"]["experiment"]["anyOf"] if part.get("type") == "object")
        self.assertNotIn("id", plan_schema["properties"])
        self.assertFalse(plan_schema["additionalProperties"])

    def test_empty_verifier_evaluation_remains_invalid(self):
        value = finding({"valid_evidence_references": []}, outcome="inconclusive")
        value["evaluation"] = " \n "

        with self.assertRaises(ValueError):
            VerifierOutput.model_validate(value)


class ExperimentCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_initializes_scope_and_exports_the_structured_plan(self):
        # Isolate resource adapters: this test exercises the real CLI and graph,
        # without launching MCP, using credentials, or relying on a network port.
        state = initial_state()
        client = TargetClient()
        graph = create_graph(
            create_planner(ScriptedModel([{"experiment": plan_data(), "planning_blocker": None}])),
            create_executor(ScriptedModel([
                *[request_message(i, operation) for i, operation in enumerate([GET, CREATE, CANCEL, GET, CANCEL, GET])],
                AIMessage(content="Done."),
            ]), tools_for(state, client)),
            create_verifier(ScriptedModel([finding])), max_iterations=1,
        )
        captured = {}

        @asynccontextmanager
        async def open_application(server_args, **kwargs):
            captured["server_args"] = server_args
            kwargs["execution_context"].update(state["execution_context"])
            yield graph

        from testing_agent import cli

        catalog = RequirementCatalog.model_validate({
            "specification_version": "v1", "requirements": state["requirements"],
        })

        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                working_directory=Path(directory), runs_path=Path("var/runs"), objective=state["objective"],
                sandbox=True, seed_file=Path("unused-seed.json"), headers_file=None,
                settings=Path("unused-settings.json"), checkpoint_path=Path("unused-checkpoint.sqlite"),
            )
            output = StringIO()

            with (patch("testing_agent.runner.open_application", open_application),
                  redirect_stdout(output)):
                await cli._run(args, catalog, [OperationReference.model_validate(item) for item in state["allowed_operations"]])

            result = json.loads(output.getvalue())
            experiment = json.loads(Path(result["experiment_path"]).read_text(encoding="utf-8"))
            evidence = json.loads(Path(result["trace_path"]).read_text(encoding="utf-8"))
            self.assertEqual(experiment["id"], result["experiment_id"])
            self.assertEqual(experiment, evidence[0]["experiment"])
            self.assertEqual(result["iterations"], 1)
            self.assertEqual(captured["server_args"].count("--operation"), 3)
            context_path = Path(result["experiment_path"]).with_name("execution_context.json")
            self.assertEqual(json.loads(context_path.read_text())["instance_id"], "test-instance")


if __name__ == "__main__":
    unittest.main()
