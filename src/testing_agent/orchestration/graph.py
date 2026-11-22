import asyncio
import inspect
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from testing_agent.orchestration.policies import RunBudget, iteration_limit_reached
from testing_agent.orchestration.routing import route_after_verifier
from testing_agent.orchestration.state import AgentState
from testing_agent.domain.experiments import OperationOutsideScope
from testing_agent.persistence.redaction import redact


AgentNode = Callable[[AgentState], dict[str, Any] | Awaitable[dict[str, Any]]]


def create_graph(
    planner: AgentNode,
    executor: AgentNode,
    verifier: AgentNode,
    *,
    max_iterations: int,
    checkpointer: BaseCheckpointSaver | None = None,
    budget: RunBudget | None = None,
) -> CompiledStateGraph:
    """Connects the prepared agents and returns the compiled graph.
    
    Uses ainvoke or astream to support the asynchronous Executor.
    The checkpointer, when provided, must support asynchronous execution.
    """

    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer.")

    def route_start(state: AgentState) -> Literal["planner", "end"]:
        # To avoid an infinite loop, the first iteration is always routed to the Planner.
        if state.get("review") or state.get("next_action") == "review" or iteration_limit_reached(state, max_iterations):
            return "end"

        return "planner"

    # Create the graph with three nodes: planner, executor and verifier
    builder = StateGraph(AgentState)

    async def bounded(node: AgentNode, state: AgentState):
        if budget and budget.seconds_left <= 0:
            raise TimeoutError

        async with asyncio.timeout(budget.seconds_left if budget else None):
            if inspect.iscoroutinefunction(node):
                return await node(state)

            result = await asyncio.to_thread(node, state)

            return await result if inspect.isawaitable(result) else result

    async def plan(state):
        try:
            return await bounded(planner, state)

        except OperationOutsideScope as error:
            return {"experiment": None, "planning_blocker": redact(str(error))}

        except TimeoutError:
            return {"experiment": None, "planning_blocker": "Investigation deadline reached."}

    async def assess(state):
        latest = (state.get("evidence") or [{}])[-1]
        reason = latest.get("stop_reason")
        unsafe = (budget.pause if budget else None) or (
            reason if reason in ("containment_lost", "recovery_required") else None
        )

        if not unsafe:
            try:
                review = await bounded(verifier, state)

            except TimeoutError:
                reason = "timeout"

            else:
                if review.get("review"):
                    return {**review, "stop_reason": "interpretation_review"}

                limit = (budget.stop if budget else None) or (
                    reason if reason in ("tool_limit", "timeout") else None
                )

                if not limit and review["iteration"] >= max_iterations and review["next_action"] != "end":
                    limit = "iteration_limit"

                if limit:
                    if review["next_action"] != "end":
                        review["case_outcome"] = "inconclusive"
                        review["evaluation"] += f" Further verification was unavailable ({limit})."

                    review.update(next_action="end", stop_reason=limit)

                return review

        limitation = unsafe or reason
        prior = state.get("findings", [])
        assessed = {finding["requirement_id"] for finding in prior}

        return {
            "iteration": state["iteration"] + 1, "stop_reason": limitation,
            "case_outcome": "inconclusive",
            "next_action": "review" if unsafe else "end",
            "review": {"reason": unsafe} if unsafe else None,
            "evaluation": f"Inconclusive. Investigation stopped: {limitation}.",
            "findings": [*prior, *[{
                "requirement_id": req["id"], "specification_version": state["specification_version"],
                "scope": "Current investigation", "outcome": "inconclusive",
                "assessment": "A verified assessment is unavailable.", "evidence": [],
                "limitations": [limitation],
            } for req in state["requirements"] if req["id"] not in assessed]],
        }

    builder.add_node("planner", plan)
    builder.add_node("executor", executor)
    builder.add_node("verifier", assess)

    # Define the flow of the graph (edges)
    builder.add_conditional_edges(
        START,
        route_start,
        {"planner": "planner", "end": END},
    )
    builder.add_edge("planner", "executor")
    builder.add_edge("executor", "verifier")
    builder.add_conditional_edges(
        "verifier",
        partial(route_after_verifier, max_iterations=max_iterations),
        {"planner": "planner", "executor": "executor", "end": END},
    )
    
    # Each review requires, at most, three nodes of the main graph.
    return builder.compile(checkpointer=checkpointer).with_config(
        recursion_limit=3 * max_iterations + 1, # Maximum number of recursive calls to the model
    )
