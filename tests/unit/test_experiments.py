import json
import unittest
from copy import deepcopy
from uuid import uuid4

from pydantic import ValidationError

from testing_agent.domain.experiments import (
    Experiment,
    ExperimentPlan,
    validate_experiment_context,
)
from testing_agent.domain.requirements import OperationReference, RequirementCatalog


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.get = {"method": "GET", "path": "/products/{product_id}"}
        self.post = {"method": "POST", "path": "/reservations"}
        self.catalog = RequirementCatalog.model_validate({
            "specification_version": "v1",
            "requirements": [{
                "id": "REQ-STOCK", "description": "Preserve stock.",
                "operations": [self.get, self.post],
                "preconditions": ["A known initial state is available."],
                "expected_behavior": "Reads preserve available stock.",
                "exceptions": [],
            }],
        })
        self.allowed = [OperationReference.model_validate(self.get)]
        self.data = {
            "specification_version": "v1",
            "hypothesis": "Reading a product could change its stock.",
            "requirement_ids": ["REQ-STOCK"],
            "preconditions": [{
                "requirement_id": "REQ-STOCK",
                "condition": "A known initial state is available.",
                "verification": "Inspect the system-provided initialization snapshot.",
            }],
            "steps": [{
                "id": "read_before", "instruction": "Read product-001 stock.",
                "operation": self.get, "depends_on": [],
                "expected_observation": "Stock matches the starting value.",
                "evidence_to_collect": ["Status and available_quantity."],
            }, {
                "id": "read_after", "instruction": "Read the same product again.",
                "operation": self.get, "depends_on": ["read_before"],
                "expected_observation": "Stock equals the first observed quantity.",
                "evidence_to_collect": ["Status and available_quantity."],
            }],
            "stop_conditions": ["Both reads are recorded or a required observation is unavailable."],
        }

    def validate(self, data=None, allowed=None):
        plan = ExperimentPlan.model_validate(self.data if data is None else data)
        validate_experiment_context(plan, self.catalog, self.allowed if allowed is None else allowed)

        return plan

    def test_json_round_trip_and_application_identity(self):
        plan = self.validate()
        experiment = Experiment(id=str(uuid4()), **plan.model_dump(mode="json"))
        self.assertEqual(Experiment.model_validate_json(experiment.model_dump_json()), experiment)
        self.assertEqual(Experiment.model_validate(json.loads(experiment.model_dump_json())), experiment)
        self.assertNotIn("id", ExperimentPlan.model_json_schema()["properties"])

    def test_blank_coerced_and_extra_fields_are_rejected(self):
        mutations = [
            lambda d: d.update(hypothesis=" \n "),
            lambda d: d.update(specification_version=1),
            lambda d: d.update(id=str(uuid4())),
            lambda d: d.update(steps=[]),
            lambda d: d.update(stop_conditions=[]),
            lambda d: d["steps"][0].update(evidence_to_collect=[]),
            lambda d: d["steps"][0].update(expected_observation=" "),
            lambda d: d["preconditions"][0].update(verified=True),
            lambda d: d["steps"][0].update(id="bad label"),
        ]

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data = deepcopy(self.data)
                mutate(data)

                with self.assertRaises(ValidationError):
                    self.validate(data)

    def test_duplicate_references_are_rejected(self):
        for field in ("requirement_ids", "preconditions", "stop_conditions"):
            with self.subTest(field=field):
                data = deepcopy(self.data)
                data[field].append(deepcopy(data[field][0]))

                with self.assertRaises(ValidationError):
                    self.validate(data)

        data = deepcopy(self.data)
        data["steps"][1]["id"] = "read_before"

        with self.assertRaises(ValidationError):
            self.validate(data)

    def test_invalid_dependencies_are_rejected(self):
        for index, dependencies in [
            (0, ["read_before"]), (0, ["read_after"]), (1, ["missing"]),
            (1, ["read_before", "read_before"]),
        ]:
            with self.subTest(index=index, dependencies=dependencies):
                data = deepcopy(self.data)
                data["steps"][index]["depends_on"] = dependencies

                with self.assertRaises(ValidationError):
                    self.validate(data)

    def test_cycle_is_rejected(self):
        data = deepcopy(self.data)
        data["steps"][0]["depends_on"] = ["read_after"]

        with self.assertRaises(ValidationError):
            self.validate(data)

    def test_unknown_version_or_requirement_is_rejected(self):
        data = deepcopy(self.data)
        data["specification_version"] = "v2"

        with self.assertRaisesRegex(ValueError, "versions differ"):
            self.validate(data)

        data = deepcopy(self.data)
        data["requirement_ids"].append("MISSING")

        with self.assertRaisesRegex(ValueError, "Unknown experiment requirement"):
            self.validate(data)

    def test_documented_operation_does_not_grant_permission(self):
        data = deepcopy(self.data)
        data["steps"][0]["operation"] = self.post

        with self.assertRaisesRegex(ValueError, "unauthorized operation"):
            self.validate(data)

    def test_operations_require_exact_method_and_template(self):
        for operation in [
            {"method": "GET", "path": "/products/product-001"},
            {"method": "DELETE", "path": "/products/{product_id}"},
        ]:
            with self.subTest(operation=operation):
                data = deepcopy(self.data)
                data["steps"][0]["operation"] = operation

                with self.assertRaisesRegex(ValueError, "unauthorized operation"):
                    self.validate(data)

    def test_preconditions_cannot_be_omitted_or_invented(self):
        data = deepcopy(self.data)
        data["preconditions"] = []

        with self.assertRaisesRegex(ValueError, "preconditions must match"):
            self.validate(data)

        data = deepcopy(self.data)
        data["preconditions"][0]["condition"] = "The sandbox was reset."

        with self.assertRaisesRegex(ValueError, "preconditions must match"):
            self.validate(data)

    def test_unavailable_verification_is_not_claimed_as_success(self):
        data = deepcopy(self.data)
        data["preconditions"][0]["verification"] = "Cannot establish this with the available capabilities."
        self.validate(data)

    def test_operation_must_relate_to_selected_requirements(self):
        extra = self.catalog.requirements[0].model_copy(update={
            "id": "REQ-OTHER", "operations": [OperationReference(method="GET", path="/other")],
            "preconditions": [],
        })
        self.catalog.requirements.append(extra)
        self.allowed.append(extra.operations[0])
        data = deepcopy(self.data)
        data["steps"][0]["operation"] = {"method": "GET", "path": "/other"}

        with self.assertRaisesRegex(ValueError, "not linked to a selected requirement"):
            self.validate(data)

    def test_permissions_must_be_explicit_and_unique(self):
        for allowed in [[], self.allowed * 2, [OperationReference(method="GET", path="/unknown")]]:
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                self.validate(allowed=allowed)

    def test_context_only_steps_and_empty_requirement_preconditions(self):
        self.catalog.requirements[0].preconditions = []
        self.data["preconditions"] = []
        self.data["steps"][0]["operation"] = None
        self.data["steps"][0]["instruction"] = "Inspect available execution context."
        self.validate()

    def test_invalid_application_identity_is_rejected(self):
        for identifier in ["model-assigned", "", str(uuid4()).upper()]:
            with self.subTest(identifier=identifier), self.assertRaises(ValidationError):
                Experiment(id=identifier, **self.data)


if __name__ == "__main__":
    unittest.main()
