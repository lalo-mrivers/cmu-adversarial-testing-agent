from typing import Literal

from testing_agent.orchestration.policies import iteration_limit_reached
from testing_agent.orchestration.state import AgentState


def route_after_verifier(
    state: AgentState,
    max_iterations: int,
) -> Literal["planner", "executor", "end"]:
    """Applies the review limit before accepting the proposed action.
    
    Returns the flow destination without modifying the evaluation or state.
    
    - Limit reached: "end"
    - Limit not reached: returns the action proposed by the Verifier ["planner", "executor"].
    - Limit not reached and Verifier proposes "end": returns "end".
    """
    
    if state.get("review") or state["next_action"] == "review" or iteration_limit_reached(state, max_iterations):
        return "end"

    next_action = state["next_action"]

    if next_action not in ("planner", "executor", "end"):
        raise ValueError(f"Action proposed by the Verifier is not valid: {next_action!r}.")

    return next_action
