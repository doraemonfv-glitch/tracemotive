"""Deterministic v0.3 evidence-supported behavioral divergence analysis.

This module is an additive read-model layer over the existing v0.2 structural
comparison.  It deliberately does not expose an HTTP route, alter v0.2
responses, or infer task relevance, causality, or root cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from tracemotive.canonical.models import Span, _canonical_json_dumps
from tracemotive.comparison import compare_trace_inputs
from tracemotive.storage.repository import TraceQueryRecord, timestamp_to_us


DivergenceState = Literal["none", "supported", "uncertain"]
CoordinateKind = Literal["span", "sibling_group", "trace_summary"]
MemberVariation = Literal["same", "order_only", "observations_changed"]

BEHAVIORAL_KINDS = frozenset(
    {
        "aligned_span_error_changed",
        "aligned_tool_input_changed",
        "aligned_tool_output_changed",
        "unique_tool_added",
        "unique_tool_removed",
        "execution_subtree_added",
        "execution_subtree_removed",
        "repeated_tool_group_cardinality_changed",
    }
)

_KIND_ORDER = {
    "aligned_span_error_changed": 0,
    "aligned_tool_output_changed": 1,
    "aligned_tool_input_changed": 2,
    "unique_tool_added": 3,
    "unique_tool_removed": 4,
    "repeated_tool_group_cardinality_changed": 5,
    "execution_subtree_added": 6,
    "execution_subtree_removed": 7,
}

_REASON_ORDER = {
    "error_observed": 0,
    "error_resolved": 1,
    "captured_values_differ": 2,
    "unique_tool_presence": 3,
    "deterministic_group_count_changed": 4,
    "structural_subtree_presence_changed": 5,
}

_GLOBAL_BARRIERS = frozenset({"cycle", "invalid_structure"})


@dataclass(frozen=True, slots=True)
class StructuralSegment:
    type: str
    operation: str
    name: str
    ordinal: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StructuralSegment":
        return cls(
            type=str(value["type"]),
            operation=str(value["operation"]),
            name=str(value["name"]),
            ordinal=int(value["ordinal"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "operation": self.operation,
            "name": self.name,
            "ordinal": self.ordinal,
        }

    def sort_key(self) -> tuple[str, str, str, int]:
        return self.type, self.operation, self.name, self.ordinal


@dataclass(frozen=True, slots=True)
class StructuralCoordinate:
    kind: CoordinateKind
    semantic_path: tuple[StructuralSegment, ...] = ()
    group_signature: StructuralSegment | None = None

    @classmethod
    def span(cls, semantic_path: Sequence[Mapping[str, Any]]) -> "StructuralCoordinate":
        return cls(
            "span",
            tuple(StructuralSegment.from_mapping(item) for item in semantic_path),
        )

    @classmethod
    def sibling_group(
        cls,
        parent_path: Sequence[Mapping[str, Any]],
        signature: Mapping[str, Any],
    ) -> "StructuralCoordinate":
        return cls(
            "sibling_group",
            tuple(StructuralSegment.from_mapping(item) for item in parent_path),
            StructuralSegment(
                type=str(signature["type"]),
                operation=str(signature["operation"]),
                name=str(signature["name"]),
                ordinal=0,
            ),
        )

    @classmethod
    def trace_summary(cls) -> "StructuralCoordinate":
        return cls("trace_summary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "semantic_path": [item.to_dict() for item in self.semantic_path],
            "group_signature": None
            if self.group_signature is None
            else {
                "type": self.group_signature.type,
                "operation": self.group_signature.operation,
                "name": self.group_signature.name,
            },
        }

    def sort_key(self) -> tuple[Any, ...]:
        if self.kind == "trace_summary":
            return (2, ())
        path = tuple(item.sort_key() for item in self.semantic_path)
        if self.kind == "sibling_group" and self.group_signature is not None:
            path = path + (self.group_signature.sort_key(),)
            kind_order = 0
        else:
            kind_order = 1
        return (0, path, kind_order)


@dataclass(frozen=True, slots=True)
class BehavioralCandidate:
    kind: str
    coordinate: StructuralCoordinate
    left: dict[str, str] | None
    right: dict[str, str] | None
    field_path: str | None
    reason_code: str
    evidence: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        if self.kind not in BEHAVIORAL_KINDS:
            raise ValueError(f"unsupported behavioral candidate kind: {self.kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "coordinate": self.coordinate.to_dict(),
            "left": self.left,
            "right": self.right,
            "field_path": self.field_path,
            "reason_code": self.reason_code,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class UncertaintyBarrier:
    reason_code: str
    side: Literal["left", "right", "both"]
    coordinate: StructuralCoordinate | None
    blocks_earlier_claim: bool
    evidence: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "side": self.side,
            "coordinate": None if self.coordinate is None else self.coordinate.to_dict(),
            "blocks_earlier_claim": self.blocks_earlier_claim,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class ReliablyMatchedPoint:
    state: Literal["none", "matched"]
    semantic_path: tuple[StructuralSegment, ...]
    left: dict[str, str] | None
    right: dict[str, str] | None
    reason: Literal["no_prior_resolved_point", "before_first_finding", "before_uncertainty_barrier"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_path": [item.to_dict() for item in self.semantic_path],
            "left": self.left,
            "right": self.right,
            "state": self.state,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DivergenceResult:
    """V03-11 result; it contains a candidate, not an investigation summary."""

    state: DivergenceState
    candidate: BehavioralCandidate | None
    barriers: tuple[UncertaintyBarrier, ...]
    last_reliably_matched_point: ReliablyMatchedPoint

    def __post_init__(self) -> None:
        if self.state not in {"none", "supported", "uncertain"}:
            raise ValueError(f"unknown divergence state: {self.state}")
        if self.state == "supported" and self.candidate is None:
            raise ValueError("supported divergence requires a candidate")
        if self.state == "uncertain" and self.candidate is not None:
            raise ValueError("uncertain divergence cannot expose a confident candidate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ordering_basis": "structural_triage_order",
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "barriers": [item.to_dict() for item in self.barriers],
            "last_reliably_matched_point": self.last_reliably_matched_point.to_dict(),
        }

    def to_json(self) -> str:
        return _canonical_json_dumps(self.to_dict())


def _trace_complete(record: TraceQueryRecord) -> bool:
    return record.trace.ended_at is not None and record.trace.status != "unset"


def _signature(span: Span) -> tuple[str, str, str]:
    return span.type, span.operation, span.name


def _span_map(spans: Sequence[Span]) -> dict[str, Span]:
    return {span.span_id: span for span in spans}


def _ref_key(reference: Mapping[str, str] | None) -> tuple[str, str]:
    if reference is None:
        return "", ""
    return reference.get("trace_id", ""), reference.get("span_id", "")


def _ref_timestamp(reference: Mapping[str, str] | None, spans: Mapping[str, Span]) -> int:
    if reference is None:
        return -1
    span = spans.get(reference.get("span_id", ""))
    if span is None:
        return -1
    value = timestamp_to_us(span.started_at)
    return -1 if value is None else value


def _candidate_sort_key(
    candidate: BehavioralCandidate,
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
) -> tuple[Any, ...]:
    return (
        candidate.coordinate.sort_key(),
        _KIND_ORDER[candidate.kind],
        candidate.field_path or "",
        _ref_timestamp(candidate.left, left_spans),
        _ref_key(candidate.left),
        _ref_timestamp(candidate.right, right_spans),
        _ref_key(candidate.right),
        _REASON_ORDER.get(candidate.reason_code, 999),
    )


def _barrier_sort_key(barrier: UncertaintyBarrier) -> tuple[Any, ...]:
    return (
        (0,) if barrier.coordinate is None else barrier.coordinate.sort_key(),
        barrier.reason_code,
        barrier.side,
    )


def _path_tuple(value: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str, str, int], ...]:
    return tuple(StructuralSegment.from_mapping(item).sort_key() for item in value)


def _same_signature_as_unavailable(
    span: Span,
    side: str,
    missing_parent_signatures: Mapping[str, set[tuple[str, str, str]]],
) -> bool:
    opposite = "right" if side == "left" else "left"
    return _signature(span) in missing_parent_signatures[opposite]


def _evidence_for_presence(
    alignment: str,
    span: Span,
) -> dict[str, Any]:
    return {
        "kind": "side_presence",
        "state": alignment,
        "signature": {
            "type": span.type,
            "operation": span.operation,
            "name": span.name,
        },
    }


def _member_fingerprint(span: Span) -> str:
    """Return group-local observed data without execution-local coordinates."""

    projection = span.to_dict()
    for key in (
        "trace_id",
        "span_id",
        "parent_span_id",
        "source",
        "started_at",
        "ended_at",
        "latency_ms",
        "metadata",
        "attributes",
    ):
        projection.pop(key, None)
    return _canonical_json_dumps(projection)


def _group_member_variation(
    group: Mapping[str, Any],
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
) -> MemberVariation:
    """Classify repeated-member variation without treating time as behavior.

    v0.2 presents repeated members in deterministic timestamp order.  The
    sequence is therefore useful for exposing an order-only ambiguity, but it
    must not make a later behavioral candidate depend on wall-clock values.
    Member observations are never paired or promoted to identity.
    """

    left_members = group.get("ambiguous_members", {}).get("left", ())
    right_members = group.get("ambiguous_members", {}).get("right", ())
    left_fingerprints = [
        _member_fingerprint(left_spans[str(reference.get("span_id", ""))])
        for reference in left_members
        if isinstance(reference, Mapping) and str(reference.get("span_id", "")) in left_spans
    ]
    right_fingerprints = [
        _member_fingerprint(right_spans[str(reference.get("span_id", ""))])
        for reference in right_members
        if isinstance(reference, Mapping) and str(reference.get("span_id", "")) in right_spans
    ]
    if left_fingerprints == right_fingerprints:
        return "same"
    if sorted(left_fingerprints) == sorted(right_fingerprints):
        return "order_only"
    return "observations_changed"


def _content_state(span: Span, field: str) -> tuple[str, bool]:
    info = span.capture.input if field == "input" else span.capture.output
    return info.state, bool(info.redacted)


def _content_candidates_and_barriers(
    item: Mapping[str, Any],
    left_span: Span,
    right_span: Span,
) -> tuple[list[BehavioralCandidate], list[UncertaintyBarrier]]:
    candidates: list[BehavioralCandidate] = []
    barriers: list[UncertaintyBarrier] = []
    coordinate = StructuralCoordinate.span(item["semantic_path"])
    left_ref = item.get("left")
    right_ref = item.get("right")
    for field in ("input", "output"):
        left_state, left_redacted = _content_state(left_span, field)
        right_state, right_redacted = _content_state(right_span, field)
        relevant_differences = [
            difference
            for difference in item.get("differences", ())
            if str(difference.get("path", "")) == f"/{field}"
            or str(difference.get("path", "")).startswith(f"/{field}/")
        ]
        relevant_uncertainties = [
            uncertainty
            for uncertainty in item.get("uncertainties", ())
            if str(uncertainty.get("path", "")) == f"/{field}"
            or str(uncertainty.get("path", "")).startswith(f"/{field}/")
        ]
        if left_redacted or right_redacted:
            # Equal sanitized placeholders do not establish equality of the
            # original values.  Keep the observation localized and unknown.
            barriers.append(
                UncertaintyBarrier(
                    "redacted_observation",
                    "both" if left_redacted and right_redacted else "left" if left_redacted else "right",
                    coordinate,
                    True,
                    ({"kind": "field_observation", "path": f"/{field}", "state": "unknown"},),
                )
            )
            continue
        if left_state == "captured" and right_state == "captured":
            if not relevant_differences:
                continue
            if any(difference.get("reason") == "redacted_observation" for difference in relevant_differences):
                barriers.append(
                    UncertaintyBarrier(
                        "redacted_observation",
                        "both",
                        coordinate,
                        True,
                        ({"kind": "field_observation", "path": f"/{field}", "state": "unknown"},),
                    )
                )
                continue
            difference = relevant_differences[0]
            candidates.append(
                BehavioralCandidate(
                    "aligned_tool_input_changed"
                    if field == "input"
                    else "aligned_tool_output_changed",
                    coordinate,
                    left_ref,
                    right_ref,
                    f"/{field}",
                    "captured_values_differ",
                    (
                        {
                            "kind": "field_observation",
                            "path": str(difference.get("path", f"/{field}")),
                            "left": difference.get("left"),
                            "right": difference.get("right"),
                            "state": "different",
                        },
                    ),
                )
            )
        elif left_state == "captured" or right_state == "captured":
            if relevant_uncertainties or relevant_differences:
                side: Literal["left", "right", "both"] = (
                    "left" if left_state == "captured" else "right"
                )
                barriers.append(
                    UncertaintyBarrier(
                        "capture_unavailable",
                        side,
                        coordinate,
                        True,
                        ({"kind": "field_observation", "path": f"/{field}", "state": "unknown"},),
                    )
                )
    return candidates, barriers


def _error_candidate(
    item: Mapping[str, Any],
    left_span: Span,
    right_span: Span,
) -> BehavioralCandidate | None:
    left_error = left_span.status == "error" and left_span.error is not None
    right_error = right_span.status == "error" and right_span.error is not None
    if left_error == right_error:
        return None
    reason = "error_observed" if right_error else "error_resolved"
    return BehavioralCandidate(
        "aligned_span_error_changed",
        StructuralCoordinate.span(item["semantic_path"]),
        item.get("left"),
        item.get("right"),
        "/status",
        reason,
        (
            {
                "kind": "status_observation",
                "path": "/status",
                "left": left_span.status,
                "right": right_span.status,
                "state": "different",
            },
        ),
    )


def _one_sided_candidates(
    comparison: Mapping[str, Any],
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
    *,
    complete: bool,
) -> list[BehavioralCandidate]:
    if not complete:
        return []
    records = [
        item
        for item in comparison.get("spans", ())
        if item.get("alignment") in {"left_only", "right_only"}
    ]
    one_sided_paths: dict[str, set[tuple[tuple[str, str, str, int], ...]]] = {
        "left": set(),
        "right": set(),
    }
    for item in records:
        side = "left" if item["alignment"] == "left_only" else "right"
        one_sided_paths[side].add(_path_tuple(item.get("semantic_path", ())))
    missing_parent_signatures: dict[str, set[tuple[str, str, str]]] = {
        "left": set(),
        "right": set(),
    }
    spans_by_side = {"left": left_spans, "right": right_spans}
    for item in comparison.get("unavailable_spans", ()):
        if item.get("reason") != "missing_parent":
            continue
        side = item.get("side")
        reference = item.get("span")
        if side not in spans_by_side or not isinstance(reference, Mapping):
            continue
        span = spans_by_side[side].get(str(reference.get("span_id", "")))
        if span is not None:
            missing_parent_signatures[side].add(_signature(span))
    candidates: list[BehavioralCandidate] = []
    for item in records:
        side = "left" if item["alignment"] == "left_only" else "right"
        reference = item.get("left") if side == "left" else item.get("right")
        if not isinstance(reference, Mapping):
            continue
        spans = left_spans if side == "left" else right_spans
        span = spans.get(str(reference.get("span_id", "")))
        if span is None:
            continue
        path = item.get("semantic_path", ())
        path_key = _path_tuple(path)
        if any(path_key[:index] in one_sided_paths[side] for index in range(1, len(path_key))):
            continue
        if _same_signature_as_unavailable(span, side, missing_parent_signatures):
            continue
        kind = (
            "unique_tool_added"
            if side == "right" and span.type == "tool"
            else "unique_tool_removed"
            if side == "left" and span.type == "tool"
            else "execution_subtree_added"
            if side == "right"
            else "execution_subtree_removed"
        )
        candidates.append(
            BehavioralCandidate(
                kind,
                StructuralCoordinate.span(path),
                item.get("left"),
                item.get("right"),
                None,
                "unique_tool_presence" if span.type == "tool" else "structural_subtree_presence_changed",
                (_evidence_for_presence(item["alignment"], span),),
            )
        )
    return candidates


def _group_candidates_and_barriers(
    comparison: Mapping[str, Any],
    *,
    complete: bool,
    globally_invalid: bool,
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
) -> tuple[list[BehavioralCandidate], list[UncertaintyBarrier]]:
    candidates: list[BehavioralCandidate] = []
    barriers: list[UncertaintyBarrier] = []
    for group in comparison.get("ambiguous_groups", ()):
        signature = group.get("group_signature", {})
        coordinate = StructuralCoordinate.sibling_group(group.get("parent_path", ()), signature)
        left_count = int(group.get("left_count", 0))
        right_count = int(group.get("right_count", 0))
        is_tool = signature.get("type") == "tool"
        if is_tool and left_count != right_count and complete and not globally_invalid:
            candidates.append(
                BehavioralCandidate(
                    "repeated_tool_group_cardinality_changed",
                    coordinate,
                    None,
                    None,
                    None,
                    "deterministic_group_count_changed",
                    (
                        {
                            "kind": "group_count",
                            "path": None,
                            "left": left_count,
                            "right": right_count,
                            "state": "different",
                        },
                    ),
                )
            )
            continue
        member_variation = _group_member_variation(
            group,
            left_spans,
            right_spans,
        )
        if is_tool and left_count == right_count and member_variation == "same":
            continue
        barriers.append(
            UncertaintyBarrier(
                "repeated_sibling_ambiguity",
                "both",
                coordinate,
                member_variation != "order_only",
                (
                    {
                        "kind": "structural_limitation",
                        "path": None,
                        "left_count": left_count,
                        "right_count": right_count,
                        "state": "unknown",
                    },
                ),
            )
        )
    return candidates, barriers


def _structural_barriers(
    comparison: Mapping[str, Any],
    left: TraceQueryRecord,
    right: TraceQueryRecord,
) -> tuple[list[UncertaintyBarrier], bool]:
    barriers: list[UncertaintyBarrier] = []
    globally_invalid = False
    for item in comparison.get("unavailable_spans", ()):
        reason = str(item.get("reason", "invalid_structure"))
        if reason == "ambiguous_parent":
            # Descendants of a repeated group are intentionally unavailable,
            # but the group-level ambiguity is the only barrier coordinate.
            continue
        side_value = item.get("side")
        side: Literal["left", "right", "both"] = side_value if side_value in {"left", "right"} else "both"
        reference = item.get("span")
        barriers.append(
            UncertaintyBarrier(
                reason,
                side,
                None,
                reason in _GLOBAL_BARRIERS,
                (
                    {
                        "kind": "structural_limitation",
                        "path": None,
                        "span": reference,
                        "state": "unknown",
                    },
                ),
            )
        )
        globally_invalid = globally_invalid or reason in _GLOBAL_BARRIERS
    if not _trace_complete(left) or not _trace_complete(right):
        side: Literal["left", "right", "both"] = (
            "both"
            if not _trace_complete(left) and not _trace_complete(right)
            else "left"
            if not _trace_complete(left)
            else "right"
        )
        barriers.append(
            UncertaintyBarrier(
                "incomplete_trace",
                side,
                StructuralCoordinate.trace_summary(),
                True,
                ({"kind": "structural_limitation", "path": None, "state": "unknown"},),
            )
        )
    return barriers, globally_invalid


def _barrier_precedes(
    barrier: UncertaintyBarrier,
    candidate: BehavioralCandidate,
) -> bool:
    if not barrier.blocks_earlier_claim:
        return False
    if barrier.coordinate is None:
        return True
    return barrier.coordinate.sort_key() <= candidate.coordinate.sort_key()


def _last_match(
    comparison: Mapping[str, Any],
    candidate: BehavioralCandidate | None,
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
    barriers: Sequence[UncertaintyBarrier],
) -> ReliablyMatchedPoint:
    matches = [
        item
        for item in comparison.get("spans", ())
        if item.get("alignment") == "exact_match"
    ]
    if candidate is None:
        return ReliablyMatchedPoint(
            "none",
            (),
            None,
            None,
            "before_uncertainty_barrier" if barriers else "no_prior_resolved_point",
        )
    earlier = [
        item
        for item in matches
        if StructuralCoordinate.span(item.get("semantic_path", ())).sort_key()
        < candidate.coordinate.sort_key()
    ]
    if not earlier:
        return ReliablyMatchedPoint("none", (), None, None, "no_prior_resolved_point")
    item = max(
        earlier,
        key=lambda value: StructuralCoordinate.span(value["semantic_path"]).sort_key(),
    )
    return ReliablyMatchedPoint(
        "matched",
        tuple(StructuralSegment.from_mapping(value) for value in item["semantic_path"]),
        item.get("left"),
        item.get("right"),
        "before_uncertainty_barrier"
        if any(_barrier_precedes(barrier, candidate) for barrier in barriers)
        else "before_first_finding",
    )


def analyze_divergence(
    left: TraceQueryRecord,
    left_spans: Sequence[Span],
    right: TraceQueryRecord,
    right_spans: Sequence[Span],
) -> DivergenceResult:
    """Analyze two persisted Canonical read models without mutating storage."""

    comparison = compare_trace_inputs(left, left_spans, right, right_spans)
    left_by_id = _span_map(left_spans)
    right_by_id = _span_map(right_spans)
    barriers, globally_invalid = _structural_barriers(comparison, left, right)
    complete = _trace_complete(left) and _trace_complete(right)

    candidates: list[BehavioralCandidate] = []
    group_candidates, group_barriers = _group_candidates_and_barriers(
        comparison,
        complete=complete,
        globally_invalid=globally_invalid,
        left_spans=left_by_id,
        right_spans=right_by_id,
    )
    candidates.extend(group_candidates)
    barriers.extend(group_barriers)
    candidates.extend(
        _one_sided_candidates(
            comparison,
            left_by_id,
            right_by_id,
            complete=complete,
        )
    )

    for item in comparison.get("spans", ()):
        if item.get("alignment") != "exact_match":
            continue
        left_ref = item.get("left")
        right_ref = item.get("right")
        if not isinstance(left_ref, Mapping) or not isinstance(right_ref, Mapping):
            continue
        left_span = left_by_id.get(str(left_ref.get("span_id", "")))
        right_span = right_by_id.get(str(right_ref.get("span_id", "")))
        if left_span is None or right_span is None:
            continue
        error = _error_candidate(item, left_span, right_span)
        if error is not None:
            candidates.append(error)
        if left_span.type == "tool" and right_span.type == "tool":
            content_candidates, content_barriers = _content_candidates_and_barriers(
                item,
                left_span,
                right_span,
            )
            candidates.extend(content_candidates)
            barriers.extend(content_barriers)

    candidates.sort(key=lambda item: _candidate_sort_key(item, left_by_id, right_by_id))
    barriers.sort(key=_barrier_sort_key)

    if globally_invalid:
        candidate = None
    elif not candidates:
        candidate = None
    else:
        candidate = candidates[0]
        if any(_barrier_precedes(barrier, candidate) for barrier in barriers):
            candidate = None

    if candidate is not None:
        state: DivergenceState = "supported"
    elif barriers:
        state = "uncertain"
    else:
        state = "none"
    return DivergenceResult(
        state,
        candidate,
        tuple(barriers),
        _last_match(comparison, candidate, left_by_id, right_by_id, barriers),
    )


__all__ = [
    "BEHAVIORAL_KINDS",
    "BehavioralCandidate",
    "DivergenceResult",
    "DivergenceState",
    "ReliablyMatchedPoint",
    "StructuralCoordinate",
    "StructuralSegment",
    "UncertaintyBarrier",
    "analyze_divergence",
]
