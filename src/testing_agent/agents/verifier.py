import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from testing_agent.domain.findings import Finding
from testing_agent.domain.requirements import NonEmptyText
from testing_agent.orchestration.state import AgentState, read_experiment
from testing_agent.persistence.redaction import redact


class RuleFragment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement_id: NonEmptyText
    quote: NonEmptyText


class InterpretationReview(BaseModel):
    """The reviewer receives exact clauses and the applicability/precedence checks."""
    model_config = ConfigDict(extra="forbid", strict=True)
    question: NonEmptyText
    fragments: list[RuleFragment] = Field(min_length=1)
    applicable_conditions: NonEmptyText
    exception_and_precedence_checks: NonEmptyText


class VerifierOutput(BaseModel):
    """Assessment of the evidence and recommended next action."""

    model_config = ConfigDict(extra="forbid", strict=True)

    evaluation: NonEmptyText = Field(
        description="Summarize the findings, limitations and recommended next step."
    )
    findings: list[Finding] = Field(
        min_length=1,
        description="Current findings for the requirements assessed. Reassess all "
        "available evidence; this list replaces previous findings. Use inconclusive "
        "when observations are insufficient and explain what is missing."
    )
    next_action: Literal["planner", "executor", "end", "review"]
    review: InterpretationReview | None = None


def evidence_references(
    state: AgentState, *, operations: set[tuple[str, str]] | None = None,
) -> set[tuple[str, str]]:
    """Indexa llamadas con un resultado posterior dentro del mismo registro."""
    references: set[tuple[str, str]] = set()
    record_ids: set[str] = set()
    execution_context = state.get("execution_context") or {}
    expected_instance_id = execution_context.get("instance_id")

    for record in state.get("evidence", []):
        record_id = record["id"]

        if record_id in record_ids:
            raise ValueError(f"Duplicate evidence record ID: {record_id}")

        record_ids.add(record_id)

        if (
            record.get("source") != "executor"
            or record.get("specification_version") != state["specification_version"]
        ):
            continue

        if (
            expected_instance_id is not None
            and record.get("instance_id") != expected_instance_id
        ):
            raise ValueError(
                f"Evidence record {record_id} does not belong to "
                f"the current sandbox instance: expected "
                f"{expected_instance_id!r}, got "
                f"{record.get('instance_id')!r}."
            )
        
        calls: dict[str, tuple[int, str]] = {}
        results: dict[str, tuple[int, str | None]] = {}
        decisions = {item["tool_call_id"]: item["decision"] for item in record.get("decisions", [])}

        for position, message in enumerate(record.get("messages", [])):
            if message.get("type") == "ai":
                for call in message.get("tool_calls", []):
                    args = call.get("args", {})

                    if operations is not None and (
                        call["name"] != "request_application" or
                        not isinstance(args.get("method"), str) or not isinstance(args.get("path"), str) or
                        (args.get("method"), args.get("path")) not in operations
                    ):
                        continue

                    call_id = call["id"]

                    if call_id in calls:
                        raise ValueError(
                            f"Duplicate tool call ID in {record_id}: {call_id}"
                        )

                    calls[call_id] = (position, call["name"])

            elif message.get("type") == "tool":
                call_id = message["tool_call_id"]

                if operations is not None and (
                    message.get("name") != "request_application" or message.get("status") == "error"
                    or decisions.get(call_id, "executed" if "decisions" not in record else None) != "executed"
                ):
                    continue

                if call_id in results:
                    raise ValueError(
                        f"Duplicate tool result in {record_id}: {call_id}"
                    )

                results[call_id] = (position, message.get("name"))

        for call_id, (call_position, call_name) in calls.items():
            result = results.get(call_id)

            if result is None:
                continue

            result_position, result_name = result

            if result_position > call_position and result_name == call_name:
                references.add((record_id, call_id))

    return references


