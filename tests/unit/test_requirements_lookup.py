import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from testing_agent.tools.requirements import create_requirements_lookup


Lookup = Callable[..., dict[str, Any]]


## Pruebas del contrato de consulta determinista de requisitos
@pytest.fixture
def catalog() -> dict[str, Any]:
    """Synthetic catalog with similar paths and shared requirements."""
    definitions = [
        (
            "REQ-USER-READ",
            "GET",
            "/users/profile",
            "Return only the caller's profile.",
        ),
        (
            "REQ-USER-ACTIVE",
            "GET",
            "/users/profile",
            "Reject access for inactive accounts.",
        ),
        (
            "REQ-ADMIN-READ",
            "GET",
            "/admin/profile",
            "Require administrator privileges.",
        ),
        (
            "REQ-USER-UPDATE",
            "PATCH",
            "/users/profile",
            "Update only editable profile fields.",
        ),
        (
            "REQ-USER-BY-ID",
            "GET",
            "/users/{user_id}",
            "Return the requested profile only to authorized callers.",
        ),
    ]

    requirements: list[dict[str, Any]] = [
        {
            "id": requirement_id,
            "description": f"Test rule {requirement_id}.",
            "operations": [{"method": method, "path": path}],
            "preconditions": ["The caller is authenticated."],
            "expected_behavior": expected_behavior,
            "exceptions": [],
        }
        for requirement_id, method, path, expected_behavior in definitions
    ]
    requirements[0]["exceptions"] = [
        {
            "id": "EX-SUSPENDED",
            "condition": "The account is suspended.",
            "expected_behavior": "Deny access to the profile.",
        }
    ]

    return {
        "specification_version": "v1",
        "requirements": requirements,
    }


def _write_catalog(
    path: Path,
    catalog: dict[str, Any],
    *,
    encoding: str = "utf-8",
) -> None:
    path.write_text(
        json.dumps(catalog, ensure_ascii=False),
        encoding=encoding,
    )


@pytest.fixture
def catalog_path(tmp_path: Path, catalog: dict[str, Any]) -> Path:
    path = tmp_path / "requirements.json"
    _write_catalog(path, catalog)

    return path


@pytest.fixture
def lookup(catalog_path: Path) -> Lookup:
    return create_requirements_lookup(catalog_path, "v1")


def test_lookup_by_id_returns_the_complete_requirement(
    lookup: Lookup,
    catalog: dict[str, Any],
) -> None:
    result = lookup("v1", requirement_id="REQ-USER-READ")

    assert result == {
        "specification_version": "v1",
        "requirements": [catalog["requirements"][0]],
    }


@pytest.mark.parametrize(
    "method, path, expected_ids",
    [
        (
            "GET",
            "/users/profile",
            ["REQ-USER-READ", "REQ-USER-ACTIVE"],
        ),
        ("PATCH", "/users/profile", ["REQ-USER-UPDATE"]),
        ("GET", "/admin/profile", ["REQ-ADMIN-READ"]),
        ("GET", "/users/{user_id}", ["REQ-USER-BY-ID"]),
    ],
)
def test_lookup_matches_the_complete_operation(
    lookup: Lookup,
    method: str,
    path: str,
    expected_ids: list[str],
) -> None:
    result = lookup("v1", method=method, path=path)

    assert result["specification_version"] == "v1"
    assert sorted(
        requirement["id"] for requirement in result["requirements"]
    ) == sorted(expected_ids)


