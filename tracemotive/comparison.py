"""Read-only comparison of persisted Canonical TraceMotive observations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from tracemotive.canonical.models import (
    Span,
    Trace,
    _ExactNumber,
    _canonical_json_dumps,
)
from tracemotive.storage.repository import TraceQueryRecord, timestamp_to_us


MAX_COMPARISON_SPANS = 10_000
MAX_DIFFERENCE_RECORDS = 4_096
MAX_COMPARISON_RESPONSE_BYTES = 4 * 1024 * 1024

_MISSING = object()
_ABSENT_PARENT = object()


class ComparisonTooLargeError(ValueError):
    """Raised when a comparison exceeds the fixed response safety limits."""


@dataclass(frozen=True, slots=True)
class _TraceValue:
    known: bool
    value: Any


@dataclass(frozen=True, slots=True)
class _Structure:
    spans: tuple[Span, ...]
    paths: dict[int, tuple[int, ...]]
    unavailable: dict[int, str]
    children: dict[int | None, tuple[int, ...]]
    children_by_parent_id: dict[str, tuple[int, ...]]


def _canonical_equal(left: Any, right: Any) -> bool:
    return _canonical_json_dumps(left) == _canonical_json_dumps(right)


def _timestamp_key(span: Span) -> tuple[int, str]:
    started_at = timestamp_to_us(span.started_at)
    if started_at is None:
        raise RuntimeError("stored Span has no started_at")
    return started_at, span.span_id


def _latency_ms(span_or_trace: Span | Trace) -> _ExactNumber | None:
    ended_at = span_or_trace.ended_at
    if ended_at is None:
        return None
    started_at = timestamp_to_us(span_or_trace.started_at)
    ended = timestamp_to_us(ended_at)
    if started_at is None or ended is None or ended < started_at:
        raise RuntimeError("stored timestamp ordering is invalid")
    return _ExactNumber(Decimal(ended - started_at) / Decimal(1000))


def _build_structure(spans: Sequence[Span]) -> _Structure:
    ordered_spans = tuple(spans)
    by_span_id: dict[str, list[int]] = defaultdict(list)
    children_by_parent_id: dict[str, list[int]] = defaultdict(list)
    for index, span in enumerate(ordered_spans):
        by_span_id[span.span_id].append(index)
        if span.parent_span_id is not None:
            children_by_parent_id[span.parent_span_id].append(index)

    duplicate_indices = {
        index
        for indices in by_span_id.values()
        if len(indices) > 1
        for index in indices
    }
    memo: dict[int, tuple[tuple[int, ...] | None, str | None]] = {}

    def resolve(index: int, stack: tuple[int, ...]) -> tuple[tuple[int, ...] | None, str | None]:
        if index in memo:
            return memo[index]
        if index in duplicate_indices:
            result = (None, "invalid_structure")
            memo[index] = result
            return result
        if index in stack:
            return None, "cycle"
        parent_id = ordered_spans[index].parent_span_id
        if parent_id is None:
            result = ((), None)
            memo[index] = result
            return result
        candidates = by_span_id.get(parent_id, [])
        if not candidates:
            result = (None, "missing_parent")
            memo[index] = result
            return result
        if len(candidates) != 1:
            result = (None, "invalid_structure")
            memo[index] = result
            return result
        parent_path, reason = resolve(candidates[0], stack + (index,))
        if reason is not None or parent_path is None:
            result = (None, reason or "invalid_structure")
        else:
            result = (parent_path + (candidates[0],), None)
        memo[index] = result
        return result

    paths: dict[int, tuple[int, ...]] = {}
    unavailable: dict[int, str] = {}
    children: dict[int | None, list[int]] = defaultdict(list)
    for index in range(len(ordered_spans)):
        path, reason = resolve(index, ())
        if reason is None and path is not None:
            paths[index] = path
            children[path[-1] if path else None].append(index)
        else:
            unavailable[index] = reason or "invalid_structure"

    sorted_children = {
        parent: tuple(sorted(indices, key=lambda i: _timestamp_key(ordered_spans[i])))
        for parent, indices in children.items()
    }
    return _Structure(
        ordered_spans,
        paths,
        unavailable,
        sorted_children,
        {
            parent_id: tuple(sorted(indices, key=lambda i: _timestamp_key(ordered_spans[i])))
            for parent_id, indices in children_by_parent_id.items()
        },
    )


def _signature(span: Span) -> tuple[str, str, str]:
    return span.type, span.operation, span.name


def _segment(span: Span, ordinal: int) -> dict[str, str | int]:
    return {
        "type": span.type,
        "operation": span.operation,
        "name": span.name,
        "ordinal": ordinal,
    }


def _signature_dict(signature: tuple[str, str, str]) -> dict[str, str]:
    return {
        "type": signature[0],
        "operation": signature[1],
        "name": signature[2],
    }


def _ref(trace_id: str, span: Span) -> dict[str, str]:
    return {"trace_id": trace_id, "span_id": span.span_id}


def _pointer(parts: Sequence[str]) -> str:
    if not parts:
        return ""
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _lookup(value: Any, parts: Sequence[str]) -> Any:
    current = value
    for part in parts:
        if type(current) is not dict or part not in current:
            return _MISSING
        current = current[part]
    return current


_SPAN_FIELDS: tuple[tuple[str, ...], ...] = tuple(
    tuple(path.split("."))
    for path in (
        "source.framework",
        "source.framework_version",
        "source.integration",
        "source.integration_version",
        "parent_path",
        "type",
        "operation",
        "name",
        "status",
        "error.type",
        "error.message",
        "details.kind",
        "details.agent_name",
        "details.agent_version",
        "details.provider",
        "details.request_model",
        "details.response_model",
        "details.response_id",
        "details.request_parameters",
        "details.usage",
        "details.finish_reasons",
        "details.estimated_cost",
        "details.tool_name",
        "details.tool_call_id",
        "details.from_agent",
        "details.to_agent",
        "details.source_type",
        "input",
        "output",
        "capture.input",
        "capture.output",
        "started_at",
        "ended_at",
        "latency_ms",
        "metadata",
        "attributes",
    )
)


def _span_projection(span: Span, semantic_path: Sequence[dict[str, str | int]]) -> dict[str, Any]:
    projection = span.to_dict()
    projection["parent_path"] = list(semantic_path[:-1])
    projection["latency_ms"] = _latency_ms(span)
    return projection


def _field_result(
    path: tuple[str, ...],
    state: str,
    left: Any,
    right: Any,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "path": _pointer(path),
        "state": state,
        "left": None if left is _MISSING else left,
        "right": None if right is _MISSING else right,
        "reason": reason,
    }


def _recursive_field_results(
    path: tuple[str, ...],
    left: Any,
    right: Any,
    *,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    if left is _MISSING and right is _MISSING:
        return []
    if left is _MISSING:
        return [_field_result(path, "right_only", left, right, reason or "missing_side")]
    if right is _MISSING:
        return [_field_result(path, "left_only", left, right, reason or "missing_side")]
    if type(left) is dict and type(right) is dict:
        results: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            results.extend(
                _recursive_field_results(
                    path + (key,),
                    left.get(key, _MISSING),
                    right.get(key, _MISSING),
                    reason=reason,
                )
            )
        return results
    if type(left) is list and type(right) is list:
        results = []
        for index in range(max(len(left), len(right))):
            results.extend(
                _recursive_field_results(
                    path + (str(index),),
                    left[index] if index < len(left) else _MISSING,
                    right[index] if index < len(right) else _MISSING,
                    reason=reason,
                )
            )
        return results
    if _canonical_equal(left, right):
        return []
    return [_field_result(path, "different", left, right, reason)]


def _compare_normal_field(
    path: tuple[str, ...],
    left_projection: dict[str, Any],
    right_projection: dict[str, Any],
    *,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    return _recursive_field_results(
        path,
        _lookup(left_projection, path),
        _lookup(right_projection, path),
        reason=reason,
    )


def _compare_capture_field(
    path: tuple[str, ...],
    left_projection: dict[str, Any],
    right_projection: dict[str, Any],
    left_info: Any,
    right_info: Any,
) -> list[dict[str, Any]]:
    reason = (
        "redacted_observation"
        if left_info.redacted or right_info.redacted
        else "capture_unavailable"
        if left_info.state != "captured" or right_info.state != "captured"
        else None
    )
    return _compare_normal_field(path, left_projection, right_projection, reason=reason)


def _compare_content_field(
    path: tuple[str, ...],
    left_span: Span,
    right_span: Span,
    left_projection: dict[str, Any],
    right_projection: dict[str, Any],
    left_info: Any,
    right_info: Any,
) -> list[dict[str, Any]]:
    left = _lookup(left_projection, path)
    right = _lookup(right_projection, path)
    left_captured = left_info.state == "captured"
    right_captured = right_info.state == "captured"
    if not left_captured or not right_captured:
        return [
            _field_result(
                path,
                "unknown",
                left if left_captured else _MISSING,
                right if right_captured else _MISSING,
                "capture_unavailable",
            )
        ]
    reason = "redacted_observation" if left_info.redacted or right_info.redacted else None
    return _recursive_field_results(path, left, right, reason=reason)


def _span_field_results(
    left_span: Span,
    right_span: Span,
    semantic_path: Sequence[dict[str, str | int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    left_projection = _span_projection(left_span, semantic_path)
    right_projection = _span_projection(right_span, semantic_path)
    differences: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []
    for path in _SPAN_FIELDS:
        if path == ("input",):
            results = _compare_content_field(
                path,
                left_span,
                right_span,
                left_projection,
                right_projection,
                left_span.capture.input,
                right_span.capture.input,
            )
        elif path == ("output",):
            results = _compare_content_field(
                path,
                left_span,
                right_span,
                left_projection,
                right_projection,
                left_span.capture.output,
                right_span.capture.output,
            )
        elif path == ("capture", "input"):
            results = _compare_capture_field(
                path,
                left_projection,
                right_projection,
                left_span.capture.input,
                right_span.capture.input,
            )
        elif path == ("capture", "output"):
            results = _compare_capture_field(
                path,
                left_projection,
                right_projection,
                left_span.capture.output,
                right_span.capture.output,
            )
        else:
            results = _compare_normal_field(path, left_projection, right_projection)
        for result in results:
            if result["state"] == "same":
                continue
            if result["state"] == "unknown":
                uncertainties.append(result)
            else:
                differences.append(result)
    return differences, uncertainties


def _trace_view(record: TraceQueryRecord) -> dict[str, Any]:
    trace = record.trace
    stats = record.stats
    return {
        "trace": trace.to_dict(),
        "stats": {
            "latency_ms": _latency_ms(trace),
            "span_count": stats.span_count,
            "error_count": stats.error_count,
            "llm_call_count": stats.llm_call_count,
            "input_tokens": stats.input_tokens,
            "output_tokens": stats.output_tokens,
        },
    }


def _known(value: Any) -> _TraceValue:
    return _TraceValue(True, value)


def _unknown(value: Any = None) -> _TraceValue:
    return _TraceValue(False, value)


def _trace_field_result(
    path: str,
    left: _TraceValue,
    right: _TraceValue,
) -> dict[str, Any]:
    if not left.known or not right.known:
        return {
            "path": path,
            "state": "unknown",
            "left": left.value if left.known else None,
            "right": right.value if right.known else None,
            "reason": "unsupported_observation",
        }
    if _canonical_equal(left.value, right.value):
        state = "same"
    else:
        state = "different"
    return {
        "path": path,
        "state": state,
        "left": left.value,
        "right": right.value,
        "reason": None,
    }


def _trace_fields(left: TraceQueryRecord, right: TraceQueryRecord) -> list[dict[str, Any]]:
    left_trace, right_trace = left.trace, right.trace
    left_fields = (
        ("status", _known(left_trace.status), _known(right_trace.status)),
        ("ended_at", _known(left_trace.ended_at), _known(right_trace.ended_at)),
        ("latency_ms", _known(_latency_ms(left_trace)) if left_trace.ended_at is not None else _unknown(), _known(_latency_ms(right_trace)) if right_trace.ended_at is not None else _unknown()),
        ("span_count", _known(left.stats.span_count), _known(right.stats.span_count)),
        ("error_count", _known(left.stats.error_count), _known(right.stats.error_count)),
        ("llm_call_count", _known(left.stats.llm_call_count), _known(right.stats.llm_call_count)),
        ("input_tokens", _known(left.stats.input_tokens) if left.stats.input_tokens is not None else _unknown(), _known(right.stats.input_tokens) if right.stats.input_tokens is not None else _unknown()),
        ("output_tokens", _known(left.stats.output_tokens) if left.stats.output_tokens is not None else _unknown(), _known(right.stats.output_tokens) if right.stats.output_tokens is not None else _unknown()),
    )
    return [_trace_field_result(path, left_value, right_value) for path, left_value, right_value in left_fields]


class _AlignmentBuilder:
    def __init__(
        self,
        left_trace_id: str,
        left_spans: Sequence[Span],
        right_trace_id: str,
        right_spans: Sequence[Span],
    ) -> None:
        self.left_trace_id = left_trace_id
        self.right_trace_id = right_trace_id
        self.left = _build_structure(left_spans)
        self.right = _build_structure(right_spans)
        self.spans: list[dict[str, Any]] = []
        self.ambiguous_groups: list[dict[str, Any]] = []
        self.unavailable: dict[tuple[str, int], str] = {
            ("left", index): reason for index, reason in self.left.unavailable.items()
        }
        self.unavailable.update(
            {("right", index): reason for index, reason in self.right.unavailable.items()}
        )

    def _groups(
        self,
        structure: _Structure,
        parent: int | None | object,
    ) -> dict[tuple[str, str, str], list[int]]:
        if parent is _ABSENT_PARENT:
            return {}
        groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for index in structure.children.get(parent, ()):
            groups[_signature(structure.spans[index])].append(index)
        return groups

    def _mark_ambiguous_subtree(self, side: str, structure: _Structure, index: int) -> None:
        stack = list(structure.children_by_parent_id.get(structure.spans[index].span_id, ()))
        visited: set[int] = set()
        while stack:
            child = stack.pop()
            if child in visited:
                continue
            visited.add(child)
            self.unavailable.setdefault((side, child), "ambiguous_parent")
            stack.extend(structure.children_by_parent_id.get(structure.spans[child].span_id, ()))

    def _add_one_sided(
        self,
        side: str,
        index: int,
        semantic_path: list[dict[str, str | int]],
    ) -> None:
        structure = self.left if side == "left" else self.right
        trace_id = self.left_trace_id if side == "left" else self.right_trace_id
        span = structure.spans[index]
        segment = _segment(span, 0)
        full_path = semantic_path + [segment]
        left_span = span if side == "left" else None
        right_span = span if side == "right" else None
        self.spans.append(
            {
                "alignment": "left_only" if side == "left" else "right_only",
                "semantic_path": full_path,
                "left": _ref(trace_id, span) if side == "left" else None,
                "right": _ref(trace_id, span) if side == "right" else None,
                "differences": [
                    {
                        "path": "",
                        "state": "left_only" if side == "left" else "right_only",
                        "left": left_span.to_dict() if left_span is not None else None,
                        "right": right_span.to_dict() if right_span is not None else None,
                        "reason": "missing_side",
                    }
                ],
                "uncertainties": [],
            }
        )
        self._process(index if side == "left" else _ABSENT_PARENT, index if side == "right" else _ABSENT_PARENT, full_path)

    def _process(
        self,
        left_parent: int | None | object,
        right_parent: int | None | object,
        parent_path: list[dict[str, str | int]],
    ) -> None:
        left_groups = self._groups(self.left, left_parent)
        right_groups = self._groups(self.right, right_parent)
        for signature in sorted(set(left_groups) | set(right_groups)):
            left_indices = left_groups.get(signature, [])
            right_indices = right_groups.get(signature, [])
            if len(left_indices) > 1 or len(right_indices) > 1:
                self.ambiguous_groups.append(
                    {
                        "alignment": "ambiguous_group",
                        "parent_path": list(parent_path),
                        "group_signature": _signature_dict(signature),
                        "left_count": len(left_indices),
                        "right_count": len(right_indices),
                        "resolved_members": [],
                        "ambiguous_members": {
                            "left": [
                                _ref(self.left_trace_id, self.left.spans[index])
                                for index in left_indices
                            ],
                            "right": [
                                _ref(self.right_trace_id, self.right.spans[index])
                                for index in right_indices
                            ],
                        },
                        "left_only_count": None,
                        "right_only_count": None,
                        "reason": "repeated_sibling_ambiguity",
                    }
                )
                for index in left_indices:
                    self._mark_ambiguous_subtree("left", self.left, index)
                for index in right_indices:
                    self._mark_ambiguous_subtree("right", self.right, index)
                continue

            if len(left_indices) == 1 and len(right_indices) == 1:
                left_index, right_index = left_indices[0], right_indices[0]
                segment = _segment(self.left.spans[left_index], 0)
                full_path = parent_path + [segment]
                differences, uncertainties = _span_field_results(
                    self.left.spans[left_index],
                    self.right.spans[right_index],
                    full_path,
                )
                self.spans.append(
                    {
                        "alignment": "exact_match",
                        "semantic_path": full_path,
                        "left": _ref(self.left_trace_id, self.left.spans[left_index]),
                        "right": _ref(self.right_trace_id, self.right.spans[right_index]),
                        "differences": differences,
                        "uncertainties": uncertainties,
                    }
                )
                self._process(left_index, right_index, full_path)
            elif len(left_indices) == 1:
                self._add_one_sided("left", left_indices[0], parent_path)
            elif len(right_indices) == 1:
                self._add_one_sided("right", right_indices[0], parent_path)

    def build(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        self._process(None, None, [])
        unavailable_records: list[tuple[int, tuple[int, str], dict[str, Any]]] = []
        for side, structure, trace_id in (
            ("left", self.left, self.left_trace_id),
            ("right", self.right, self.right_trace_id),
        ):
            for (record_side, index), reason in self.unavailable.items():
                if record_side != side:
                    continue
                span = structure.spans[index]
                unavailable_records.append(
                    (
                        0 if side == "left" else 1,
                        _timestamp_key(span),
                        {
                            "alignment": "unavailable",
                            "side": side,
                            "span": _ref(trace_id, span),
                            "reason": reason,
                        },
                    )
                )
        unavailable_records.sort(key=lambda item: (item[0], item[1]))
        return self.spans, self.ambiguous_groups, [item[2] for item in unavailable_records]


def compare_trace_inputs(
    left: TraceQueryRecord,
    left_spans: Sequence[Span],
    right: TraceQueryRecord,
    right_spans: Sequence[Span],
) -> dict[str, Any]:
    """Compare two persisted Canonical Trace inputs without mutating storage."""

    if len(left_spans) > MAX_COMPARISON_SPANS or len(right_spans) > MAX_COMPARISON_SPANS:
        raise ComparisonTooLargeError("comparison span limit exceeded")

    aligned = _AlignmentBuilder(left.trace.trace_id, left_spans, right.trace.trace_id, right_spans)
    span_results, ambiguous_groups, unavailable = aligned.build()
    trace_fields = _trace_fields(left, right)
    difference_count = sum(len(item["differences"]) for item in span_results)
    uncertainty_count = (
        sum(len(item["uncertainties"]) for item in span_results)
        + len(ambiguous_groups)
        + len(unavailable)
    )
    result = {
        "comparison_version": "0.2",
        "left_trace": _trace_view(left),
        "right_trace": _trace_view(right),
        "summary": {
            "trace_fields": trace_fields,
            "alignment": {
                "matched_spans": sum(item["alignment"] == "exact_match" for item in span_results),
                "left_only_spans": sum(item["alignment"] == "left_only" for item in span_results),
                "right_only_spans": sum(item["alignment"] == "right_only" for item in span_results),
                "ambiguous_groups": len(ambiguous_groups),
                "unavailable_spans": len(unavailable),
            },
            "difference_count": difference_count,
            "uncertainty_count": uncertainty_count,
        },
        "spans": span_results,
        "ambiguous_groups": ambiguous_groups,
        "unavailable_spans": unavailable,
    }

    emitted_records = sum(
        1
        for item in trace_fields
        if item["state"] != "same"
    ) + difference_count + sum(len(item["uncertainties"]) for item in span_results)
    if emitted_records > MAX_DIFFERENCE_RECORDS:
        raise ComparisonTooLargeError("comparison record limit exceeded")
    if len(_canonical_json_dumps(result).encode("utf-8")) > MAX_COMPARISON_RESPONSE_BYTES:
        raise ComparisonTooLargeError("comparison response limit exceeded")
    return result


__all__ = [
    "ComparisonTooLargeError",
    "MAX_COMPARISON_RESPONSE_BYTES",
    "MAX_COMPARISON_SPANS",
    "MAX_DIFFERENCE_RECORDS",
    "compare_trace_inputs",
]
