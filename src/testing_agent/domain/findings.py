from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from testing_agent.domain.requirements import NonEmptyText


# violation: A violation of the requirement was observed in the scope.
# no_violation_observed: The observations do not show a violation of the requirement in the scope.
# inconclusive: The observations do not support a conclusion about the requirement in the scope.
FindingOutcome = Literal[
    "violation",
    "no_violation_observed",
    "inconclusive",
]


class EvidenceReference(BaseModel):
    """Locates a call within an evidence record.

    The existence of both identifiers and their correspondence to an actual
    result must be verified against the execution trace.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_id: NonEmptyText = Field(
        description="ID of the evidence record containing the tool result."
    )
    tool_call_id: NonEmptyText = Field(
        description="ID of the tool call matched to that result."
    )


class Finding(BaseModel):
    """Conclusion about a requirement within an explicit scope.

    Validates the structure and internal consistency of the result. It does
    not verify that the references exist, that the requirement belongs to
    the catalog, or that the evidence supports the conclusion.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    requirement_id: NonEmptyText = Field(
        description="Exact requirement ID from the loaded specification."
    )
    specification_version: NonEmptyText | None = Field(
        default=None, description="Loaded version. The controller stamps this field on export."
    )
    outcome: FindingOutcome = Field(
        description="Outcome for this requirement within the stated scope."
    )
    scope: NonEmptyText = Field(
        description="The specific operations, sequence and state assessed."
    )
    assessment: NonEmptyText = Field(
        description="Explain how the observations support the outcome."
    )
    evidence: list[EvidenceReference] = Field(
        description="References to the tool results used in the assessment."
    )
    limitations: list[NonEmptyText] = Field(
        description="Missing observations, unverified preconditions or other "
        "limits on the conclusion. Use an empty list if none apply."
    )

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        references = {
            (reference.evidence_id, reference.tool_call_id)
            for reference in self.evidence
        }

        if len(references) != len(self.evidence):
            raise ValueError("Las referencias de evidencia no pueden repetirse.")

        if self.outcome != "inconclusive" and not self.evidence:
            raise ValueError(
                "Una conclusión sobre cumplimiento o infracción requiere evidencia."
            )

        if self.outcome == "inconclusive" and not self.limitations:
            raise ValueError(
                "Un resultado inconcluso debe explicar sus limitaciones."
            )

        return self
