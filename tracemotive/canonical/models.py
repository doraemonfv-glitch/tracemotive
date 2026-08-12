"""Canonical TraceMotive v0.1 schema models.

This module deliberately depends only on the Python standard library.  It is
the boundary between framework adapters and the rest of TraceMotive, so it does
not import or know about any framework, transport, storage, or UI package.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
import json
import math
import re
from typing import Any, ClassVar, Literal, Mapping, TypeAlias, TypeVar, Union


AGENTLENS_SCHEMA_VERSION = "0.1"
STATUSES = frozenset({"unset", "ok", "error"})
SPAN_TYPES = frozenset({"agent", "llm", "tool", "handoff", "retrieval", "custom"})
CAPTURE_STATES = frozenset({"captured", "not_captured"})
CAPTURE_REASONS = frozenset(
    {
        "disabled",
        "source_unavailable",
        "not_yet_available",
        "size_limit",
        "serialization_error",
    }
)

TraceStatus: TypeAlias = Literal["unset", "ok", "error"]
SpanType: TypeAlias = Literal[
    "agent", "llm", "tool", "handoff", "retrieval", "custom"
]
JSONPrimitive: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
CanonicalTimestamp: TypeAlias = str


class ValidationError(ValueError):
    """Raised when a value cannot satisfy a frozen canonical contract."""


@dataclass(frozen=True, slots=True, eq=False)
class _ExactNumber:
    """Private exact representation for parsed or normalized JSON decimals."""

    value: Decimal

    def __post_init__(self) -> None:
        if not self.value.is_finite():
            _fail("canonical JSON number must be finite")

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, _ExactNumber):
            return self.value == other.value
        if type(other) is int:
            return self.value == Decimal(other)
        if type(other) is float and math.isfinite(other):
            return self.value == Decimal(repr(other))
        return NotImplemented


_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_JSON_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_MISSING = object()
_TModel = TypeVar("_TModel", bound="CanonicalModel")


def _fail(message: str) -> None:
    raise ValidationError(message)


def _require_string(value: Any, field_name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        _fail(f"{field_name} must be a string")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        _fail(f"{field_name} must be a boolean")
    return value


def _require_non_empty_string(value: Any, field_name: str, maximum: int) -> str:
    value = _require_string(value, field_name)
    if value == "":
        _fail(f"{field_name} must be non-empty")
    if len(value) > maximum:
        _fail(f"{field_name} must be at most {maximum} Unicode code points")
    return value


def _require_enum(value: Any, field_name: str, allowed: frozenset[str]) -> str:
    value = _require_string(value, field_name)
    if value not in allowed:
        _fail(f"{field_name} has invalid value: {value!r}")
    return value


def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field_name} must be an object")
    return value


def _check_exact_fields(data: Any, expected: frozenset[str], model_name: str) -> dict[str, Any]:
    data = _require_dict(data, model_name)
    actual = frozenset(data)
    missing = expected - actual
    extra = actual - expected
    if missing:
        _fail(f"{model_name} is missing fields: {sorted(missing)!r}")
    if extra:
        _fail(f"{model_name} has unknown fields: {sorted(extra)!r}")
    return data


def normalize_json_value(value: Any, *, field_name: str = "value") -> JSONValue:
    """Validate and recursively copy one canonical JSONValue.

    Only the Python representations of the frozen JSONValue alternatives are
    accepted.  In particular, JSON encoders are not allowed to coerce bytes,
    sets, datetime objects, non-string object keys, or non-finite numbers.
    """

    value_type = type(value)
    if value is None or value_type is bool or value_type is int or value_type is str:
        return value
    if value_type is _ExactNumber:
        return value
    if value_type is float:
        if not math.isfinite(value):
            _fail(f"{field_name} must not contain NaN or Infinity")
        return _ExactNumber(Decimal(repr(value)))
    if value_type not in (list, dict):
        _fail(f"{field_name} is not a JSONValue: {value_type.__name__}")

    normalized_root: JSONValue = [] if value_type is list else {}
    active_ids = {id(value)}
    stack: list[tuple[Any, Any, Any, str]] = [
        (
            value,
            normalized_root,
            enumerate(value) if value_type is list else iter(value.items()),
            field_name,
        )
    ]
    while stack:
        source, target, iterator, source_name = stack[-1]
        try:
            key, item = next(iterator)
        except StopIteration:
            active_ids.remove(id(source))
            stack.pop()
            continue

        if type(source) is dict:
            if type(key) is not str:
                _fail(f"{source_name} object keys must be strings")
            item_name = f"{source_name}.{key}"
        else:
            item_name = f"{source_name}[{key}]"

        item_type = type(item)
        if item is None or item_type is bool or item_type is int or item_type is str:
            normalized_item: JSONValue = item
        elif item_type is _ExactNumber:
            normalized_item = item
        elif item_type is float:
            if not math.isfinite(item):
                _fail(f"{item_name} must not contain NaN or Infinity")
            normalized_item = _ExactNumber(Decimal(repr(item)))
        elif item_type in (list, dict):
            item_id = id(item)
            if item_id in active_ids:
                _fail(f"{item_name} contains a cyclic JSONValue")
            normalized_item = [] if item_type is list else {}
            active_ids.add(item_id)
            stack.append(
                (
                    item,
                    normalized_item,
                    enumerate(item) if item_type is list else iter(item.items()),
                    item_name,
                )
            )
        else:
            _fail(f"{item_name} is not a JSONValue: {item_type.__name__}")

        if type(source) is dict:
            target[key] = normalized_item
        else:
            target.append(normalized_item)

    return normalized_root


def _normalize_json_object(value: Any, field_name: str) -> JSONObject:
    normalized = normalize_json_value(value, field_name=field_name)
    if not isinstance(normalized, dict):
        _fail(f"{field_name} must be a JSON object")
    return normalized


def _sanitize_text(value: str) -> str:
    from ..privacy import sanitize_text

    sanitized, _ = sanitize_text(value)
    return sanitized


def _sanitize_json_value(value: Any, field_name: str) -> tuple[JSONValue, bool]:
    from ..privacy import sanitize_json_value

    return sanitize_json_value(value, field_name=field_name)


def _sanitize_json_object(value: Any, field_name: str) -> tuple[JSONObject, bool]:
    from ..privacy import sanitize_json_object

    sanitized, redacted = sanitize_json_object(value, field_name=field_name)
    return sanitized, redacted


def validate_trace_id(value: Any) -> str:
    value = _require_string(value, "trace_id")
    if _TRACE_ID_RE.fullmatch(value) is None or value == "0" * 32:
        _fail("trace_id must be 32 lowercase hexadecimal characters and not all zero")
    return value


def validate_span_id(value: Any) -> str:
    value = _require_string(value, "span_id")
    if _SPAN_ID_RE.fullmatch(value) is None or value == "0" * 16:
        _fail("span_id must be 16 lowercase hexadecimal characters and not all zero")
    return value


def _parse_timestamp(value: Any, field_name: str) -> tuple[str, datetime]:
    value = _require_string(value, field_name)
    if _TIMESTAMP_RE.fullmatch(value) is None:
        _fail(
            f"{field_name} must be an RFC 3339 UTC timestamp using Z and at most microseconds"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"{field_name} is not a valid timestamp") from exc
    return value, parsed


def validate_timestamp(value: Any, *, field_name: str = "timestamp") -> CanonicalTimestamp:
    """Validate one wire CanonicalTimestamp and return it unchanged."""

    return _parse_timestamp(value, field_name)[0]


def _optional_timestamp(value: Any, field_name: str) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    return _parse_timestamp(value, field_name)


def _require_model(value: Any, model_type: type[_TModel], field_name: str) -> _TModel:
    if not isinstance(value, model_type):
        _fail(f"{field_name} must be {model_type.__name__}")
    return value


def _json_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("JSON document must contain an object")
    return value


class CanonicalModel:
    """Common deterministic serialization behavior for canonical models."""

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset()
    _COPY_ON_ACCESS_FIELDS: ClassVar[frozenset[str]] = frozenset()

    def __getattribute__(self, name: str) -> Any:
        if name != "_COPY_ON_ACCESS_FIELDS":
            copy_fields = object.__getattribute__(self, "_COPY_ON_ACCESS_FIELDS")
            if name in copy_fields:
                value = object.__getattribute__(self, name)
                if value is None:
                    return None
                return normalize_json_value(value, field_name=name)
        return object.__getattribute__(self, name)

    def to_dict(self) -> dict[str, Any]:
        if not is_dataclass(self):
            raise TypeError("CanonicalModel subclasses must be dataclasses")
        return _to_wire(self)

    def to_json(self) -> str:
        return _canonical_json_dumps(self.to_dict())

    serialize = to_json

    @classmethod
    def from_json(cls: type[_TModel], value: str) -> _TModel:
        if not isinstance(value, str):
            _fail("serialized canonical data must be a string")
        decoded = _parse_canonical_json(value)
        return cls.from_dict(decoded)  # type: ignore[attr-defined]

    deserialize = from_json


def _wire_frame(value: Any) -> tuple[Any, Any, str] | None:
    if isinstance(value, CanonicalModel):
        target: dict[str, Any] = {}
        iterator = iter((field.name, getattr(value, field.name)) for field in fields(value))
        return target, iterator, "dict"
    if type(value) is dict:
        return {}, iter(value.items()), "dict"
    if type(value) is list:
        return [], enumerate(value), "list"
    return None


def _to_wire(value: Any) -> Any:
    frame = _wire_frame(value)
    if frame is None:
        return value

    target, iterator, kind = frame
    active_ids = {id(value)}
    stack: list[tuple[Any, Any, Any, str]] = [(value, target, iterator, kind)]
    while stack:
        source, destination, source_iterator, source_kind = stack[-1]
        try:
            key, item = next(source_iterator)
        except StopIteration:
            active_ids.remove(id(source))
            stack.pop()
            continue

        child_frame = _wire_frame(item)
        if child_frame is None:
            wire_item = item
        else:
            if id(item) in active_ids:
                _fail("canonical value contains a cyclic container")
            wire_item, child_iterator, child_kind = child_frame
            active_ids.add(id(item))
            stack.append((item, wire_item, child_iterator, child_kind))

        if source_kind == "dict":
            destination[key] = wire_item
        else:
            destination.append(wire_item)

    return target


def _integer_to_decimal(value: int) -> str:
    """Convert an int without Python's configurable decimal-digit limit."""

    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    value = abs(value)
    chunks: list[str] = []
    base = 1_000_000_000
    while value:
        value, remainder = divmod(value, base)
        chunks.append(f"{remainder:09d}")
    return sign + chunks[-1].lstrip("0") + "".join(reversed(chunks[:-1]))


