import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from testing_agent.domain.requirements import RequirementCatalog, OperationReference
from testing_agent.orchestration.policies import RunBudget
from testing_agent.persistence.redaction import redact
from testing_agent.tools.application import ApplicationScope
from testing_agent.orchestration.state import AgentState, read_experiment


def _result_decision(message: ToolMessage, limits: RunBudget) -> dict[str, str]:
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    result = artifact.get("structuredContent")

    if result is None:
        try:
            result = json.loads(message.content)

        except (TypeError, ValueError):
            result = {}

    if not isinstance(result, dict):
        result = {}

    decision = result.get("decision")

    if decision in ("blocked", "unknown"):
        reason = str(result.get("reason", "Missing reason"))

        if decision == "unknown" or reason in ("containment_lost", "recovery_required"):
            limits.pause = "containment_lost" if reason == "containment_lost" else "recovery_required"

        elif reason == "tool_limit":
            limits.remaining = 0

        elif reason == "timeout":
            limits.deadline = 0

        return {"decision": decision, "reason": reason}

    if message.name == "request_application":
        response = result.get("response")

        if message.status == "error" or not isinstance(response, dict) or type(response.get("status_code")) is not int:
            limits.pause = "recovery_required"

            return {"decision": "unknown", "reason": limits.pause}

    return {"decision": "error" if message.status == "error" else "executed"}


