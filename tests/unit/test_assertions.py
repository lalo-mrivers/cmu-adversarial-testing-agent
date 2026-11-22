import json
from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest

from testing_agent.tools.assertions import Check, check_response


_INVALID_JSON = (
    "",
    "not-json",
    '{"id": 1, "id": 2}',
    '{"user": {"id": 1, "id": 2}}',
    "NaN",
    "Infinity",
    "-Infinity",
)


@pytest.fixture
def response() -> dict[str, Any]:
    return {
        "status_code": 200,
        "headers": [
            ["Content-Type", "application/json"],
            ["X-Tag", "first"],
            ["x-tag", "second"],
        ],
        "body": (
            '{"user": {"id": 1, "nickname": null}, '
            '"items": [10, 20]}'
        ),
        "body_truncated": False,
    }

#Pruebas del comparador determinista de respuestas HTTP.
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, "passed"),
        (403, "failed"),
        (500, "failed"),
    ],
)
def test_status_code(status: int, outcome: str) -> None:
    result = check_response(
        response={"status_code": status},
        check="status_code",
        expected_json="200",
    )

    assert result["outcome"] == outcome
    assert result["actual_json"] == str(status)


@pytest.mark.parametrize(
    "observed",
    [
        {},
        {"status_code": None},
        {"status_code": "200"},
        {"status_code": 200.0},
        {"status_code": True},
    ],
)
def test_invalid_observed_status_is_inconclusive(
    observed: dict[str, Any],
) -> None:
    result = check_response(observed, "status_code", "200")

    assert result["outcome"] == "inconclusive"
    assert result["actual_json"] is None


@pytest.mark.parametrize(
    ("selector", "expected_json", "outcome"),
    [
        ("content-type", '["application/json"]', "passed"),
        ("CONTENT-TYPE", '["application/json"]', "passed"),
        ("Content-Type", '["Application/JSON"]', "failed"),
        ("X-Tag", '["first", "second"]', "passed"),
        ("x-tag", '["second", "first"]', "failed"),
        ("X-Tag", '["first"]', "failed"),
        ("X-Missing", "[]", "passed"),
        ("X-Missing", '["value"]', "failed"),
    ],
)
def test_header_comparison(
    response: dict[str, Any],
    selector: str,
    expected_json: str,
    outcome: str,
) -> None:
    result = check_response(
        response, "header", expected_json, selector
    )

    assert result["outcome"] == outcome


@pytest.mark.parametrize(
    "observed",
    [
        {},
        {"headers": None},
        {"headers": {"Content-Type": "application/json"}},
        {"headers": [["X-Tag"]]},
        {"headers": [["X-Tag", "first", "second"]]},
        {"headers": [["X-Tag", 123]]},
        {"headers": [[123, "first"]]},
    ],
)
def test_malformed_headers_are_inconclusive(
    observed: dict[str, Any],
) -> None:
    result = check_response(
        observed, "header", "[]", "X-Missing"
    )

    assert result["outcome"] == "inconclusive"
    assert result["actual_json"] is None


@pytest.mark.parametrize(
    ("body", "expected_json", "outcome"),
    [
        (
            '{"a": 1, "b": [2, null]}',
            '{"b": [2, null], "a": 1}',
            "passed",
        ),
        ('{"a": 1}', '{"a": 1, "b": 2}', "failed"),
        ("[1, 2]", "[2, 1]", "failed"),
        ("[1]", "[1, 2]", "failed"),
        ("true", "1", "failed"),
        ("false", "0", "failed"),
        ('{"enabled": true}', '{"enabled": 1}', "failed"),
        ("[false]", "[0]", "failed"),
        ('"1"', "1", "failed"),
        ("1.00", "1e0", "passed"),
        ("9007199254740993", "9007199254740992", "failed"),
        ("0.10000000000000000001", "0.1", "failed"),
        ("null", "null", "passed"),
    ],
)
def test_json_value_comparison(
    response: dict[str, Any],
    body: str,
    expected_json: str,
    outcome: str,
) -> None:
    response["body"] = body

    result = check_response(
        response, "json_value", expected_json, ""
    )

    assert result["outcome"] == outcome


def test_actual_json_preserves_numeric_precision(
    response: dict[str, Any],
) -> None:
    number = "9007199254740993.00000000000000000001"
    response["body"] = '{"value": ' + number + "}"

    result = check_response(
        response, "json_value", number, "/value"
    )

    assert result["outcome"] == "passed"
    assert Decimal(result["actual_json"]) == Decimal(number)


@pytest.mark.parametrize(
    ("selector", "expected_json"),
    [
        ("/a~1b/~0key/1", "7"),
        ("/a~1b/~0key/0/", "null"),
        ("/~01", "8"),
        ("/01", '"object-key"'),
    ],
)
def test_json_pointer_resolution(
    response: dict[str, Any],
    selector: str,
    expected_json: str,
) -> None:
    response["body"] = (
        '{"a/b": {"~key": [{"": null}, 7]}, '
        '"~1": 8, "01": "object-key"}'
    )

    result = check_response(
        response, "json_value", expected_json, selector
    )

    assert result["outcome"] == "passed"


