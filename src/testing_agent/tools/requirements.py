import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from testing_agent.domain.requirements import (
    HttpMethod,
    OperationReference,
    Requirement,
    RequirementCatalog,
)


def _unique_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rejects repeated keys to avoid ambiguous interpretation of the JSON."""
    result: dict[str, Any] = {}
    
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}.")

        result[key] = value
    
    return result


def create_requirements_lookup(
    path: str | Path,
    specification_version: str,
) -> Callable[..., dict[str, Any]]:
    """Loads a single version and prepares the function that MCP will expose.
    
    The path is provided by the application when preparing the tool.
    Queries receive only the version and the exact selector.
    """

    with Path(path).open(encoding="utf-8-sig") as file:
        data = json.load(file, object_pairs_hook=_unique_json_keys)

    catalog = RequirementCatalog.model_validate(data)
    
    if catalog.specification_version != specification_version:
        raise ValueError("The catalog does not match the requested specification version.")

    by_id = {requirement.id: requirement for requirement in catalog.requirements}
    by_operation: dict[tuple[str, str], list[Requirement]] = {}
    
    for requirement in catalog.requirements:
        for operation in requirement.operations:
            key = (operation.method, operation.path)
            by_operation.setdefault(key, []).append(requirement)

    def lookup_requirements(
        specification_version: str,
        requirement_id: str | None = None,
        method: HttpMethod | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve authoritative requirements from the loaded specification version.

        Provide either requirement_id, or both method and path. Do not combine them.
        Use the exact identifier or uppercase HTTP method and full specification path,
        including path-template parameters such as /users/{user_id} when applicable.
        Matching is case-sensitive; paths are not normalized or matched by similarity.
        Returns all matching requirements, including preconditions and exceptions.
        An error means the lookup could not establish applicable requirements; it is
        not evidence that the operation is unrestricted.
        """

        if specification_version != catalog.specification_version:
            raise ValueError("The requested specification version is not loaded.")

        if requirement_id is not None:
            if method is not None or path is not None:
                raise ValueError("Provide requirement_id or method and path, not both.")

            if not isinstance(requirement_id, str) or not requirement_id.strip():
                raise ValueError("requirement_id must be a non-empty string.")

            requirement = by_id.get(requirement_id)
            matches = [] if requirement is None else [requirement]

        else:
            if method is None or path is None:
                raise ValueError("Provide requirement_id, or both method and path.")

            operation = OperationReference(method=method, path=path)
            matches = by_operation.get((operation.method, operation.path), [])

        if not matches:
            raise LookupError("No requirements match this exact selector in the loaded version.")

        return {
            "specification_version": catalog.specification_version,
            "requirements": [requirement.model_dump(mode="json") for requirement in matches],
        }

    return lookup_requirements