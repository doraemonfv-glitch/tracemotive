"""Bounded, deterministic structural diff for already-sanitized JSON values.

This module is deliberately independent of trace alignment.  It compares two
values that an authoritative finding has already established as comparable;
it never chooses span identity, infers moves, or assigns meaning to a path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from tracemotive.canonical.models import _ExactNumber, _canonical_json_dumps


MAX_STRUCTURED_DIFF_DEPTH = 32
MAX_STRUCTURED_DIFF_NODES = 4_096
MAX_STRUCTURED_DIFF_RECORDS = 256
MAX_STRUCTURED_DIFF_VALUE_BYTES = 64 * 1024

_MISSING = object()


@dataclass(frozen=True, slots=True)
class StructuredDiffResult:
    """The records collected before an explicit projection bound, if any."""

    records: tuple[dict[str, Any], ...]
    truncated: bool
    reason: str | None


def _pointer(parts: Sequence[str]) -> str:
    if not parts:
        return ""
    return "/" + "/".join(
        part.replace("~", "~0").replace("/", "~1") for part in parts
    )


def _unescape_pointer_part(part: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(part):
        char = part[index]
        if char != "~":
            result.append(char)
            index += 1
            continue
        if index + 1 >= len(part) or part[index + 1] not in {"0", "1"}:
            raise ValueError("invalid JSON Pointer escape")
        result.append("~" if part[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _path_prefix(value: str) -> tuple[str, ...]:
    if value == "":
        return ()
    if not value.startswith("/"):
        raise ValueError("structured diff path prefix must be a JSON Pointer")
    return tuple(_unescape_pointer_part(part) for part in value[1:].split("/"))


def _is_scalar(value: Any) -> bool:
    return value is None or type(value) in {bool, int, float, str, _ExactNumber}


def _scalar_type(value: Any) -> type[Any]:
    return type(value)


def _same_scalar(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, _ExactNumber):
        return left.value == right.value
    return left == right


def _canonical_equal(left: Any, right: Any) -> bool:
    return _canonical_json_dumps(left) == _canonical_json_dumps(right)


def _simple_array(value: list[Any]) -> bool:
    if not value:
        return True
    if not all(_is_scalar(item) for item in value):
        return False
    # A mixed scalar array has no single conservative index interpretation.
    return len({_scalar_type(item) for item in value}) == 1


def _wrapper(value: Any) -> dict[str, Any]:
    if value is _MISSING:
        return {"state": "absent", "value": None}
    return {"state": "present", "value": value}


def _record_size(record: dict[str, Any]) -> int:
    return len(_canonical_json_dumps(record).encode("utf-8"))


def structured_diff(
    left: Any,
    right: Any,
    *,
    path_prefix: str = "",
    max_depth: int = MAX_STRUCTURED_DIFF_DEPTH,
    max_nodes: int = MAX_STRUCTURED_DIFF_NODES,
    max_records: int = MAX_STRUCTURED_DIFF_RECORDS,
    max_value_bytes: int = MAX_STRUCTURED_DIFF_VALUE_BYTES,
) -> StructuredDiffResult:
    """Compare two canonical JSON values using only safe structural rules.

    Object keys are visited in sorted order.  Scalar arrays are compared by
    position only.  Arrays containing objects, arrays, or mixed scalar types
    are represented by one whole-array replacement, so this function never
    claims element identity or move semantics.
    """

    if type(max_depth) is not int or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    if type(max_nodes) is not int or max_nodes < 1:
        raise ValueError("max_nodes must be a positive integer")
    if type(max_records) is not int or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    if type(max_value_bytes) is not int or max_value_bytes < 1:
        raise ValueError("max_value_bytes must be a positive integer")

    prefix = _path_prefix(path_prefix)
    records: list[dict[str, Any]] = []
    stack: list[tuple[tuple[str, ...], Any, Any]] = [(prefix, left, right)]
    visited_nodes = 0
    truncated = False
    truncation_reason: str | None = None

    def truncate(reason: str) -> None:
        nonlocal truncated, truncation_reason
        if not truncated:
            truncated = True
            truncation_reason = reason

    def add_record(
        path: tuple[str, ...],
        op: str,
        left_value: Any,
        right_value: Any,
        reason: str | None = None,
    ) -> bool:
        if len(records) >= max_records:
            truncate("max_change_records")
            return False
        record = {
            "op": op,
            "path": _pointer(path),
            "left": _wrapper(left_value),
            "right": _wrapper(right_value),
            "reason": reason,
        }
        if _record_size(record) > max_value_bytes:
            truncate("max_value_bytes")
            return False
        records.append(record)
        return True

    while stack:
        path, left_value, right_value = stack.pop()
        if len(path) > max_depth:
            truncate("max_depth")
            continue
        if visited_nodes >= max_nodes:
            truncate("max_nodes")
            break
        visited_nodes += 1

        if left_value is _MISSING:
            if not add_record(path, "add", left_value, right_value):
                break
            continue
        if right_value is _MISSING:
            if not add_record(path, "remove", left_value, right_value):
                break
            continue

        left_is_dict = type(left_value) is dict
        right_is_dict = type(right_value) is dict
        left_is_list = type(left_value) is list
        right_is_list = type(right_value) is list

        if left_is_dict and right_is_dict:
            if len(path) >= max_depth:
                if not _canonical_equal(left_value, right_value):
                    truncate("max_depth")
                continue
            keys = sorted(set(left_value) | set(right_value), reverse=True)
            for key in keys:
                stack.append(
                    (
                        path + (key,),
                        left_value[key] if key in left_value else _MISSING,
                        right_value[key] if key in right_value else _MISSING,
                    )
                )
            continue

        if left_is_list and right_is_list:
            if _simple_array(left_value) and _simple_array(right_value):
                if len(path) >= max_depth and not _canonical_equal(left_value, right_value):
                    truncate("max_depth")
                    continue
                for index in range(max(len(left_value), len(right_value)) - 1, -1, -1):
                    stack.append(
                        (
                            path + (str(index),),
                            left_value[index] if index < len(left_value) else _MISSING,
                            right_value[index] if index < len(right_value) else _MISSING,
                        )
                    )
            elif not _canonical_equal(left_value, right_value):
                if not add_record(
                    path,
                    "replace",
                    left_value,
                    right_value,
                ):
                    break
            continue

        if left_is_dict or right_is_dict or left_is_list or right_is_list:
            if not add_record(path, "replace", left_value, right_value):
                break
            continue

        if not _same_scalar(left_value, right_value):
            if not add_record(path, "replace", left_value, right_value):
                break

    return StructuredDiffResult(tuple(records), truncated, truncation_reason)


__all__ = [
    "MAX_STRUCTURED_DIFF_DEPTH",
    "MAX_STRUCTURED_DIFF_NODES",
    "MAX_STRUCTURED_DIFF_RECORDS",
    "MAX_STRUCTURED_DIFF_VALUE_BYTES",
    "StructuredDiffResult",
    "structured_diff",
]
