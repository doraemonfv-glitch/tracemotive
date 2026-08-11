"""Privacy normalization for the AgentLens v0.1 canonical boundary.

This module intentionally has no framework, transport, storage, or network
dependencies.  It contains only the Frozen Issue 02 redaction guarantees.
"""

from __future__ import annotations

from typing import Any
import re


REDACTION_PLACEHOLDER = "[REDACTED]"
MAX_CONTENT_BYTES = 262144
DEFAULT_CAPTURE_CONTENT = False

# This is the exact mandatory set from Frozen Specification section 13.2.
MANDATORY_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "cookie",
        "set-cookie",
    }
)

SENSITIVE_KEYS = MANDATORY_SENSITIVE_KEYS
_SENSITIVE_KEY_PATTERN = "|".join(
    re.escape(key) for key in sorted(MANDATORY_SENSITIVE_KEYS, key=len, reverse=True)
)
_SCHEME_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<scheme>Bearer|Basic)(?P<space>\s+)(?P<credential>[^\s]+)",
    re.IGNORECASE,
)
_QUOTED_ASSIGNMENT_PREFIX_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<key>{_SENSITIVE_KEY_PATTERN})"
    r"(?![A-Za-z0-9_-])(?P<assignment>\s*[:=]\s*)(?P<quote>['\"])",
    re.IGNORECASE,
)
_UNQUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<key>{_SENSITIVE_KEY_PATTERN})"
    r"(?P<assignment>\s*[:=]\s*)"
    r"(?![\"'])"
    r"(?P<value>(?:\[REDACTED\]|[^\s,;)}\]])[^\s,;)}\]]*)",
    re.IGNORECASE,
)


def sanitize_text(value: str) -> tuple[str, bool]:
    """Sanitize the Frozen recognizable credential patterns in one string."""

    if not isinstance(value, str):
        raise TypeError("sanitize_text requires a string")

    sanitized = _SCHEME_RE.sub(_replace_scheme_credential, value)
    sanitized = _sanitize_quoted_assignments(sanitized)
    sanitized = _UNQUOTED_ASSIGNMENT_RE.sub(_replace_unquoted_assignment, sanitized)
    return sanitized, sanitized != value


def _replace_scheme_credential(match: re.Match[str]) -> str:
    credential = match.group("credential")
    if credential == REDACTION_PLACEHOLDER:
        return match.group(0)
    return f"{match.group('scheme').capitalize()} {REDACTION_PLACEHOLDER}"


def _sanitize_quoted_assignments(value: str) -> str:
    """Replace quoted assignment values with a linear escape-aware scan."""

    pieces: list[str] = []
    cursor = 0
    search_start = 0
    while True:
        match = _QUOTED_ASSIGNMENT_PREFIX_RE.search(value, search_start)
        if match is None:
            pieces.append(value[cursor:])
            return "".join(pieces)

        pieces.append(value[cursor:match.start()])
        quote = match.group("quote")
        value_start = match.end()
        index = value_start
        while index < len(value):
            character = value[index]
            if character == "\\":
                index += 2
                continue
            if character == quote:
                break
            index += 1

        prefix = value[match.start():value_start]
        if index >= len(value):
            # An unterminated quoted scalar has no safe closing boundary.
            # Redact the remainder deterministically rather than leaking it.
            if value[value_start:] == REDACTION_PLACEHOLDER:
                pieces.append(value[match.start():])
            else:
                pieces.append(prefix + REDACTION_PLACEHOLDER)
            return "".join(pieces)

        raw_value = value[value_start:index]
        if raw_value == REDACTION_PLACEHOLDER:
            pieces.append(value[match.start():index + 1])
        else:
            pieces.append(prefix + REDACTION_PLACEHOLDER + quote)
        cursor = index + 1
        search_start = cursor


def _replace_unquoted_assignment(match: re.Match[str]) -> str:
    if match.group("value") == REDACTION_PLACEHOLDER:
        return match.group(0)
    return f"{match.group('key')}{match.group('assignment')}{REDACTION_PLACEHOLDER}"


def sanitize_json_value(value: Any, *, field_name: str = "value") -> tuple[Any, bool]:
    """Normalize, recursively sanitize, and copy one JSONValue.

    The returned boolean is true only when this call replaced a value or a
    recognizable free-text credential pattern.  The caller's containers are
    never modified.
    """

    from .canonical.models import normalize_json_value

    normalized = normalize_json_value(value, field_name=field_name)
    return _sanitize_normalized_value(normalized)


