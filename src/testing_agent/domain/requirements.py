from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
HttpMethod = Literal[
    "GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OperationReference(_StrictModel):
    """Operation identified by http method and full path of the specification."""
    
    method: HttpMethod
    path: str = Field(pattern=r"^/[^?#\s]*$")


class RequirementException(_StrictModel):
    """Exception to a requirement.
    
    - id: unique identifier of the exception within the requirement.
    - condition: precondition that triggers the exception.
    - expected_behavior: behavior that is expected when the exception is triggered.
    """

    id: NonEmptyText
    condition: NonEmptyText
    expected_behavior: NonEmptyText


class Requirement(_StrictModel):
    """ Requirement / Rule.

    Lists of preconditions and exceptions must be declared in the JSON,
    even when they are empty. Exception IDs are local to the requirement.

    - id: unique identifier of the requirement within the specification version.
    - description: brief description of the requirement.
    - operations: list of operations that must comply with the requirement.
    - preconditions: list of conditions that must be met for the requirement to apply.
    - expected_behavior: behavior that is expected when the requirement applies.
    - exceptions: list of exceptions to the requirement.
    
    """

    id: NonEmptyText
    description: NonEmptyText
    operations: list[OperationReference] = Field(min_length=1)
    preconditions: list[NonEmptyText]
    expected_behavior: NonEmptyText
    exceptions: list[RequirementException]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        operations = {(operation.method, operation.path) for operation in self.operations}

        if len(operations) != len(self.operations):
            raise ValueError(f"Requirement '{self.id}' contains duplicate operations.")

        exception_ids = [exception.id for exception in self.exceptions]

        if len(exception_ids) != len(set(exception_ids)):
            raise ValueError(f"Requirement '{self.id}' contains duplicate exception IDs.")

        return self


class RequirementCatalog(_StrictModel):
    """Set of requirements belonging to the same specification version."""

    specification_version: NonEmptyText
    requirements: list[Requirement] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_requirement_ids(self) -> Self:
        requirement_ids = [requirement.id for requirement in self.requirements]

        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError(f"Requirement IDs in specification version '{self.specification_version}' must be unique.")

        return self