def _json_encode_string(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "".join(
        f"\\u{ord(character):04x}"
        if 0xD800 <= ord(character) <= 0xDFFF
        else character
        for character in encoded
    )


def _canonical_json_dumps(value: Any) -> str:
    """Encode canonical JSON iteratively with deterministic key ordering."""

    if isinstance(value, CanonicalModel):
        value = _to_wire(value)
    output: list[str] = []
    stack: list[tuple[str, Any]] = [("value", value)]
    while stack:
        task, item = stack.pop()
        if task == "text":
            output.append(item)
            continue
        if type(item) is _ExactNumber:
            output.append(str(item.value))
        elif item is None:
            output.append("null")
        elif type(item) is bool:
            output.append("true" if item else "false")
        elif type(item) is int:
            output.append(_integer_to_decimal(item))
        elif type(item) is float:
            if not math.isfinite(item):
                _fail("canonical JSON cannot contain NaN or Infinity")
            output.append(json.dumps(item, allow_nan=False, separators=(",", ":")))
        elif type(item) is str:
            output.append(_json_encode_string(item))
        elif type(item) is list:
            output.append("[")
            stack.append(("text", "]"))
            for index in range(len(item) - 1, -1, -1):
                if index < len(item) - 1:
                    stack.append(("text", ","))
                stack.append(("value", item[index]))
        elif type(item) is dict:
            for key in item:
                if type(key) is not str:
                    _fail("canonical JSON object keys must be strings")
            entries = sorted(item.items(), key=lambda entry: entry[0])
            output.append("{")
            stack.append(("text", "}"))
            for index in range(len(entries) - 1, -1, -1):
                key, entry_value = entries[index]
                if index < len(entries) - 1:
                    stack.append(("text", ","))
                stack.append(("value", entry_value))
                stack.append(("text", ":"))
                stack.append(("text", _json_encode_string(key)))
        else:
            _fail(f"value is not a JSONValue: {type(item).__name__}")
    return "".join(output)


class _CanonicalJSONParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0

    def parse(self) -> JSONValue:
        self._skip_whitespace()
        value, is_container = self._read_value()
        stack: list[dict[str, Any]] = []
        if is_container:
            stack.append(self._frame_for(value))

        while stack:
            frame = stack[-1]
            if frame["kind"] == "list":
                if frame["state"] in {"value_or_end", "value"}:
                    self._skip_whitespace()
                    if frame["state"] == "value_or_end" and self._peek() == "]":
                        self.index += 1
                        stack.pop()
                        continue
                    child, child_is_container = self._read_value()
                    frame["container"].append(child)
                    frame["state"] = "comma_or_end"
                    if child_is_container:
                        stack.append(self._frame_for(child))
                else:
                    self._skip_whitespace()
                    current = self._peek()
                    if current == ",":
                        self.index += 1
                        frame["state"] = "value"
                    elif current == "]":
                        self.index += 1
                        stack.pop()
                    else:
                        _fail("invalid JSON array separator")
            elif frame["state"] in {"key_or_end", "key"}:
                self._skip_whitespace()
                if frame["state"] == "key_or_end" and self._peek() == "}":
                    self.index += 1
                    stack.pop()
                    continue
                if self._peek() != '"':
                    _fail("JSON object keys must be strings")
                try:
                    key, self.index = json.decoder.scanstring(self.text, self.index + 1, True)
                except ValueError as exc:
                    raise ValidationError("invalid JSON object key") from exc
                self._skip_whitespace()
                if self._peek() != ":":
                    _fail("invalid JSON object separator")
                self.index += 1
                frame["pending_key"] = key
                frame["state"] = "value"
            elif frame["state"] == "value":
                child, child_is_container = self._read_value()
                frame["container"][frame["pending_key"]] = child
                frame["state"] = "comma_or_end"
                if child_is_container:
                    stack.append(self._frame_for(child))
            else:
                self._skip_whitespace()
                current = self._peek()
                if current == ",":
                    self.index += 1
                    frame["state"] = "key"
                elif current == "}":
                    self.index += 1
                    stack.pop()
                else:
                    _fail("invalid JSON object separator")

        self._skip_whitespace()
        if self.index != len(self.text):
            _fail("unexpected trailing JSON data")
        return value

    @staticmethod
    def _frame_for(value: list[Any] | dict[str, Any]) -> dict[str, Any]:
        if type(value) is list:
            return {"kind": "list", "container": value, "state": "value_or_end"}
        return {"kind": "dict", "container": value, "state": "key_or_end", "pending_key": None}

    def _read_value(self) -> tuple[JSONValue, bool]:
        self._skip_whitespace()
        current = self._peek()
        if current == "[":
            self.index += 1
            return [], True
        if current == "{":
            self.index += 1
            return {}, True
        if current == '"':
            try:
                value, self.index = json.decoder.scanstring(self.text, self.index + 1, True)
            except ValueError as exc:
                raise ValidationError("invalid JSON string") from exc
            return value, False
        for literal, value in (("null", None), ("true", True), ("false", False)):
            if self.text.startswith(literal, self.index):
                self.index += len(literal)
                return value, False
        match = _JSON_NUMBER_RE.match(self.text, self.index)
        if match is not None:
            token = match.group(0)
            self.index = match.end()
            if "." in token or "e" in token.lower():
                return _ExactNumber(Decimal(token)), False
            if token == "-0":
                return _ExactNumber(Decimal(token)), False
            return _parse_integer_token(token), False
        _fail("invalid JSON value")
        raise AssertionError("unreachable")

    def _skip_whitespace(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t\r\n":
            self.index += 1

    def _peek(self) -> str:
        if self.index >= len(self.text):
            _fail("unexpected end of JSON")
        return self.text[self.index]


def _parse_canonical_json(text: str) -> JSONValue:
    return _CanonicalJSONParser(text).parse()


def _parse_integer_token(token: str) -> int:
    negative = token.startswith("-")
    digits = token[1:] if negative else token
    value = 0
    for digit in digits:
        value = value * 10 + (ord(digit) - ord("0"))
    return -value if negative else value


@dataclass(frozen=True, slots=True)
class TraceSource(CanonicalModel):
    framework: str | None
    framework_version: str | None
    integration: str
    integration_version: str
    native_trace_id: str | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"framework", "framework_version", "integration", "integration_version", "native_trace_id"}
    )

    def __post_init__(self) -> None:
        _require_string(self.framework, "framework", nullable=True)
        _require_string(self.framework_version, "framework_version", nullable=True)
        _require_string(self.integration, "integration")
        _require_string(self.integration_version, "integration_version")
        _require_string(self.native_trace_id, "native_trace_id", nullable=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TraceSource":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "TraceSource")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class SpanSource(CanonicalModel):
    framework: str | None
    framework_version: str | None
    integration: str
    integration_version: str
    native_trace_id: str | None
    native_span_id: str | None
    native_parent_span_id: str | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "framework",
            "framework_version",
            "integration",
            "integration_version",
            "native_trace_id",
            "native_span_id",
            "native_parent_span_id",
        }
    )

    def __post_init__(self) -> None:
        _require_string(self.framework, "framework", nullable=True)
        _require_string(self.framework_version, "framework_version", nullable=True)
        _require_string(self.integration, "integration")
        _require_string(self.integration_version, "integration_version")
        _require_string(self.native_trace_id, "native_trace_id", nullable=True)
        _require_string(self.native_span_id, "native_span_id", nullable=True)
        _require_string(self.native_parent_span_id, "native_parent_span_id", nullable=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpanSource":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "SpanSource")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class Error(CanonicalModel):
    type: str | None
    message: str | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"type", "message"})

    def __post_init__(self) -> None:
        _require_string(self.type, "error.type", nullable=True)
        _require_string(self.message, "error.message", nullable=True)
        if self.message is not None:
            object.__setattr__(self, "message", _sanitize_text(self.message))
        if self.type is None and self.message is None:
            _fail("Error requires type or message")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Error":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "Error")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class CaptureInfo(CanonicalModel):
    state: str
    reason: str | None
    redacted: bool

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"state", "reason", "redacted"})

    def __post_init__(self) -> None:
        _require_enum(self.state, "capture.state", CAPTURE_STATES)
        _require_string(self.reason, "capture.reason", nullable=True)
        _require_bool(self.redacted, "capture.redacted")
        if self.state == "captured":
            if self.reason is not None:
                _fail("captured CaptureInfo must have reason=null")
        else:
            if self.reason not in CAPTURE_REASONS:
                _fail("not_captured CaptureInfo requires a valid reason")
            if self.redacted:
                _fail("not_captured CaptureInfo must have redacted=false")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CaptureInfo":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "CaptureInfo")
        _require_string(data["reason"], "capture.reason", nullable=True)
        return cls(**data)