def sanitize_json_object(value: Any, *, field_name: str = "value") -> tuple[Any, bool]:
    """Normalize and sanitize a JSON object, preserving its object shape."""

    from .canonical.models import ValidationError

    sanitized, redacted = sanitize_json_value(value, field_name=field_name)
    if type(sanitized) is not dict:
        raise ValidationError(f"{field_name} must be a JSON object")
    return sanitized, redacted


def _sanitize_normalized_value(value: Any) -> tuple[Any, bool]:
    if type(value) is str:
        return sanitize_text(value)
    if type(value) not in (list, dict):
        return value, False

    sanitized_root: Any = [] if type(value) is list else {}
    changed = False
    active_ids = {id(value)}
    stack: list[tuple[Any, Any, Any]] = [
        (
            value,
            sanitized_root,
            enumerate(value) if type(value) is list else iter(value.items()),
        )
    ]
    while stack:
        source, target, iterator = stack[-1]
        try:
            key, item = next(iterator)
        except StopIteration:
            active_ids.remove(id(source))
            stack.pop()
            continue

        if type(source) is dict and key.casefold() in MANDATORY_SENSITIVE_KEYS:
            if type(item) is str and item == REDACTION_PLACEHOLDER:
                target[key] = item
            else:
                target[key] = REDACTION_PLACEHOLDER
                changed = True
            continue

        if type(item) is str:
            sanitized_item, item_changed = sanitize_text(item)
            changed = changed or item_changed
        elif type(item) in (list, dict):
            if id(item) in active_ids:
                # normalize_json_value already rejects cycles.  This guard
                # keeps this second pass safe if its implementation changes.
                from .canonical.models import ValidationError

                raise ValidationError("value contains a cyclic JSONValue")
            sanitized_item = [] if type(item) is list else {}
            active_ids.add(id(item))
            stack.append(
                (
                    item,
                    sanitized_item,
                    enumerate(item) if type(item) is list else iter(item.items()),
                )
            )
            if type(source) is dict:
                target[key] = sanitized_item
            else:
                target.append(sanitized_item)
            continue
        else:
            sanitized_item = item

        if type(source) is dict:
            target[key] = sanitized_item
        else:
            target.append(sanitized_item)

    return sanitized_root, changed


def capture_content(
    source: Any = None,
    *,
    capture_content: bool = DEFAULT_CAPTURE_CONTENT,
    source_available: bool = True,
    not_yet_available: bool = False,
) -> tuple[Any, Any]:
    """Apply the Frozen CaptureInfo precedence to one input/output value.

    ``source_available`` explicitly distinguishes an unavailable source from
    a source whose value is JSON null.  Disabled and not-yet-available paths
    return before the source is normalized or otherwise inspected.
    """

    from .canonical.models import CaptureInfo, _canonical_json_dumps, normalize_json_value

    if not_yet_available:
        return None, CaptureInfo("not_captured", "not_yet_available", False)
    if not capture_content:
        return None, CaptureInfo("not_captured", "disabled", False)
    if not source_available:
        return None, CaptureInfo("not_captured", "source_unavailable", False)

    try:
        normalized = normalize_json_value(source, field_name="content")
        sanitized, redacted = _sanitize_normalized_value(normalized)
        serialized = _canonical_json_dumps(sanitized)
        if len(serialized.encode("utf-8")) > MAX_CONTENT_BYTES:
            return None, CaptureInfo("not_captured", "size_limit", False)
    except Exception:
        # Privacy/tracing processing must not escape into Agent execution.
        return None, CaptureInfo("not_captured", "serialization_error", False)

    return sanitized, CaptureInfo("captured", None, redacted)


sanitize_json = sanitize_json_value
redact_json = sanitize_json_value
sanitize_content = capture_content


__all__ = [
    "DEFAULT_CAPTURE_CONTENT",
    "MAX_CONTENT_BYTES",
    "MANDATORY_SENSITIVE_KEYS",
    "REDACTION_PLACEHOLDER",
    "SENSITIVE_KEYS",
    "capture_content",
    "redact_json",
    "sanitize_content",
    "sanitize_json",
    "sanitize_json_object",
    "sanitize_json_value",
    "sanitize_text",
]
