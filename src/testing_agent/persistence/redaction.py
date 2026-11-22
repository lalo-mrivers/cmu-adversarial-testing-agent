import json
import re
from typing import Any


_KEY = (r"(?:authorization|proxy-authorization|(?:set-)?cookie|(?:x[-_])?api[-_]?key|password|passwd|"
        r"secret|(?:access[-_]|refresh[-_]|auth[-_])?token|client[-_]secret|email|ssn|card[-_]?number)")
_SENSITIVE = re.compile(_KEY, re.I)
_ASSIGNMENT = re.compile(
    rf"""(?i)(\b{_KEY}\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\r\n&,;}}\s]+(?:\s+[^\r\n&,;}}\s]+)?)"""
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_USERINFO = re.compile(r"(https?://)[^/\s?#@]+@", re.I)
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_SPACE = re.compile(r"\s*")
_DECODER = json.JSONDecoder()
MASK = "[REDACTED]"


def _mask_text(value: str) -> str:
    if not value.lstrip().startswith(("{", "[")):
        value = _BEARER.sub(r"\1 [REDACTED]", value)
        value = _USERINFO.sub(r"\1[REDACTED]@", value)

        return _ASSIGNMENT.sub(lambda m: m[1] + MASK, value)

    # Preserve duplicate keys, number spellings and whitespace in HTTP evidence.
    # Replace individual value spans, including JSON serialized in MCP text fields.
    pieces = []
    cursor = 0

    for match in _STRING.finditer(value):
        if match.start() < cursor:
            continue

        try:
            decoded = json.loads(match[0])

        except ValueError:
            continue

        end = match.end()
        separator = _SPACE.match(value, end).end()
        following = value[separator:separator + 1]

        if _SENSITIVE.fullmatch(decoded) and (
            following == ":" or (following == "," and value[:match.start()].rstrip().endswith("["))
        ):
            start = _SPACE.match(value, separator + 1).end()

            try:
                _, end = _DECODER.raw_decode(value, start)

            except (ValueError, RecursionError):
                end = len(value)

            pieces.extend([value[cursor:start], json.dumps(MASK)])
            cursor = end

        elif following != ":":
            masked = _mask_text(decoded)

            if masked != decoded:
                pieces.extend([value[cursor:match.start()], json.dumps(masked, ensure_ascii=False)])
                cursor = end

    return "".join([*pieces, value[cursor:]])


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: MASK if _SENSITIVE.fullmatch(key) else redact(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        if len(value) == 2 and isinstance(value[0], str) and _SENSITIVE.fullmatch(value[0]):
            return [value[0], MASK]

        return [redact(item) for item in value]

    if isinstance(value, str):
        return _mask_text(value)

    return value