@dataclass(frozen=True, slots=True)
class Capture(CanonicalModel):
    input: CaptureInfo
    output: CaptureInfo

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"input", "output"})

    def __post_init__(self) -> None:
        _require_model(self.input, CaptureInfo, "capture.input")
        _require_model(self.output, CaptureInfo, "capture.output")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Capture":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "Capture")
        return cls(
            input=CaptureInfo.from_dict(data["input"]),
            output=CaptureInfo.from_dict(data["output"]),
        )


@dataclass(frozen=True, slots=True)
class AgentDetails(CanonicalModel):
    kind: Literal["agent"]
    agent_name: str | None
    agent_version: str | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "agent_name", "agent_version"})

    def __post_init__(self) -> None:
        if self.kind != "agent":
            _fail("AgentDetails.kind must be 'agent'")
        _require_string(self.agent_name, "agent_name", nullable=True)
        _require_string(self.agent_version, "agent_version", nullable=True)
        if self.agent_name is not None:
            object.__setattr__(self, "agent_name", _sanitize_text(self.agent_name))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentDetails":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "AgentDetails")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class LLMUsage(CanonicalModel):
    input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        }
    )

    def __post_init__(self) -> None:
        for field_name in self._WIRE_FIELDS:
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                _fail(f"{field_name} must be a non-negative integer or null")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LLMUsage":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "LLMUsage")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class EstimatedCost(CanonicalModel):
    amount: int | float
    currency: str
    estimated: Literal[True]

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"amount", "currency", "estimated"})

    def __post_init__(self) -> None:
        amount = self.amount
        if type(amount) is int:
            if amount < 0:
                _fail("estimated_cost.amount must be non-negative")
        elif type(amount) is float:
            if not math.isfinite(amount):
                _fail("estimated_cost.amount must be a finite number")
            object.__setattr__(self, "amount", _ExactNumber(Decimal(repr(amount))))
            amount = self.amount
            if amount.value < 0:
                _fail("estimated_cost.amount must be non-negative")
        elif type(amount) is _ExactNumber:
            if not amount.value.is_finite():
                _fail("estimated_cost.amount must be a finite number")
            if amount.value < 0:
                _fail("estimated_cost.amount must be non-negative")
        else:
            _fail("estimated_cost.amount must be a finite number")
        _require_string(self.currency, "estimated_cost.currency")
        if self.currency == "" or not self.currency.isupper():
            _fail("estimated_cost.currency must be an uppercase string")
        if self.estimated is not True:
            _fail("estimated_cost.estimated must be true")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EstimatedCost":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "EstimatedCost")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class LLMDetails(CanonicalModel):
    kind: Literal["llm"]
    provider: str | None
    request_model: str | None
    response_model: str | None
    response_id: str | None
    usage: LLMUsage
    finish_reasons: list[str]
    request_parameters: JSONObject | None
    estimated_cost: EstimatedCost | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "kind",
            "provider",
            "request_model",
            "response_model",
            "response_id",
            "usage",
            "finish_reasons",
            "request_parameters",
            "estimated_cost",
        }
    )
    _COPY_ON_ACCESS_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"finish_reasons", "request_parameters"}
    )

    def __post_init__(self) -> None:
        if self.kind != "llm":
            _fail("LLMDetails.kind must be 'llm'")
        for field_name in ("provider", "request_model", "response_model", "response_id"):
            _require_string(getattr(self, field_name), field_name, nullable=True)
        _require_model(self.usage, LLMUsage, "usage")
        finish_reasons = normalize_json_value(self.finish_reasons, field_name="finish_reasons")
        if type(finish_reasons) is not list:
            _fail("finish_reasons must be an array")
        for index, reason in enumerate(finish_reasons):
            _require_string(reason, f"finish_reasons[{index}]")
        object.__setattr__(self, "finish_reasons", finish_reasons)
        if self.request_parameters is not None:
            normalized, _ = _sanitize_json_object(self.request_parameters, "request_parameters")
            object.__setattr__(self, "request_parameters", normalized)
        _require_model_or_none(self.estimated_cost, EstimatedCost, "estimated_cost")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LLMDetails":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "LLMDetails")
        return cls(
            kind=data["kind"],
            provider=data["provider"],
            request_model=data["request_model"],
            response_model=data["response_model"],
            response_id=data["response_id"],
            usage=LLMUsage.from_dict(data["usage"]),
            finish_reasons=data["finish_reasons"],
            request_parameters=(
                None
                if data["request_parameters"] is None
                else _normalize_json_object(data["request_parameters"], "request_parameters")
            ),
            estimated_cost=(
                None
                if data["estimated_cost"] is None
                else EstimatedCost.from_dict(data["estimated_cost"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolDetails(CanonicalModel):
    kind: Literal["tool"]
    tool_name: str
    tool_call_id: str | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "tool_name", "tool_call_id"})

    def __post_init__(self) -> None:
        if self.kind != "tool":
            _fail("ToolDetails.kind must be 'tool'")
        tool_name = _require_string(self.tool_name, "tool_name")
        if tool_name == "":
            _fail("tool_name must be non-empty")
        object.__setattr__(self, "tool_name", _sanitize_text(tool_name))
        _require_string(self.tool_call_id, "tool_call_id", nullable=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolDetails":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "ToolDetails")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class HandoffDetails(CanonicalModel):
    kind: Literal["handoff"]
    from_agent: str | None
    to_agent: str | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "from_agent", "to_agent"})

    def __post_init__(self) -> None:
        if self.kind != "handoff":
            _fail("HandoffDetails.kind must be 'handoff'")
        _require_string(self.from_agent, "from_agent", nullable=True)
        _require_string(self.to_agent, "to_agent", nullable=True)
        if self.from_agent is not None:
            object.__setattr__(self, "from_agent", _sanitize_text(self.from_agent))
        if self.to_agent is not None:
            object.__setattr__(self, "to_agent", _sanitize_text(self.to_agent))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HandoffDetails":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "HandoffDetails")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class RetrievalDetails(CanonicalModel):
    kind: Literal["retrieval"]

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind"})

    def __post_init__(self) -> None:
        if self.kind != "retrieval":
            _fail("RetrievalDetails.kind must be 'retrieval'")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RetrievalDetails":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "RetrievalDetails")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class CustomDetails(CanonicalModel):
    kind: Literal["custom"]
    source_type: str | None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "source_type"})

    def __post_init__(self) -> None:
        if self.kind != "custom":
            _fail("CustomDetails.kind must be 'custom'")
        _require_string(self.source_type, "source_type", nullable=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CustomDetails":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "CustomDetails")
        return cls(**data)


Details: TypeAlias = Union[
    AgentDetails,
    LLMDetails,
    ToolDetails,
    HandoffDetails,
    RetrievalDetails,
    CustomDetails,
]
_DETAIL_TYPES: dict[str, type[CanonicalModel]] = {
    "agent": AgentDetails,
    "llm": LLMDetails,
    "tool": ToolDetails,
    "handoff": HandoffDetails,
    "retrieval": RetrievalDetails,
    "custom": CustomDetails,
}


def _require_model_or_none(value: Any, model_type: type[_TModel], field_name: str) -> _TModel | None:
    if value is not None:
        return _require_model(value, model_type, field_name)
    return None


def details_from_dict(data: Mapping[str, Any]) -> Details:
    data = _require_dict(data, "details")
    kind = data.get("kind", _MISSING)
    if not isinstance(kind, str) or kind not in _DETAIL_TYPES:
        _fail("details.kind must be one of the canonical Span types")
    detail_type = _DETAIL_TYPES[kind]
    return detail_type.from_dict(data)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Trace(CanonicalModel):
    schema_version: str
    trace_id: str
    name: str
    started_at: CanonicalTimestamp
    ended_at: CanonicalTimestamp | None
    status: TraceStatus
    source: TraceSource
    metadata: JSONObject
    attributes: JSONObject

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "trace_id",
            "name",
            "started_at",
            "ended_at",
            "status",
            "source",
            "metadata",
            "attributes",
        }
    )
    _COPY_ON_ACCESS_FIELDS: ClassVar[frozenset[str]] = frozenset({"metadata", "attributes"})

    def __post_init__(self) -> None:
        if self.schema_version != AGENTLENS_SCHEMA_VERSION:
            _fail(f"schema_version must equal {AGENTLENS_SCHEMA_VERSION!r}")
        validate_trace_id(self.trace_id)
        name = _sanitize_text(_require_string(self.name, "name"))
        _require_non_empty_string(name, "name", 256)
        object.__setattr__(self, "name", name)
        _, started = _parse_timestamp(self.started_at, "started_at")
        _, ended = _optional_timestamp(self.ended_at, "ended_at")
        if ended is not None and ended < started:
            _fail("ended_at must not be before started_at")
        _require_enum(self.status, "status", STATUSES)
        _require_model(self.source, TraceSource, "source")
        metadata, _ = _sanitize_json_object(self.metadata, "metadata")
        attributes, _ = _sanitize_json_object(self.attributes, "attributes")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "attributes", attributes)
        if ended is None and self.status != "unset":
            _fail("an unfinished Trace must have status=unset")

    def validate_started(self) -> None:
        if self.ended_at is not None or self.status != "unset":
            _fail("trace.started requires ended_at=null and status=unset")

    def validate_ended(self) -> None:
        if self.ended_at is None:
            _fail("trace.ended requires ended_at")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Trace":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "Trace")
        return cls(
            schema_version=data["schema_version"],
            trace_id=data["trace_id"],
            name=data["name"],
            started_at=data["started_at"],
            ended_at=data["ended_at"],
            status=data["status"],
            source=TraceSource.from_dict(data["source"]),
            metadata=_normalize_json_object(data["metadata"], "metadata"),
            attributes=_normalize_json_object(data["attributes"], "attributes"),
        )