def _evidence_for_verifier(
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Preserves tool observations and order without model-internal content.

    This view is only for the Verifier input. The stored trace and the raw
    evidence used for reference validation remain unchanged.
    """
    records: list[dict[str, Any]] = []

    for record_index, record in enumerate(evidence):
        projected = deepcopy({
            key: value for key, value in record.items() if key != "messages"
        })
        projected["record_index"] = record_index
        messages: list[dict[str, Any]] = []

        for message_index, message in enumerate(record.get("messages", [])):
            if message.get("type") == "tool":
                # Keep content, artifacts, status and IDs exactly as recorded.
                observation = deepcopy(message)

            elif message.get("type") == "ai":
                calls = message.get("tool_calls", [])
                invalid_calls = message.get("invalid_tool_calls", [])

                if not calls and not invalid_calls:
                    continue

                observation = {
                    "type": "ai",
                    "tool_calls": deepcopy(calls),
                    "invalid_tool_calls": deepcopy(invalid_calls),
                }

            else:
                continue

            observation["message_index"] = message_index
            messages.append(observation)

        projected["messages"] = messages
        records.append(projected)

    return records


def _validate_references(result: VerifierOutput, state: AgentState) -> None:
    """Comprueba identidad y correspondencia; no demuestra la conclusión."""
    rules = {requirement["id"]: requirement for requirement in state["requirements"]}
    references = evidence_references(state)

    for finding in result.findings:
        if finding.specification_version not in (None, state["specification_version"]):
            raise ValueError("The finding belongs to a different specification version.")

        if finding.requirement_id not in rules:
            raise ValueError(
                f"Unknown requirement ID: {finding.requirement_id}"
            )

        for reference in finding.evidence:
            pair = (reference.evidence_id, reference.tool_call_id)

            if pair not in references:
                raise ValueError(
                    f"Evidence reference has no matching tool result: {pair}"
                )

        if finding.outcome != "inconclusive":
            operations = {(op["method"], op["path"]) for op in rules[finding.requirement_id]["operations"]}
            observations = evidence_references(state, operations=operations)

            if not any((ref.evidence_id, ref.tool_call_id) in observations for ref in finding.evidence):
                raise ValueError("A conclusive finding requires an observed response for the requirement.")


def create_verifier(
    model: BaseChatModel, *, asynchronous: bool = False,
) -> Callable[[AgentState], dict[str, Any] | Awaitable[dict[str, Any]]]:
    """Prepares the prompt and the model, and returns the verification node."""
    
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "verifier.md"
    prompt = prompt_path.read_text(encoding="utf-8-sig")
    structured_model = model.with_structured_output(VerifierOutput)

    def messages(state: AgentState) -> list[tuple[str, str]]:
        experiment = read_experiment(state)
        context = {
            "objective": state["objective"],
            "specification_version": state["specification_version"],
            "execution_context": state.get("execution_context"),
            "requirements": state["requirements"],
            "allowed_operations": state["allowed_operations"],
            "experiment": experiment.model_dump(mode="json") if experiment is not None else None,
            "planning_blocker": state.get("planning_blocker"),
            "evidence": _evidence_for_verifier(state.get("evidence", [])),
            "valid_evidence_references": [
                {
                    "evidence_id": evidence_id,
                    "tool_call_id": tool_call_id,
                }
                for evidence_id, tool_call_id in sorted(
                    evidence_references(state)
                )
            ],
            "evaluation": state.get("evaluation", ""),
            "findings": state.get("findings", []),
        }

        return [
            ("system", prompt),
            ("human", json.dumps(context, ensure_ascii=False)),
        ]

    def decision(state: AgentState, result: Any) -> dict[str, Any]:
        #if not result.evaluation:
        #    raise ValueError("The evaluation of the Verifier cannot be empty.")
        result = VerifierOutput.model_validate(result)
        _validate_references(result, state)
        
        if result.next_action == "executor" and state["experiment"] is None:
            raise ValueError("The Verifier cannot request execution without an experiment.")

        output = result.model_dump(mode="json")

        if (result.next_action == "review") != (result.review is not None):
            raise ValueError("A review action requires a structured interpretation request.")

        if result.review:
            rules = {rule["id"]: rule for rule in state["requirements"]}
            affected = set()

            for fragment in result.review.fragments:
                rule = rules.get(fragment.requirement_id)

                if not rule:
                    raise ValueError("Review refers to an unknown requirement.")

                clauses = [rule["expected_behavior"], rule["description"], *rule["preconditions"]]

                for exception in rule["exceptions"]:
                    clauses.extend([exception["condition"], exception["expected_behavior"]])

                if not any(fragment.quote in clause for clause in clauses):
                    raise ValueError("Review fragment does not match the versioned requirement.")

                affected.add(fragment.requirement_id)

            for finding in output["findings"]:
                if finding["requirement_id"] in affected:
                    finding.update(outcome="inconclusive", assessment=result.review.question,
                                   limitations=[result.review.question])

            output["evaluation"] = result.review.question

        for finding in output["findings"]:
            finding["specification_version"] = state["specification_version"]

        return redact({
            **output,
            "iteration": state["iteration"] + 1,
        })

    async def async_verifier(state: AgentState):
        return decision(state, await structured_model.ainvoke(messages(state)))

    def verifier(state: AgentState):
        return decision(state, structured_model.invoke(messages(state)))

    return async_verifier if asynchronous else verifier
