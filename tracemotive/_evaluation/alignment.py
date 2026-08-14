"""Evaluation-only implementation of Simple Structural Alignment v1.

This module is deliberately below a private package.  It is not imported by
the public SDK, Collector, Query API, CLI, or frontend.  V02-19 uses it to
measure the exact structural contract before V02-20 introduces production
comparison behavior.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal, Sequence

from tracemotive.canonical import Span, Trace
from tracemotive.storage.repository import timestamp_to_us


Side = Literal["left", "right"]
UnavailableReason = Literal["missing_parent", "cycle", "invalid_structure"]


@dataclass(frozen=True, slots=True)
class StructuralSegment:
    type: str
    operation: str
    name: str
    ordinal: int

    def sort_key(self) -> tuple[str, str, str, int]:
        return self.type, self.operation, self.name, self.ordinal

    def to_dict(self) -> dict[str, str | int]:
        return {
            "type": self.type,
            "operation": self.operation,
            "name": self.name,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class StructuralKey:
    parent_path: tuple[StructuralSegment, ...]
    segment: StructuralSegment

    def sort_key(self) -> tuple[tuple[tuple[str, str, str, int], ...], tuple[str, str, str, int]]:
        return (
            tuple(segment.sort_key() for segment in self.parent_path),
            self.segment.sort_key(),
        )

    def semantic_path(self) -> tuple[StructuralSegment, ...]:
        return self.parent_path + (self.segment,)

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_path": [segment.to_dict() for segment in self.parent_path],
            "type": self.segment.type,
            "operation": self.segment.operation,
            "name": self.segment.name,
            "ordinal": self.segment.ordinal,
        }


@dataclass(frozen=True, slots=True)
class AlignmentMetrics:
    left_total: int
    right_total: int
    matched: int
    left_only: int
    right_only: int
    ambiguous_groups: int
    unavailable: int
    match_coverage: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "left_total": self.left_total,
            "right_total": self.right_total,
            "matched": self.matched,
            "left_only": self.left_only,
            "right_only": self.right_only,
            "ambiguous_groups": self.ambiguous_groups,
            "unavailable": self.unavailable,
            "match_coverage": self.match_coverage,
        }


@dataclass(frozen=True, slots=True)
class AlignmentReport:
    metrics: AlignmentMetrics
    spans: tuple[dict[str, object], ...]
    ambiguous_groups: tuple[dict[str, object], ...]
    unavailable_spans: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics.to_dict(),
            "spans": list(self.spans),
            "ambiguous_groups": list(self.ambiguous_groups),
            "unavailable_spans": list(self.unavailable_spans),
        }


@dataclass(frozen=True, slots=True)
class _Entry:
    index: int
    span: Span


def _ref(trace_id: str, span: Span) -> dict[str, str]:
    return {"trace_id": trace_id, "span_id": span.span_id}


def _sort_span_indices(spans: Sequence[Span], indices: list[int]) -> list[int]:
    return sorted(
        indices,
        key=lambda index: (timestamp_to_us(spans[index].started_at), spans[index].span_id),
    )


def _structural_state(
    spans: Sequence[Span],
) -> tuple[
    dict[int, tuple[int, ...]],
    dict[int, UnavailableReason],
]:
    """Resolve parent chains without using native or Canonical IDs as keys."""

    by_span_id: dict[str, list[int]] = defaultdict(list)
    for index, span in enumerate(spans):
        by_span_id[span.span_id].append(index)

    memo: dict[int, tuple[tuple[int, ...] | None, UnavailableReason | None]] = {}

    def resolve(index: int, stack: tuple[int, ...]) -> tuple[tuple[int, ...] | None, UnavailableReason | None]:
        if index in memo:
            return memo[index]
        if index in stack:
            return None, "cycle"

        span = spans[index]
        parent_id = span.parent_span_id
        if parent_id is None:
            result: tuple[tuple[int, ...] | None, UnavailableReason | None] = ((), None)
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

        parent_index = candidates[0]
        parent_path, reason = resolve(parent_index, stack + (index,))
        if reason is not None or parent_path is None:
            result = (None, reason or "invalid_structure")
        else:
            result = (parent_path + (parent_index,), None)
        memo[index] = result
        return result

    paths: dict[int, tuple[int, ...]] = {}
    unavailable: dict[int, UnavailableReason] = {}
    for index in range(len(spans)):
        path, reason = resolve(index, ())
        if reason is None and path is not None:
            paths[index] = path
        else:
            unavailable[index] = reason or "invalid_structure"
    return paths, unavailable


def _keys_for_side(
    spans: Sequence[Span],
) -> tuple[dict[StructuralKey, list[int]], dict[int, UnavailableReason]]:
    parent_paths, unavailable = _structural_state(spans)
    groups: dict[tuple[int | None, str, str, str], list[int]] = defaultdict(list)
    for index, parent_path in parent_paths.items():
        span = spans[index]
        parent_index = parent_path[-1] if parent_path else None
        groups[(parent_index, span.type, span.operation, span.name)].append(index)

    segments: dict[int, StructuralSegment] = {}
    for (parent_index, span_type, operation, name), indices in groups.items():
        ordered = _sort_span_indices(spans, indices)
        position = 0
        cursor = 0
        while cursor < len(ordered):
            current_sort = (
                timestamp_to_us(spans[ordered[cursor]].started_at),
                spans[ordered[cursor]].span_id,
            )
            end = cursor + 1
            while end < len(ordered) and (
                timestamp_to_us(spans[ordered[end]].started_at),
                spans[ordered[end]].span_id,
            ) == current_sort:
                end += 1

            # A duplicate exact sort key cannot be ordered independently from
            # the input sequence.  Give the tied entries one key so the
            # collision is reported rather than guessed through.
            ordinal = position
            for ordered_index in ordered[cursor:end]:
                segments[ordered_index] = StructuralSegment(
                    span_type,
                    operation,
                    name,
                    ordinal,
                )
            position += end - cursor
            cursor = end

    keys: dict[StructuralKey, list[int]] = defaultdict(list)
    for index, parent_path in parent_paths.items():
        parent_segments = tuple(segments[parent_index] for parent_index in parent_path)
        key = StructuralKey(parent_segments, segments[index])
        keys[key].append(index)
    return keys, unavailable


def _unavailable_records(
    side: Side,
    trace_id: str,
    spans: Sequence[Span],
    unavailable: dict[int, UnavailableReason],
) -> list[dict[str, object]]:
    return [
        {
            "side": side,
            "span": _ref(trace_id, spans[index]),
            "reason": reason,
        }
        for index, reason in sorted(
            unavailable.items(),
            key=lambda item: (
                timestamp_to_us(spans[item[0]].started_at),
                spans[item[0]].span_id,
            ),
        )
    ]


def _unavailable_sort_key(
    record: dict[str, object],
    spans: Sequence[Span],
) -> tuple[int, str]:
    span_ref = record["span"]
    if not isinstance(span_ref, dict):
        raise AssertionError("alignment unavailable record has an invalid span reference")
    span_id = span_ref["span_id"]
    span = next(span for span in spans if span.span_id == span_id)
    return timestamp_to_us(span.started_at), span.span_id


def align_traces(
    left_trace: Trace,
    left_spans: Sequence[Span],
    right_trace: Trace,
    right_spans: Sequence[Span],
) -> AlignmentReport:
    """Evaluate exact Simple Structural Alignment v1 for two Canonical runs."""

    left_keys, left_unavailable = _keys_for_side(left_spans)
    right_keys, right_unavailable = _keys_for_side(right_spans)

    span_results: list[dict[str, object]] = []
    ambiguous_groups: list[dict[str, object]] = []
    matched = left_only = right_only = 0

    all_keys = sorted(set(left_keys) | set(right_keys), key=StructuralKey.sort_key)
    for key in all_keys:
        left_indices = left_keys.get(key, [])
        right_indices = right_keys.get(key, [])
        if len(left_indices) > 1 or len(right_indices) > 1:
            ambiguous_groups.append(
                {
                    "key": key.to_dict(),
                    "left": [_ref(left_trace.trace_id, left_spans[index]) for index in left_indices],
                    "right": [_ref(right_trace.trace_id, right_spans[index]) for index in right_indices],
                    "reason": "structural_key_collision",
                }
            )
            continue

        if left_indices and right_indices:
            alignment = "matched"
            matched += 1
            left_ref = _ref(left_trace.trace_id, left_spans[left_indices[0]])
            right_ref = _ref(right_trace.trace_id, right_spans[right_indices[0]])
        elif left_indices:
            alignment = "left_only"
            left_only += 1
            left_ref = _ref(left_trace.trace_id, left_spans[left_indices[0]])
            right_ref = None
        else:
            alignment = "right_only"
            right_only += 1
            left_ref = None
            right_ref = _ref(right_trace.trace_id, right_spans[right_indices[0]])

        span_results.append(
            {
                "alignment": alignment,
                "semantic_path": [segment.to_dict() for segment in key.semantic_path()],
                "left": left_ref,
                "right": right_ref,
                "differences": [],
                "uncertainties": [],
            }
        )

    unavailable_records = _unavailable_records(
        "left",
        left_trace.trace_id,
        left_spans,
        left_unavailable,
    )
    unavailable_records.extend(
        _unavailable_records(
            "right",
            right_trace.trace_id,
            right_spans,
            right_unavailable,
        )
    )
    unavailable_records.sort(
        key=lambda record: (
            0 if record["side"] == "left" else 1,
            _unavailable_sort_key(
                record,
                left_spans if record["side"] == "left" else right_spans,
            ),
        )
    )
    ambiguous_groups.sort(
        key=lambda group: (
            tuple(
                (
                    segment["type"],
                    segment["operation"],
                    segment["name"],
                    segment["ordinal"],
                )
                for segment in group["key"]["parent_path"]
            ),
            group["key"]["type"],
            group["key"]["operation"],
            group["key"]["name"],
            group["key"]["ordinal"],
        )
    )

    left_total = len(left_spans)
    right_total = len(right_spans)
    denominator = max(left_total, right_total)
    coverage = 1.0 if denominator == 0 else matched / denominator
    metrics = AlignmentMetrics(
        left_total,
        right_total,
        matched,
        left_only,
        right_only,
        len(ambiguous_groups),
        len(unavailable_records),
        coverage,
    )
    return AlignmentReport(
        metrics,
        tuple(span_results),
        tuple(ambiguous_groups),
        tuple(unavailable_records),
    )


__all__ = ["AlignmentMetrics", "AlignmentReport", "StructuralKey", "StructuralSegment", "align_traces"]
