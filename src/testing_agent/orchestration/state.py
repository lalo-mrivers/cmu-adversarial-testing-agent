from operator import add
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from testing_agent.domain.experiments import Experiment, validate_experiment_context
from testing_agent.domain.requirements import OperationReference, RequirementCatalog


class AgentState(TypedDict):
    """Information about an investigation and its progress within the graph."""

    ## Research context.
    objective: str
    specification_version: str
    requirements: list[dict[str, Any]]
    allowed_operations: list[dict[str, str]]

    # Contexto generado por el sistema para la instancia investigada.
    execution_context: NotRequired[dict[str, Any]]

    ## Current hypothesis and experiment, defined by the Planner.
    # Experiment.model_dump(mode="json"); None when there is no testable hypothesis.
    experiment: dict[str, Any] | None
    planning_blocker: str | None

    ## Operations and results: each update adds new records.
    evidence: Annotated[list[dict[str, Any]], add]

    ## Evaluation and next step proposed by the Verifier.
    evaluation: str
    
    ##Finding.model_dump(mode="json"):   #cada actualizacion sustituye la lista
    ##opcional para admitir estados iniciales y checkpoints anteriores
    findings: NotRequired[list[dict[str, Any]]]
    next_action: Literal["planner", "executor", "end", "review"]
    review: NotRequired[dict[str, Any] | None]
    stop_reason: NotRequired[str]
    case_outcome: NotRequired[Literal["inconclusive"]]

    # Completed Verifier reviews; starts at zero.
    iteration: int


def read_experiment(state: AgentState) -> Experiment | None:
    """Revalidate a serialized plan at agent boundaries, including checkpoints."""
    value = state["experiment"]

    if value is None:
        return None

    if isinstance(value, list):
        raise ValueError(
            "Legacy textual experiments cannot be executed. Start a new investigation; "
            "historical evidence records remain readable."
        )

    experiment = Experiment.model_validate(value)
    validate_experiment_context(
        experiment,
        RequirementCatalog.model_validate({
            "specification_version": state["specification_version"],
            "requirements": state["requirements"],
        }),
        [OperationReference.model_validate(item) for item in state["allowed_operations"]],
    )

    return experiment