@dataclass(frozen=True, slots=True)
class Span(CanonicalModel):
    schema_version: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    type: SpanType
    operation: str
    name: str
    started_at: CanonicalTimestamp
    ended_at: CanonicalTimestamp | None
    status: TraceStatus
    error: Error | None
    input: JSONValue
    output: JSONValue
    capture: Capture
    source: SpanSource
    metadata: JSONObject
    attributes: JSONObject
    details: Details

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "trace_id",
            "span_id",
            "parent_span_id",
            "type",
            "operation",
            "name",
            "started_at",
            "ended_at",
            "status",
            "error",
            "input",
            "output",
            "capture",
            "source",
            "metadata",
            "attributes",
            "details",
        }
    )
    _COPY_ON_ACCESS_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"input", "output", "metadata", "attributes"}
    )

    def __post_init__(self) -> None:
        if self.schema_version != AGENTLENS_SCHEMA_VERSION:
            _fail(f"schema_version must equal {AGENTLENS_SCHEMA_VERSION!r}")
        validate_trace_id(self.trace_id)
        validate_span_id(self.span_id)
        if self.parent_span_id is not None:
            validate_span_id(self.parent_span_id)
        _require_enum(self.type, "type", SPAN_TYPES)
        operation = _sanitize_text(_require_string(self.operation, "operation"))
        name = _sanitize_text(_require_string(self.name, "name"))
        _require_non_empty_string(operation, "operation", 128)
        _require_non_empty_string(name, "name", 256)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "name", name)
        _, started = _parse_timestamp(self.started_at, "started_at")
        _, ended = _optional_timestamp(self.ended_at, "ended_at")
        if ended is not None and ended < started:
            _fail("ended_at must not be before started_at")
        _require_enum(self.status, "status", STATUSES)
        _require_model_or_none(self.error, Error, "error")
        _require_model(self.capture, Capture, "capture")
        if self.capture.input.state == "not_captured" and self.input is not None:
            _fail("not_captured input must be null")
        if self.capture.output.state == "not_captured" and self.output is not None:
            _fail("not_captured output must be null")

        from ..privacy import MAX_CONTENT_BYTES

        input_value, input_redacted = (None, False)
        capture_input = self.capture.input
        if capture_input.state == "captured":
            input_value, input_redacted = _sanitize_json_value(self.input, "input")
            if len(_canonical_json_dumps(input_value).encode("utf-8")) > MAX_CONTENT_BYTES:
                input_value = None
                input_redacted = False
                capture_input = CaptureInfo("not_captured", "size_limit", False)

        output_value, output_redacted = (None, False)
        capture_output = self.capture.output
        if capture_output.state == "captured":
            output_value, output_redacted = _sanitize_json_value(self.output, "output")
            if len(_canonical_json_dumps(output_value).encode("utf-8")) > MAX_CONTENT_BYTES:
                output_value = None
                output_redacted = False
                capture_output = CaptureInfo("not_captured", "size_limit", False)

        object.__setattr__(self, "input", input_value)
        object.__setattr__(self, "output", output_value)
        if capture_input.state == "captured" and capture_input.redacted != input_redacted:
            capture_input = CaptureInfo("captured", None, input_redacted)
        if capture_output.state == "captured" and capture_output.redacted != output_redacted:
            capture_output = CaptureInfo("captured", None, output_redacted)
        if capture_input != self.capture.input or capture_output != self.capture.output:
            object.__setattr__(self, "capture", Capture(capture_input, capture_output))
        _require_model(self.source, SpanSource, "source")
        metadata, _ = _sanitize_json_object(self.metadata, "metadata")
        attributes, _ = _sanitize_json_object(self.attributes, "attributes")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "attributes", attributes)
        if not isinstance(self.details, tuple(_DETAIL_TYPES.values())):
            _fail("details must be a canonical Details variant")
        if self.details.kind != self.type:
            _fail("details.kind must equal span.type")
        if ended is None:
            if self.status != "unset" or self.error is not None:
                _fail("an unfinished Span must have status=unset and error=null")
            if self.capture.output != CaptureInfo("not_captured", "not_yet_available", False):
                _fail("an unfinished Span must have not_yet_available output capture")
        else:
            if self.status not in {"ok", "error"}:
                _fail("a completed Span must have status=ok or status=error")
            if self.status == "ok" and self.error is not None:
                _fail("a Span with status=ok must have error=null")

    def validate_started(self) -> None:
        if self.ended_at is not None or self.status != "unset" or self.error is not None:
            _fail("span.started requires ended_at=null, status=unset, and error=null")
        if self.capture.output != CaptureInfo("not_captured", "not_yet_available", False):
            _fail("span.started requires not_yet_available output capture")

    def validate_ended(self) -> None:
        if self.ended_at is None or self.status not in {"ok", "error"}:
            _fail("span.ended requires ended_at and status=ok|error")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Span":
        data = _check_exact_fields(data, cls._WIRE_FIELDS, "Span")
        return cls(
            schema_version=data["schema_version"],
            trace_id=data["trace_id"],
            span_id=data["span_id"],
            parent_span_id=data["parent_span_id"],
            type=data["type"],
            operation=data["operation"],
            name=data["name"],
            started_at=data["started_at"],
            ended_at=data["ended_at"],
            status=data["status"],
            error=None if data["error"] is None else Error.from_dict(data["error"]),
            input=normalize_json_value(data["input"], field_name="input"),
            output=normalize_json_value(data["output"], field_name="output"),
            capture=Capture.from_dict(data["capture"]),
            source=SpanSource.from_dict(data["source"]),
            metadata=_normalize_json_object(data["metadata"], "metadata"),
            attributes=_normalize_json_object(data["attributes"], "attributes"),
            details=details_from_dict(data["details"]),
        )


def validate_trace_transition(previous: Trace, current: Trace) -> None:
    """Validate the frozen terminal Trace status transition rule."""

    _require_model(previous, Trace, "previous")
    _require_model(current, Trace, "current")
    if previous.trace_id != current.trace_id:
        _fail("Trace transitions require the same trace_id")
    if previous.status in {"ok", "error"} and current.status != previous.status:
        _fail("terminal Trace status cannot transition")
