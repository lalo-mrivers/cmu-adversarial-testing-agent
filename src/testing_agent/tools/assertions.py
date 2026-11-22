import json
import re
from decimal import Decimal, DecimalException
from typing import Any, Literal


Check = Literal["status_code", "header", "json_value", "json_exists"]
_MISSING = object()
_MAX_JSON_CHARS = 1_048_576


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys are ambiguous.")

        result[key] = value
    
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"Non-finite JSON number: {value}.")


def _load_json(text: str) -> Any:
    """Preserves numeric precision and rejects ambiguous documents."""

    if not isinstance(text, str) or len(text) > _MAX_JSON_CHARS:
        raise ValueError("JSON input must be text within the configured size limit.")
    
    try:
        return json.loads(
            text,
            parse_int=Decimal,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_keys,
        )
    
    except (RecursionError, DecimalException) as error:
        raise ValueError("JSON input exceeds the parser's supported limits.") from error


def _dump_json(value: Any) -> str:
    """Represents Decimal numbers as JSON numbers, without converting to float."""

    if isinstance(value, Decimal):
        return str(value)
    
    if isinstance(value, list):
        return "[" + ",".join(_dump_json(item) for item in value) + "]"
    
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key) + ":" + _dump_json(item) for key, item in value.items()
        ) + "}"
    
    return json.dumps(value, allow_nan=False)


def _equal(left: Any, right: Any) -> bool:
    """Distinguishes booleans from numbers, also within objects and arrays."""
    
    if type(left) is not type(right):
        return False
    
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    
    return left == right


def _pointer_tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("Use a JSON Pointer: an empty string or a path starting with '/'.")
    
    if re.search(r"~(?![01])", pointer):
        raise ValueError("JSON Pointer escapes must be ~0 or ~1.")
    
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]


def _resolve(document: Any, tokens: list[str]) -> Any:
    current = document
    
    for token in tokens:
        if isinstance(current, dict):
            current = current.get(token, _MISSING)
        
        elif isinstance(current, list):
            if token == "-":
                return _MISSING
            
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("Array indices must be unsigned integers without leading zeros.")
            
            if len(token) > len(str(len(current))) or int(token) >= len(current):
                return _MISSING

            current = current[int(token)]
        
        else:
            return _MISSING
        
        if current is _MISSING:
            return _MISSING
    
    return current


def check_response(
    response: dict[str, Any],
    check: Check,
    expected_json: str,
    selector: str | None = None,
) -> dict[str, Any]:
    """Compare supplied HTTP response data against an explicit expectation.

    Pass the response object returned inside request_application's result.
    expected_json is JSON text, preserving exact numbers and JSON value types.
    status_code: expect an integer; omit selector.
    header: selector is a case-insensitive header name; expect an ordered JSON
    array of exact string values. An empty array means the header must be absent.
    json_value: selector is an RFC 6901 JSON Pointer; expect any JSON value.
    json_exists: use the same pointer; expect true or false. JSON null exists.
    For pointers, '' selects the root, /items/0 selects an array element, ~1
    escapes '/' and ~0 escapes '~'. URI fragments are not supported. The '-'
    array token denotes an absent element; malformed array indices are errors.
    JSON checks require an explicitly untruncated, valid, unambiguous body.
    Numbers compare exactly by value; booleans and strings are distinct types.
    Returns passed, failed or inconclusive. This is a comparison of caller-supplied
    data, not new application evidence. It does not authenticate the source or
    establish that the expectation follows from a requirement. The Verifier must
    check both against the original tool result and the versioned requirements.
    """

    if check not in {"status_code", "header", "json_value", "json_exists"}:
        raise ValueError("Unsupported response check.")
    
    expected = _load_json(expected_json)
    tokens: list[str] = []
    
    if check == "status_code":
        if selector is not None or not isinstance(expected, Decimal) or expected != expected.to_integral_value():
            raise ValueError("status_code requires an integer expectation and no selector.")
    
    elif check == "header":
        if not isinstance(selector, str) or not selector or any(char.isspace() for char in selector):
            raise ValueError("header requires a non-empty header name.")
        
        if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
            raise ValueError("header requires a JSON array of string values.")
    
    else:
        if selector is None:
            raise ValueError("Provide a JSON Pointer, including '' to select the root.")
        
        tokens = _pointer_tokens(selector)
        
        if check == "json_exists" and type(expected) is not bool:
            raise ValueError("json_exists requires a boolean expectation.")

    def result(outcome: str, reason: str, actual: Any = _MISSING) -> dict[str, Any]:
        return {
            "check": check,
            "selector": selector,
            "expected_json": expected_json,
            "actual_json": None if actual is _MISSING else _dump_json(actual),
            "outcome": outcome,
            "reason": reason,
            "input_source": "caller_supplied",
        }

    if check == "status_code":
        status = response.get("status_code")
        
        if type(status) is not int:
            return result("inconclusive", "A valid observed status code is unavailable.")
        
        actual = Decimal(status)
    
    elif check == "header":
        headers = response.get("headers")
        
        if not isinstance(headers, list) or any(
            not isinstance(pair, (list, tuple)) or len(pair) != 2
            or any(not isinstance(item, str) for item in pair) for pair in headers
        ):
            return result("inconclusive", "The observed header collection is unavailable or malformed.")
        
        actual = [value for name, value in headers if name.lower() == str(selector).lower()]
    
    else:
        if response.get("body_truncated") is not False:
            return result("inconclusive", "The response body is truncated or its completeness is unknown.")
        
        body = response.get("body")
        
        if not isinstance(body, str):
            return result("inconclusive", "The observed response body is unavailable.")
        
        try:
            document = _load_json(body)

        except ValueError:
            return result("inconclusive", "The response body cannot be interpreted as unambiguous JSON.")
        
        actual = _resolve(document, tokens)
        
        if check == "json_exists":
            actual = actual is not _MISSING

        elif actual is _MISSING:
            return result("failed", "The selected JSON value is absent.")

    try:
        passed = _equal(actual, expected)

        return result("passed" if passed else "failed", "Values match." if passed else "Values differ.", actual)

    except RecursionError:
        return result("inconclusive", "The values exceed the comparator's supported nesting depth.")