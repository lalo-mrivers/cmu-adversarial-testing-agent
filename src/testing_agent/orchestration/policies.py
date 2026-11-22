import math
from time import monotonic

from testing_agent.orchestration.state import AgentState


class RunBudget:
    """Owned by one investigation. Charge synchronously before dispatch."""

    def __init__(self, max_calls: int = 10, seconds: float = 120):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("A positive integer call limit is required.")

        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("A finite positive time limit is required.")

        self.remaining = max_calls
        self.deadline = monotonic() + seconds
        self.pause: str | None = None
        self.user_stop = False

    def request_stop(self) -> None:
        """Ask the controller to refuse further operations; nothing is cancelled."""
        self.user_stop = True

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.deadline - monotonic())

    @property
    def stop(self) -> str | None:
        if self.pause:
            return self.pause

        if self.user_stop:
            return "user_stopped"

        if self.seconds_left == 0:
            return "timeout"

        if self.remaining == 0:
            return "tool_limit"

        return None

    def charge(self) -> str | None:
        reason = self.stop

        if reason is None:
            self.remaining -= 1

        return reason


def iteration_limit_reached(state: AgentState, max_iterations: int) -> bool:
    """Indicates whether the allowed number of Verifier reviews has been exhausted.
    
    Checks the state without modifying it. The limit is received from the
    configuration, and the counter represents already completed reviews.

    The counter corresponds to the number of Verifier reviews that
    have been completed, not the total number of iterations or tool calls.
    """

    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer.")

    iteration = state["iteration"]

    if type(iteration) is not int or iteration < 0:
        raise ValueError("iteration must be a non-negative integer.")

    return iteration >= max_iterations