@pytest.mark.parametrize(
    "selector",
    [
        "/missing",
        "/user/missing",
        "/user/id/child",
        "/items/2",
        "/items/-",
        "/items/999999999999999999999999",
    ],
)
def test_missing_value_does_not_equal_json_null(
    response: dict[str, Any],
    selector: str,
) -> None:
    result = check_response(
        response, "json_value", "null", selector
    )

    assert result["outcome"] == "failed"
    assert result["actual_json"] is None


def test_present_json_null_matches(
    response: dict[str, Any],
) -> None:
    result = check_response(
        response, "json_value", "null", "/user/nickname"
    )

    assert result["outcome"] == "passed"
    assert result["actual_json"] == "null"


@pytest.mark.parametrize(
    ("selector", "expected_json", "outcome", "actual_json"),
    [
        ("/user/nickname", "true", "passed", "true"),
        ("/user/nickname", "false", "failed", "true"),
        ("/missing", "false", "passed", "false"),
        ("/missing", "true", "failed", "false"),
        ("/items/-", "false", "passed", "false"),
        ("/items/1", "true", "passed", "true"),
        ("", "true", "passed", "true"),
    ],
)
def test_json_existence(
    response: dict[str, Any],
    selector: str,
    expected_json: str,
    outcome: str,
    actual_json: str,
) -> None:
    result = check_response(
        response, "json_exists", expected_json, selector
    )

    assert result["outcome"] == outcome
    assert result["actual_json"] == actual_json


@pytest.mark.parametrize("invalid_json", _INVALID_JSON)
@pytest.mark.parametrize(
    ("check", "expected_json"),
    [
        ("json_value", "null"),
        ("json_exists", "false"),
    ],
)
def test_invalid_observed_json_is_inconclusive(
    response: dict[str, Any],
    invalid_json: str,
    check: Check,
    expected_json: str,
) -> None:
    response["body"] = invalid_json

    result = check_response(
        response, check, expected_json, "/missing"
    )

    assert result["outcome"] == "inconclusive"
    assert result["actual_json"] is None


@pytest.mark.parametrize("invalid_json", _INVALID_JSON)
def test_invalid_expected_json_is_rejected(
    response: dict[str, Any],
    invalid_json: str,
) -> None:
    with pytest.raises(ValueError):
        check_response(
            response, "json_value", invalid_json, ""
        )


@pytest.mark.parametrize(
    "observed",
    [
        {"body": '{"value": 1}'},
        {"body": '{"value": 1}', "body_truncated": True},
        {"body": '{"value": 1}', "body_truncated": None},
        {"body": '{"value": 1}', "body_truncated": 0},
        {"body": '{"value": 1}', "body_truncated": "false"},
        {"body_truncated": False},
        {"body": None, "body_truncated": False},
        {"body": {"value": 1}, "body_truncated": False},
    ],
)
@pytest.mark.parametrize(
    ("check", "expected_json"),
    [
        ("json_value", "1"),
        ("json_exists", "true"),
    ],
)
def test_unavailable_or_incomplete_body_is_inconclusive(
    observed: dict[str, Any],
    check: Check,
    expected_json: str,
) -> None:
    result = check_response(
        observed, check, expected_json, "/value"
    )

    assert result["outcome"] == "inconclusive"
    assert result["actual_json"] is None


@pytest.mark.parametrize(
    ("check", "expected_json", "selector"),
    [
        ("status_code", "true", None),
        ("status_code", '"200"', None),
        ("status_code", "200.5", None),
        ("status_code", "200", "/status"),
        ("header", '"application/json"', "Content-Type"),
        ("header", '["first", 1]', "X-Tag"),
        ("header", "[]", None),
        ("header", "[]", ""),
        ("header", "[]", "X Tag"),
        ("json_exists", "1", "/user"),
        ("json_value", "null", None),
        ("json_value", "null", "user"),
        ("json_value", "null", "#/user"),
        ("json_value", "null", "/~2"),
        ("json_value", "null", "/user~"),
    ],
)
def test_invalid_check_arguments_are_rejected(
    response: dict[str, Any],
    check: Check,
    expected_json: str,
    selector: str | None,
) -> None:
    with pytest.raises(ValueError):
        check_response(
            response, check, expected_json, selector
        )


@pytest.mark.parametrize(
    "selector",
    [
        "/items/01",
        "/items/-1",
        "/items/+1",
        "/items/1.0",
        "/items/",
    ],
)
def test_invalid_array_indices_are_rejected(
    response: dict[str, Any],
    selector: str,
) -> None:
    with pytest.raises(ValueError):
        check_response(
            response, "json_value", "null", selector
        )


def test_result_preserves_input_and_identifies_its_source(
    response: dict[str, Any],
) -> None:
    original = deepcopy(response)
    expected_json = ' [ "first", "second" ] '

    result = check_response(
        response, "header", expected_json, "x-tag"
    )

    assert response == original
    assert result["check"] == "header"
    assert result["selector"] == "x-tag"
    assert result["expected_json"] == expected_json
    assert json.loads(result["actual_json"]) == ["first", "second"]
    assert result["outcome"] == "passed"
    assert isinstance(result["reason"], str)
    assert result["reason"].strip()
    assert result["input_source"] == "caller_supplied"

    json.dumps(result, allow_nan=False)