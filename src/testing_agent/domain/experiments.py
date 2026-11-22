from collections.abc import Sequence
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from testing_agent.domain.requirements import (
    NonEmptyText,
    OperationReference,
    RequirementCatalog,
)


StepId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExperimentPrecondition(_StrictModel):
    """A Requirement condition and the proposed way to establish it.

    `verification` describes a plan or a known capability gap, not a claim
    that the condition has been satisfied. Test-specific setup goes in steps.
    """

    requirement_id: NonEmptyText
    condition: NonEmptyText = Field(description="Exact precondition from the requirement.")
    verification: NonEmptyText = Field(
        description="How to establish this condition, or why available capabilities cannot."
    )


class ExperimentStep(_StrictModel):
    """One ordered action with observable expectations, not an HTTP script.

    The Executor resolves concrete data from observations through its tools.
    Dependencies identify earlier steps whose observations this step needs.
    A null operation permits only documentation/context checks or comparisons.
    """

    id: StepId
    instruction: NonEmptyText
    operation: OperationReference | None = Field(
        description="Exact authorized specification method/path for one application "
        "request, or null for a check that sends no application request."
    )
    depends_on: list[StepId]
    expected_observation: NonEmptyText
    evidence_to_collect: list[NonEmptyText] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lists(self) -> Self:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(f"Step {self.id!r} has duplicate dependencies.")

        if len(set(self.evidence_to_collect)) != len(self.evidence_to_collect):
            raise ValueError(f"Step {self.id!r} has duplicate evidence descriptions.")

        return self


class ExperimentPlan(_StrictModel):
    """Planner output before the application assigns its identity."""
    #Note: planned conditions are never observations.

    specification_version: NonEmptyText
    hypothesis: NonEmptyText
    requirement_ids: list[NonEmptyText] = Field(min_length=1)
    preconditions: list[ExperimentPrecondition]
    steps: list[ExperimentStep] = Field(min_length=1)
    stop_conditions: list[NonEmptyText] = Field(
        min_length=1,
        description="Observable completion criteria and conditions that block the "
        "sequence. Runtime call/time/iteration limits remain enforced separately."
    )

    @model_validator(mode="after")
    def validate_structure(self) -> Self:
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("Experiment requirement IDs must be unique.")
        
        if len(set(self.stop_conditions)) != len(self.stop_conditions):
            raise ValueError("Experiment stop conditions must be unique.")

        conditions: set[tuple[str, str]] = set()

        for precondition in self.preconditions:
            if precondition.requirement_id not in self.requirement_ids:
                raise ValueError("A precondition refers to an unselected requirement.")
            
            key = (precondition.requirement_id, precondition.condition)
            
            if key in conditions:
                raise ValueError("Duplicate experiment precondition.")

            conditions.add(key)

        previous: set[str] = set()

        for step in self.steps:
            if step.id in previous:
                raise ValueError(f"Duplicate step ID: {step.id!r}.")
            
            # Earlier-only references reject missing, self, forward and cyclic edges.
            
            if not set(step.depends_on).issubset(previous):
                raise ValueError(f"Dependencies of step {step.id!r} must refer to earlier steps.")
            
            previous.add(step.id)
        
        return self


class Experiment(ExperimentPlan):
    """Validated plan with a UUID assigned by the application, never the LLM."""

    id: NonEmptyText

    @model_validator(mode="after")
    def validate_id(self) -> Self:
        if str(UUID(self.id)) != self.id:
            raise ValueError("Experiment ID must be a canonical UUID.")
        
        return self


class OperationOutsideScope(ValueError):
    """The proposed plan includes an operation the supervisor did not grant."""


def validate_experiment_context(
    experiment: ExperimentPlan,
    catalog: RequirementCatalog,
    allowed_operations: Sequence[OperationReference],
) -> None:
    """Check version, exact references and permissions without executing anything.

    Every declared requirement precondition must be represented, even when its
    verification is unavailable. This checks the plan's contract, not the truth
    of its hypothesis, feasibility, or satisfaction of its preconditions.
    The HTTP tool remains responsible for enforcing actual request permissions.
    """

    if experiment.specification_version != catalog.specification_version:
        raise ValueError("Experiment and catalog specification versions differ.")

    requirements = {requirement.id: requirement for requirement in catalog.requirements}
    unknown = set(experiment.requirement_ids) - requirements.keys()
    
    if unknown:
        raise ValueError(f"Unknown experiment requirement IDs: {sorted(unknown)}.")

    documented = {
        (operation.method, operation.path)
        for requirement in catalog.requirements
        for operation in requirement.operations
    }
    allowed = {(operation.method, operation.path) for operation in allowed_operations}
    
    if not allowed or len(allowed) != len(allowed_operations):
        raise ValueError("Explicit, unique authorized operations are required.")
    
    if not allowed.issubset(documented) or any(method == "CONNECT" for method, _ in allowed):
        raise ValueError("Authorized operations must be documented and cannot use CONNECT.")

    selected = [requirements[identifier] for identifier in experiment.requirement_ids]
    relevant_operations = {
        (operation.method, operation.path)
        for requirement in selected
        for operation in requirement.operations
    }
    
    for step in experiment.steps:
        if step.operation is None:
            continue
        
        key = (step.operation.method, step.operation.path)
        
        if key not in allowed:
            raise OperationOutsideScope(f"Step {step.id!r} uses an unauthorized operation: {key}.")
        
        if key not in relevant_operations:
            raise ValueError(f"Step {step.id!r} is not linked to a selected requirement: {key}.")

    required_conditions = {
        (requirement.id, condition)
        for requirement in selected
        for condition in requirement.preconditions
    }
    
    proposed_conditions = {
        (condition.requirement_id, condition.condition)
        for condition in experiment.preconditions
    }
    
    if proposed_conditions != required_conditions:
        missing = sorted(required_conditions - proposed_conditions)
        unknown_conditions = sorted(proposed_conditions - required_conditions)
        
        raise ValueError(
            "Experiment preconditions must match the selected requirements exactly; "
            f"missing={missing}, unknown={unknown_conditions}."
        )