def create_executor(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    max_tool_calls: int = 10,
    timeout_seconds: float = 120,
    budget: RunBudget | None = None,
    application_scope: ApplicationScope | None = None,
) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    """Prepares the Executor with authorized tools and execution limits."""
    
    limits = budget or RunBudget(max_tool_calls, timeout_seconds)
    
    if not tools or len({tool.name for tool in tools}) != len(tools):
        raise ValueError("Unique tool names are required.")

    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "executor.md"
    prompt = prompt_path.read_text(encoding="utf-8-sig")
    model_with_tools = model.bind_tools(tools)

    tool_node = ToolNode(tools, handle_tool_errors=True)

    def stop_reason(messages: list[BaseMessage]) -> str:
        """Determines the reason for stopping the execution based on the last message of the model.
        Returns an empty string if the model should continue execution.
        
        Possible return values:
        - "invalid_tool_call": The model produced a malformed tool call.
        - "multiple_tool_calls": The model attempted to call multiple tools in a single message.
        - "tool_limit": The model has reached the maximum number of tool calls allowed.
        - "model_finished": The model has finished execution (no tool calls).
        - "": The model should continue execution.
        
        """
        last = messages[-1]
        
        if not isinstance(last, AIMessage):
            raise ValueError("A response from the model was expected.")
        
        if last.invalid_tool_calls:
            return "invalid_tool_call"
        
        if not last.tool_calls:
            return "model_finished"
        
        if len(last.tool_calls) != 1:
            return "multiple_tool_calls"
        
        return limits.stop or ""

    async def call_model(local_state: MessagesState) -> dict[str, Any]:
        history = [
            type(message).model_validate(redact(message.model_dump(mode="json")))
            for message in local_state["messages"]
        ]
        response = await model_with_tools.ainvoke(
            [SystemMessage(content=prompt), *history]
        )

        return {"messages": [response]}

    def route(local_state: MessagesState) -> str:
        """Determines the next node based on the last message of the model.
        
        - END: The model has finished execution or has reached a stopping condition.
        - "tools": The model has called a tool and should proceed to the tools node.
        """

        return END if stop_reason(local_state["messages"]) else "tools"

    async def executor(state: AgentState) -> dict[str, Any]:
        # Fail before model/tool use if a plan or checkpoint violates the contract.
        experiment = read_experiment(state)
        plan = experiment.model_dump(mode="json") if experiment is not None else None
        record: dict[str, Any] = {
            "id": str(uuid4()),
            "source": "executor",
            "specification_version": state["specification_version"],
            "instance_id": (
                state.get("execution_context") or {}
            ).get("instance_id"),
            "experiment_id": experiment.id if experiment is not None else None,
            # Retain the historical record field, derived from the single plan.
            "hypothesis": experiment.hypothesis if experiment is not None else "",
            "experiment": plan,
            "planning_blocker": state.get("planning_blocker"),
            "decisions": [],
            "messages": [],
            "stop_reason": "empty_experiment",
        }

        if experiment is None:
            return {"evidence": [record]}

        if limits.stop:
            record["stop_reason"] = limits.stop

            return {"evidence": [record]}

        scope = application_scope or ApplicationScope(RequirementCatalog.model_validate({
            "specification_version": state["specification_version"], "requirements": state["requirements"],
        }), [OperationReference.model_validate(item) for item in state["allowed_operations"]])

        async def dispatch(local_state: MessagesState, config: RunnableConfig):
            call = local_state["messages"][-1].tool_calls[0]
            name, arguments = call["name"], call["args"]
            event = {"tool_call_id": call["id"], "decision": "proposed"}
            record["decisions"].append(event)
            reason = limits.charge()

            if not reason and name == "request_application":
                try:
                    scope.resolve(**arguments)

                except (ValueError, TypeError, PermissionError, AttributeError) as error:
                    reason = redact(str(error))

            if not reason and name not in {tool.name for tool in tools}:
                reason = "Tool outside the authorized catalog."

            if reason:
                event.update(controller="blocked", decision="blocked", reason=reason)
                message = ToolMessage(name=name, tool_call_id=call["id"], status="error",
                                      content=json.dumps(event))

            else:
                event.update(controller="allowed", decision="dispatched")

                try:
                    update = await tool_node.ainvoke(local_state, config)
                    message = update["messages"][0]
                    event.update(_result_decision(message, limits))

                except BaseException:
                    event["decision"] = "unknown"

                    if name == "request_application":
                        limits.pause = "recovery_required"

                    raise

            return {"messages": [message.model_copy(update={
                "content": redact(message.content), "artifact": redact(message.artifact),
            })]}

        builder = StateGraph(MessagesState)
        builder.add_node("model", call_model)
        builder.add_node("tools", dispatch)
        builder.add_edge(START, "model")
        builder.add_conditional_edges("model", route, {"tools": "tools", END: END})
        builder.add_conditional_edges("tools", lambda _: END if limits.pause else "model",
                                      {"model": "model", END: END})
        # Inner proposals may contain secrets. Only checkpoint the sanitized outer record.
        agent = builder.compile(checkpointer=False)

        context = {
            "objective": state["objective"],
            "specification_version": state["specification_version"],
            "execution_context": state.get("execution_context"),
            "requirements": state["requirements"],
            "allowed_operations": state["allowed_operations"],
            "experiment": plan,
            "evidence": state.get("evidence", []),
            "evaluation": state.get("evaluation", ""),
            "findings": state.get("findings", []),
            "max_tool_calls": limits.remaining,
            "timeout_seconds": limits.seconds_left,
        }
        messages: list[BaseMessage] = [
            HumanMessage(content=json.dumps(context, ensure_ascii=False))
        ]

        try:
            async with asyncio.timeout(limits.seconds_left):
                async for snapshot in agent.astream(
                    {"messages": messages},
                    config={"recursion_limit": 2 * limits.remaining + 3},
                    stream_mode="values",
                ):
                    messages = snapshot["messages"]
            
            record["stop_reason"] = limits.pause or stop_reason(messages)
        
        except TimeoutError:
            record["stop_reason"] = limits.pause or "timeout"
            record["error"] = "Time limit reached; pending operations may have an unknown outcome."
        
        except Exception as error:
            record["stop_reason"] = limits.pause or "execution_error"
            record["error"] = {"type": type(error).__name__, "message": str(error)}

        #  Avoid duplicating evidence in the record; only include messages from this execution.
        record["messages"] = [message.model_dump(mode="json") for message in messages[1:]]

        if isinstance(messages[-1], AIMessage) and record["stop_reason"] in (
            "tool_limit", "multiple_tool_calls", "invalid_tool_call", "timeout",
        ):
            dispatched = {event["tool_call_id"] for event in record["decisions"]}

            for call in messages[-1].tool_calls:
                if call["id"] not in dispatched:
                    record["decisions"].append({"tool_call_id": call["id"], "controller": "blocked",
                                                "decision": "blocked", "reason": record["stop_reason"]})

        return {"evidence": [redact(record)]}

    return executor
