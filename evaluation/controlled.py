import json
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from evaluation.cases import OPERATIONS, judge, request_arguments
from evaluation.trace import calls_in, result_of


def _expectation_text(expect: dict[str, Any]) -> str:
    parts = [f"HTTP {expect['status']}"]
    parts += [f"{pointer} as specified" for pointer in expect.get("json", {})]

    return ", ".join(parts)


def plan_for(case: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """An experiment that walks the case's operations and nothing else."""
    rules = {rule["id"]: rule for rule in context["requirements"]}
    operations = [OPERATIONS[step["call"]] for step in case["steps"]]
    selected = [case["requirement"]]
    covered = {(op["method"], op["path"]) for op in rules[case["requirement"]]["operations"]}

    for rule_id, rule in rules.items():
        pending = {op for op in operations if op not in covered}

        if not pending:
            break

        gained = {(op["method"], op["path"]) for op in rule["operations"]} & pending

        if gained and rule_id not in selected:
            selected.append(rule_id)
            covered |= gained

    preconditions = [{"requirement_id": rule_id, "condition": condition,
                      "verification": "Not verifiable with the available tools."}
                     for rule_id in selected for condition in rules[rule_id]["preconditions"]]
    steps = []

    for index, step in enumerate(case["steps"]):
        method, path = OPERATIONS[step["call"]]
        steps.append({
            "id": f"s{index + 1}", "instruction": f"Send {method} {path}.",
            "operation": {"method": method, "path": path},
            "depends_on": [f"s{index}"] if index else [],
            "expected_observation": _expectation_text(step["expect"]),
            "evidence_to_collect": ["HTTP status and response body."],
        })

    return {"planning_blocker": None, "experiment": {
        "specification_version": context["specification_version"], "hypothesis": case["objective"],
        "requirement_ids": selected, "preconditions": preconditions, "steps": steps,
        "stop_conditions": ["Every step is executed, or execution is blocked."],
    }}


class _Structured:
    """Stands in for a chat model used through `with_structured_output`."""

    def with_structured_output(self, schema):
        self.schema = schema

        return self

    def respond(self, context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def ainvoke(self, messages):
        return self.schema.model_validate(self.respond(json.loads(messages[-1][1])))


class ScriptedPlanner(_Structured):
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def respond(self, context):
        return plan_for(self.case, context)


class ScriptedExecutor:
    """Proposes the case's steps, then its forbidden proposals, one call per turn."""

    def __init__(self, case: dict[str, Any], version: str) -> None:
        self.case = case
        self.version = version
        self.proposals: list[tuple[str, dict[str, Any]]] = [("step", step) for step in case["steps"]]

        for arguments in case["forbidden"]:
            filled = {key: (self.version if value == "$version" else value) for key, value in arguments.items()}
            self.proposals.append(("forbidden", filled))

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        answered = [m for m in messages if isinstance(m, ToolMessage)]
        position = len(answered)

        if position >= len(self.proposals):
            return AIMessage(content="Done.")

        refs: dict[str, str] = {}

        for (kind, step), message in zip(self.proposals, answered):
            if kind == "step" and "save" in step:
                response = result_of(message.model_dump(mode="json")).get("response") or {}

                try:
                    document = json.loads(response.get("body", ""))

                except ValueError:
                    continue

                if isinstance(document, dict) and isinstance(document.get("id"), str):
                    refs[step["save"]] = document["id"]

        kind, item = self.proposals[position]
        arguments = request_arguments(item, refs, self.version) if kind == "step" else item

        return AIMessage(content="", tool_calls=[{
            "id": f"call-{position}", "name": "request_application", "args": arguments, "type": "tool_call",
        }])


class ScriptedVerifier(_Structured):
    """Compares what was observed with what the requirements demand."""

    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def respond(self, context):
        case = self.case
        rule = next(item for item in context["requirements"] if item["id"] == case["requirement"])
        rule_operations = {(op["method"], op["path"]) for op in rule["operations"]}
        calls = [call for call in calls_in(context["evidence"]) if call["executed"]]
        steps = len(case["steps"])
        responses: list[dict[str, Any] | None] = [None] * steps
        by_step: dict[int, dict[str, Any]] = {}

        for call in calls:
            index = int(call["call_id"].rsplit("-", 1)[1])

            if index < steps:
                responses[index] = call["response"]
                by_step[index] = call

        verdict = judge(case, responses)

        def cite(selected):
            return [{"evidence_id": call["record_id"], "tool_call_id": call["call_id"]} for call in selected]

        relevant = [call for call in by_step.values()
                    if (call["args"].get("method"), call["args"].get("path")) in rule_operations]
        review = case.get("review")
        override = case.get("controlled_verdict")

        if review:
            outcome, assessment, limits = "inconclusive", review["question"], [review["question"]]

        elif override:
            outcome, assessment, limits = override["outcome"], override["limitation"], [override["limitation"]]

        elif verdict["outcome"] == "violation":
            first = verdict["mismatches"][0]
            outcome = "violation"
            assessment = f"Step {first['step'] + 1}: " + "; ".join(first["problems"])
            limits = []

        elif verdict["outcome"] == "inconclusive":
            outcome = "inconclusive"
            assessment = f"Steps {[i + 1 for i in verdict['missing']]} produced no response."
            limits = [assessment]

        else:
            outcome, assessment, limits = "no_violation_observed", "Every observed response matched the requirements.", []

        if outcome == "violation":
            cited = {by_step[item["step"]]["call_id"]: by_step[item["step"]] for item in verdict["mismatches"][:1]}

            if relevant:
                cited.setdefault(relevant[-1]["call_id"], relevant[-1])

            references = cite(cited.values())

        else:
            references = cite(relevant)

        return {
            "evaluation": assessment, "next_action": "review" if review else "end", "review": review,
            "findings": [{"requirement_id": case["requirement"], "scope": "The recorded sequence in this sandbox instance.",
                          "outcome": outcome, "assessment": assessment, "limitations": limits,
                          "evidence": references}],
        }
