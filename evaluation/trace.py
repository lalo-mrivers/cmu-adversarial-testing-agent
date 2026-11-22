import json
from typing import Any


def result_of(message: dict[str, Any]) -> dict[str, Any]:
    """The structured result of a tool message, wherever the transport put it."""
    artifact = message.get("artifact")

    if isinstance(artifact, dict) and isinstance(artifact.get("structuredContent"), dict):
        return artifact["structuredContent"]

    content = message.get("content")

    if isinstance(content, list):
        content = "".join(block.get("text", "") for block in content if isinstance(block, dict))

    try:
        value = json.loads(content)

    except (TypeError, ValueError):
        return {}

    return value if isinstance(value, dict) else {}


def calls_in(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every proposed call, in order, with its decision and, if executed, its response.

    `ordinal` numbers the executed calls from 1. It matches the order in which requests reach the
    application, because the application is only reachable through these calls.
    """
    calls: list[dict[str, Any]] = []

    for record in evidence:
        if record.get("source") != "executor":
            continue

        decisions = {item["tool_call_id"]: item for item in record.get("decisions", [])}
        results = {m["tool_call_id"]: m for m in record.get("messages", []) if m.get("type") == "tool"}

        for message in record.get("messages", []):
            if message.get("type") != "ai":
                continue

            for call in message.get("tool_calls", []):
                decision = decisions.get(call["id"], {})
                result = result_of(results[call["id"]]) if call["id"] in results else {}
                response = result.get("response") if isinstance(result.get("response"), dict) else None
                executed = decision.get("decision") == "executed" and response is not None
                calls.append({
                    "record_id": record["id"], "call_id": call["id"], "args": call.get("args", {}),
                    "decision": decision.get("decision"), "reason": decision.get("reason"),
                    "executed": executed, "response": response if executed else None,
                })

    ordinal = 0

    for call in calls:
        if call["executed"]:
            ordinal += 1
            call["ordinal"] = ordinal

    return calls
