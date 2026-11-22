import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, model_validator

from testing_agent.domain.experiments import (
    Experiment,
    ExperimentPlan,
    validate_experiment_context,
)
from testing_agent.domain.requirements import (
    NonEmptyText,
    OperationReference,
    RequirementCatalog,
)
from testing_agent.orchestration.state import AgentState
from testing_agent.persistence.redaction import redact


class PlannerOutput(BaseModel):
    """Hypothesis and experiment proposed by the Planner."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    experiment: ExperimentPlan | None
    planning_blocker: NonEmptyText | None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if (self.experiment is None) == (self.planning_blocker is None):
            raise ValueError("Provide an experiment or a planning blocker, exclusively.")

        return self


def create_planner(
    model: BaseChatModel, *, asynchronous: bool = False,
) -> Callable[[AgentState], dict[str, Any] | Awaitable[dict[str, Any]]]:
    """Prepares the prompt and the model, and returns the planning node."""
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "planner.md"
    prompt = prompt_path.read_text(encoding="utf-8-sig")
    structured_model = model.with_structured_output(PlannerOutput)

    def messages(state: AgentState) -> list[tuple[str, str]]:
        context = {
            "objective": state["objective"],
            "specification_version": state["specification_version"],
            "requirements": state["requirements"],
            "allowed_operations": state["allowed_operations"],
            "execution_context": state.get("execution_context"),
            "experiment": state.get("experiment"),
            "planning_blocker": state.get("planning_blocker"),
            "evidence": state.get("evidence", []),
            "evaluation": state.get("evaluation", ""),
            "findings": state.get("findings", []),
        }

        return [
            ("system", prompt),
            ("human", json.dumps(context, ensure_ascii=False)),
        ]

    def decision(state: AgentState, result: Any) -> dict[str, Any]:
        result = PlannerOutput.model_validate(result)

        if result.experiment is None:
            return {"experiment": None, "planning_blocker": redact(result.planning_blocker)}

        validate_experiment_context(
            result.experiment,
            RequirementCatalog.model_validate({
                "specification_version": state["specification_version"],
                "requirements": state["requirements"],
            }),
            [OperationReference.model_validate(item) for item in state["allowed_operations"]],
        )
        experiment = Experiment(
            id=str(uuid4()),
            **result.experiment.model_dump(mode="json"),
        )

        return {"experiment": redact(experiment.model_dump(mode="json")), "planning_blocker": None}

    async def async_planner(state: AgentState):
        return decision(state, await structured_model.ainvoke(messages(state)))

    def planner(state: AgentState):
        return decision(state, structured_model.invoke(messages(state)))

    return async_planner if asynchronous else planner