@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"method": "GET"},
        {"path": "/users/profile"},
        {"requirement_id": ""},
        {"requirement_id": "   "},
        {"requirement_id": "REQ-USER-READ", "method": "GET"},
        {
            "requirement_id": "REQ-USER-READ",
            "path": "/users/profile",
        },
        {
            "requirement_id": "REQ-USER-READ",
            "method": "GET",
            "path": "/users/profile",
        },
        {"method": "get", "path": "/users/profile"},
        {"method": "GET", "path": "/users/profile?active=true"},
    ],
)
def test_rejects_invalid_or_combined_selectors(
    lookup: Lookup,
    selector: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        lookup("v1", **selector)


@pytest.mark.parametrize(
    "selector",
    [
        {"requirement_id": "REQ-UNKNOWN"},
        {"requirement_id": "req-user-read"},
        {"requirement_id": "REQ-USER-READ "},
        {"method": "GET", "path": "/profile"},
        {"method": "GET", "path": "/Users/profile"},
        {"method": "GET", "path": "/users/profile/"},
        {"method": "GET", "path": "/users/123"},
        {"method": "POST", "path": "/users/profile"},
    ],
)
def test_missing_exact_match_raises_lookup_error(
    lookup: Lookup,
    selector: dict[str, Any],
) -> None:
    with pytest.raises(LookupError):
        lookup("v1", **selector)


@pytest.mark.parametrize(
    "selector",
    [
        {"requirement_id": "REQ-USER-READ"},
        {"method": "GET", "path": "/users/profile"},
    ],
)
def test_rejects_queries_for_another_version(
    lookup: Lookup,
    selector: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        lookup("v2", **selector)


def test_rejects_loading_a_different_version(
    catalog_path: Path,
) -> None:
    with pytest.raises(ValueError):
        create_requirements_lookup(catalog_path, "v2")


@pytest.mark.parametrize("field", ["preconditions", "exceptions"])
def test_rejects_requirements_with_missing_required_fields(
    catalog_path: Path,
    catalog: dict[str, Any],
    field: str,
) -> None:
    del catalog["requirements"][0][field]
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValidationError):
        create_requirements_lookup(catalog_path, "v1")


def test_rejects_duplicate_requirement_ids(
    catalog_path: Path,
    catalog: dict[str, Any],
) -> None:
    catalog["requirements"][1]["id"] = catalog["requirements"][0]["id"]
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValidationError):
        create_requirements_lookup(catalog_path, "v1")


@pytest.mark.parametrize(
    "fragment",
    [
        '"specification_version": "v1"',
        '"condition": "The account is suspended."',
    ],
)
def test_rejects_duplicate_json_keys_at_different_depths(
    catalog_path: Path,
    catalog: dict[str, Any],
    fragment: str,
) -> None:
    content = json.dumps(catalog, ensure_ascii=False)
    assert fragment in content

    #Conserva la sintaxis JSON, pero repite una clave
    content = content.replace(fragment, f"{fragment}, {fragment}", 1)
    catalog_path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        create_requirements_lookup(catalog_path, "v1")


def test_rejects_malformed_json(catalog_path: Path) -> None:
    catalog_path.write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        create_requirements_lookup(catalog_path, "v1")


def test_missing_catalog_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        create_requirements_lookup(tmp_path / "missing.json", "v1")


def test_accepts_utf8_with_bom(
    catalog_path: Path,
    catalog: dict[str, Any],
) -> None:
    _write_catalog(catalog_path, catalog, encoding="utf-8-sig")

    lookup = create_requirements_lookup(catalog_path, "v1")

    assert lookup("v1", requirement_id="REQ-USER-READ") == {
        "specification_version": "v1",
        "requirements": [catalog["requirements"][0]],
    }


def test_each_lookup_keeps_its_loaded_version(
    lookup: Lookup,
    catalog_path: Path,
    catalog: dict[str, Any],
) -> None:
    original_result = lookup("v1", requirement_id="REQ-USER-READ")

    catalog["specification_version"] = "v2"
    catalog["requirements"][0]["expected_behavior"] = (
        "Apply the updated behavior defined in version two."
    )
    _write_catalog(catalog_path, catalog)

    newer_lookup = create_requirements_lookup(catalog_path, "v2")

    assert lookup("v1", requirement_id="REQ-USER-READ") == original_result
    assert newer_lookup("v2", requirement_id="REQ-USER-READ") == {
        "specification_version": "v2",
        "requirements": [catalog["requirements"][0]],
    }


def test_modifying_a_response_does_not_change_the_catalog(
    lookup: Lookup,
    catalog: dict[str, Any],
) -> None:
    result = lookup("v1", requirement_id="REQ-USER-READ")
    requirement = result["requirements"][0]

    requirement["operations"][0]["path"] = "/modified"
    requirement["preconditions"].append("Injected precondition.")
    requirement["exceptions"][0]["condition"] = "Modified condition."
    result["requirements"].append({"id": "INJECTED"})

    assert lookup("v1", requirement_id="REQ-USER-READ") == {
        "specification_version": "v1",
        "requirements": [catalog["requirements"][0]],
    }