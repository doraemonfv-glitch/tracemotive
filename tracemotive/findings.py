"""Deterministic internal V03-20 diagnostic findings.

This module turns the existing v0.2 comparison read model into structured
observations for later summary layers.  It does not select a first divergence,
recommend an investigation point, or infer causality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from tracemotive.canonical.models import Span, _canonical_json_dumps
from tracemotive.comparison import compare_trace_inputs
from tracemotive.divergence import (
    StructuralCoordinate,
    StructuralSegment,
    UncertaintyBarrier,
    _content_candidates_and_barriers,
    _group_candidates_and_barriers,
    _structural_barriers,
)
from tracemotive.storage.repository import TraceQueryRecord


FindingScope = Literal["behavioral", "context_only"]
ObservationState = Literal["confirmed_observation", "observation_limited"]

_BEHAVIORAL_TYPES = frozenset(
    {
        "new_error",
        "resolved_error",
        "tool_input_changed",
        "tool_output_changed",
        "tool_added",
        "tool_removed",
        "execution_subtree_added",
        "execution_subtree_removed",
        "tool_repetition_changed",
    }
)
_CONTEXT_TYPES = frozenset(
    {"model_changed", "request_parameters_changed", "trace_status_changed"}
)
_REASON_CODES = frozenset(
    {
        "error_observed",
        "error_resolved",
        "captured_values_differ",
        "unique_tool_presence",
        "deterministic_group_count_changed",
        "structural_subtree_presence_changed",
        "known_model_changed",
        "known_request_parameters_changed",
        "observed_trace_status_changed",
    }
)
_FINDING_ORDER = {
    "new_error": 0,
    "resolved_error": 1,
    "tool_output_changed": 2,
    "tool_input_changed": 3,
    "tool_added": 4,
    "tool_removed": 5,
    "tool_repetition_changed": 6,
    "execution_subtree_added": 7,
    "execution_subtree_removed": 8,
    "model_changed": 9,
    "request_parameters_changed": 10,
    "trace_status_changed": 11,
}
_GLOBAL_BARRIERS = frozenset({"cycle", "invalid_structure"})


class FindingConsistencyError(RuntimeError):
    """Raised by test-facing checks when findings contradict V03-11 evidence."""


@dataclass(frozen=True, slots=True)
class DiagnosticFinding:
    """One structured, sanitized V03-20 observation difference.

    ``finding_id`` is a response-local display/reference identifier assigned
    after deterministic sorting.  It is not a durable identity across later
    comparisons or database updates.
    """

    finding_id: str
    type: str
    coordinate: StructuralCoordinate
    left: dict[str, str] | None
    right: dict[str, str] | None
    field_path: str | None
    scope: FindingScope
    observation_state: ObservationState
    reason_code: str
    observed: dict[str, Any]
    evidence: tuple[dict[str, Any], ...] = ()
    relationships: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.type not in _BEHAVIORAL_TYPES | _CONTEXT_TYPES:
            raise ValueError(f"unsupported finding type: {self.type}")
        expected_scope = "context_only" if self.type in _CONTEXT_TYPES else "behavioral"
        if self.scope != expected_scope:
            raise ValueError(f"finding scope does not match type: {self.type}")
        if self.observation_state not in {"confirmed_observation", "observation_limited"}:
            raise ValueError(f"unsupported observation state: {self.observation_state}")
        if self.reason_code not in _REASON_CODES:
            raise ValueError(f"unsupported finding reason code: {self.reason_code}")
        if any(
            relationship.get("relation")
            not in {
                "same_structural_region",
                "descendant_evidence",
                "observed_after",
                "terminal_summary",
                "blocked_by_uncertainty",
            }
            for relationship in self.relationships
        ):
            raise ValueError("unsupported finding relationship")

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "type": self.type,
            "coordinate": self.coordinate.to_dict(),
            "left": self.left,
            "right": self.right,
            "field_path": self.field_path,
            "scope": self.scope,
            "observation_state": self.observation_state,
            "reason_code": self.reason_code,
            "observed": self.observed,
            "evidence": list(self.evidence),
            "relationships": list(self.relationships),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticFindings:
    """V03-20 findings and localized V03-11 uncertainty records."""

    findings: tuple[DiagnosticFinding, ...]
    uncertainties: tuple[UncertaintyBarrier, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "uncertainties": [
                {
                    "uncertainty_id": f"uncertainty-{index:04d}",
                    **barrier.to_dict(),
                }
                for index, barrier in enumerate(self.uncertainties, start=1)
            ],
        }

    def to_json(self) -> str:
        return _canonical_json_dumps(self.to_dict())


def _copy_ref(value: Mapping[str, str] | None) -> dict[str, str] | None:
    return None if value is None else {"trace_id": str(value["trace_id"]), "span_id": str(value["span_id"])}


def _signature(span: Span) -> tuple[str, str, str]:
    return span.type, span.operation, span.name


def _trace_complete(record: TraceQueryRecord) -> bool:
    return record.trace.ended_at is not None and record.trace.status != "unset"


def _span_map(spans: Sequence[Span]) -> dict[str, Span]:
    return {span.span_id: span for span in spans}


def _path_key(value: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str, str, int], ...]:
    return tuple(
        StructuralSegment.from_mapping(item).sort_key()
        for item in value
    )


def _coordinate(item: Mapping[str, Any]) -> StructuralCoordinate:
    return StructuralCoordinate.span(item.get("semantic_path", ()))


def _error_observed(span: Span) -> bool:
    return span.status == "error" and span.error is not None


def _error_value(span: Span) -> dict[str, Any]:
    return {
        "status": span.status,
        "error": None if span.error is None else span.error.to_dict(),
    }


def _field_difference(
    item: Mapping[str, Any],
    field: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    prefix = f"/{field}"
    differences = [
        difference
        for difference in item.get("differences", ())
        if str(difference.get("path", "")) == prefix
        or str(difference.get("path", "")).startswith(f"{prefix}/")
    ]
    uncertainties = [
        uncertainty
        for uncertainty in item.get("uncertainties", ())
        if str(uncertainty.get("path", "")) == prefix
        or str(uncertainty.get("path", "")).startswith(f"{prefix}/")
    ]
    return (differences[0] if differences else None), uncertainties


def _content_finding(
    item: Mapping[str, Any],
    left_span: Span,
    right_span: Span,
    field: Literal["input", "output"],
) -> DiagnosticFinding | None:
    left_info = left_span.capture.input if field == "input" else left_span.capture.output
    right_info = right_span.capture.input if field == "input" else right_span.capture.output
    difference, uncertainties = _field_difference(item, field)
    if (
        difference is None
        or uncertainties
        or left_info.state != "captured"
        or right_info.state != "captured"
        or left_info.redacted
        or right_info.redacted
    ):
        return None
    left_value = left_span.input if field == "input" else left_span.output
    right_value = right_span.input if field == "input" else right_span.output
    return DiagnosticFinding(
        "",
        "tool_input_changed" if field == "input" else "tool_output_changed",
        _coordinate(item),
        _copy_ref(item.get("left")),
        _copy_ref(item.get("right")),
        f"/{field}",
        "behavioral",
        "confirmed_observation",
        "captured_values_differ",
        {
            "left": {"state": "captured", "value": left_value},
            "right": {"state": "captured", "value": right_value},
        },
        (
            {
                "kind": "field_observation",
                "path": str(difference.get("path", f"/{field}")),
                "left": difference.get("left"),
                "right": difference.get("right"),
                "state": "different",
                "reason": None,
            },
        ),
    )


def _error_finding(
    item: Mapping[str, Any],
    left_span: Span,
    right_span: Span,
) -> DiagnosticFinding | None:
    left_error = _error_observed(left_span)
    right_error = _error_observed(right_span)
    if left_error and right_span.status == "ok" and right_span.error is None:
        type_name = "resolved_error"
        reason = "error_resolved"
    elif right_error and left_span.status == "ok" and left_span.error is None:
        type_name = "new_error"
        reason = "error_observed"
    else:
        return None
    evidence: list[dict[str, Any]] = [
        {
            "kind": "status_observation",
            "path": "/status",
            "left": left_span.status,
            "right": right_span.status,
            "state": "different",
            "reason": None,
        }
    ]
    if left_span.error is not None or right_span.error is not None:
        evidence.extend(
            {
                "kind": "error_observation",
                "path": path,
                "left": _error_field(left_span, path),
                "right": _error_field(right_span, path),
                "state": "different",
                "reason": None,
            }
            for path in ("/error/type", "/error/message")
            if _error_field(left_span, path) is not None or _error_field(right_span, path) is not None
        )
    return DiagnosticFinding(
        "",
        type_name,
        _coordinate(item),
        _copy_ref(item.get("left")),
        _copy_ref(item.get("right")),
        "/status",
        "behavioral",
        "confirmed_observation",
        reason,
        {
            "left": {"state": "observed", "value": _error_value(left_span)},
            "right": {"state": "observed", "value": _error_value(right_span)},
        },
        tuple(evidence),
    )


def _error_field(span: Span, path: str) -> str | None:
    if span.error is None:
        return None
    return span.error.type if path == "/error/type" else span.error.message


def _missing_parent_signatures(
    comparison: Mapping[str, Any],
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
) -> dict[str, set[tuple[str, str, str]]]:
    result: dict[str, set[tuple[str, str, str]]] = {"left": set(), "right": set()}
    spans_by_side = {"left": left_spans, "right": right_spans}
    for item in comparison.get("unavailable_spans", ()):
        side = item.get("side")
        reference = item.get("span")
        if item.get("reason") != "missing_parent" or side not in spans_by_side or not isinstance(reference, Mapping):
            continue
        span = spans_by_side[side].get(str(reference.get("span_id", "")))
        if span is not None:
            result[side].add(_signature(span))
    return result


def _one_sided_roots(
    comparison: Mapping[str, Any],
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
) -> list[tuple[Mapping[str, Any], Span, str]]:
    records = [
        item
        for item in comparison.get("spans", ())
        if item.get("alignment") in {"left_only", "right_only"}
    ]
    paths_by_side: dict[str, set[tuple[tuple[str, str, str, int], ...]]] = {"left": set(), "right": set()}
    for item in records:
        side = "left" if item.get("alignment") == "left_only" else "right"
        paths_by_side[side].add(_path_key(item.get("semantic_path", ())))
    missing = _missing_parent_signatures(comparison, left_spans, right_spans)
    result: list[tuple[Mapping[str, Any], Span, str]] = []
    for item in records:
        side = "left" if item.get("alignment") == "left_only" else "right"
        path = _path_key(item.get("semantic_path", ()))
        if any(path[:index] in paths_by_side[side] for index in range(1, len(path))):
            continue
        reference = item.get("left") if side == "left" else item.get("right")
        if not isinstance(reference, Mapping):
            continue
        spans = left_spans if side == "left" else right_spans
        span = spans.get(str(reference.get("span_id", "")))
        if span is None or _signature(span) in missing["right" if side == "left" else "left"]:
            continue
        result.append((item, span, side))
    return result


def _presence_finding(
    item: Mapping[str, Any],
    span: Span,
    side: str,
) -> DiagnosticFinding:
    is_right = side == "right"
    finding_type = (
        "tool_added"
        if span.type == "tool" and is_right
        else "tool_removed"
        if span.type == "tool"
        else "execution_subtree_added"
        if is_right
        else "execution_subtree_removed"
    )
    reason = "unique_tool_presence" if span.type == "tool" else "structural_subtree_presence_changed"
    signature = {"type": span.type, "operation": span.operation, "name": span.name}
    absent = {"state": "absent", "value": None}
    present = {"state": "present", "value": signature}
    presence_state = "right_only" if is_right else "left_only"
    return DiagnosticFinding(
        "",
        finding_type,
        _coordinate(item),
        _copy_ref(item.get("left")),
        _copy_ref(item.get("right")),
        None,
        "behavioral",
        "confirmed_observation",
        reason,
        {"left": present if not is_right else absent, "right": present if is_right else absent},
        (
            {
                "kind": "side_presence",
                "state": presence_state,
                "signature": signature,
            },
        ),
    )


def _unique_error_finding(
    item: Mapping[str, Any],
    span: Span,
    side: str,
) -> DiagnosticFinding | None:
    if side != "right" or not _error_observed(span):
        return None
    signature = {"type": span.type, "operation": span.operation, "name": span.name}
    evidence: list[dict[str, Any]] = [
        {
            "kind": "status_observation",
            "path": "/status",
            "left": None,
            "right": span.status,
            "state": "different",
            "reason": None,
        }
    ]
    evidence.extend(
        {
            "kind": "error_observation",
            "path": path,
            "left": None,
            "right": _error_field(span, path),
            "state": "right_only",
            "reason": None,
        }
        for path in ("/error/type", "/error/message")
        if _error_field(span, path) is not None
    )
    return DiagnosticFinding(
        "",
        "new_error",
        _coordinate(item),
        None,
        _copy_ref(item.get("right")),
        "/status",
        "behavioral",
        "confirmed_observation",
        "error_observed",
        {
            "left": {"state": "absent", "value": None},
            "right": {"state": "observed", "value": _error_value(span)},
        },
        tuple(evidence),
    )


def _group_findings(
    comparison: Mapping[str, Any],
    *,
    complete: bool,
    globally_invalid: bool,
    side_only_root_paths: set[tuple[tuple[str, str, str, int], ...]],
) -> list[DiagnosticFinding]:
    if not complete or globally_invalid:
        return []
    findings: list[DiagnosticFinding] = []
    for group in comparison.get("ambiguous_groups", ()):
        signature = group.get("group_signature", {})
        if signature.get("type") != "tool":
            continue
        parent_path = _path_key(group.get("parent_path", ()))
        if any(
            parent_path[:index] in side_only_root_paths
            for index in range(1, len(parent_path) + 1)
        ):
            continue
        left_count = int(group.get("left_count", 0))
        right_count = int(group.get("right_count", 0))
        if left_count == right_count:
            continue
        coordinate = StructuralCoordinate.sibling_group(group.get("parent_path", ()), signature)
        findings.append(
            DiagnosticFinding(
                "",
                "tool_repetition_changed",
                coordinate,
                None,
                None,
                None,
                "behavioral",
                "confirmed_observation",
                "deterministic_group_count_changed",
                {
                    "left": {"state": "known", "value": left_count},
                    "right": {"state": "known", "value": right_count},
                },
                (
                    {
                        "kind": "group_count",
                        "path": None,
                        "left": left_count,
                        "right": right_count,
                        "state": "different",
                        "reason": None,
                    },
                ),
            )
        )
    return findings


def _context_findings(
    comparison: Mapping[str, Any],
    left: TraceQueryRecord,
    right: TraceQueryRecord,
    left_spans: Mapping[str, Span],
    right_spans: Mapping[str, Span],
) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []
    for item in comparison.get("spans", ()):
        if item.get("alignment") != "exact_match":
            continue
        left_ref = item.get("left")
        right_ref = item.get("right")
        if not isinstance(left_ref, Mapping) or not isinstance(right_ref, Mapping):
            continue
        left_span = left_spans.get(str(left_ref.get("span_id", "")))
        right_span = right_spans.get(str(right_ref.get("span_id", "")))
        if left_span is None or right_span is None or left_span.type != "llm" or right_span.type != "llm":
            continue
        if not hasattr(left_span.details, "request_model") or not hasattr(right_span.details, "request_model"):
            continue
        left_model = left_span.details.request_model
        right_model = right_span.details.request_model
        if left_model is not None and right_model is not None and left_model != right_model:
            findings.append(
                DiagnosticFinding(
                    "",
                    "model_changed",
                    _coordinate(item),
                    _copy_ref(left_ref),
                    _copy_ref(right_ref),
                    "/details/request_model",
                    "context_only",
                    "confirmed_observation",
                    "known_model_changed",
                    {
                        "left": {"state": "known", "value": left_model},
                        "right": {"state": "known", "value": right_model},
                    },
                    (
                        {
                            "kind": "field_observation",
                            "path": "/details/request_model",
                            "left": left_model,
                            "right": right_model,
                            "state": "different",
                            "reason": None,
                        },
                    ),
                )
            )
        left_parameters = getattr(left_span.details, "request_parameters", None)
        right_parameters = getattr(right_span.details, "request_parameters", None)
        parameter_difference, _ = _field_difference(item, "details/request_parameters")
        if (
            left_parameters is not None
            and right_parameters is not None
            and parameter_difference is not None
        ):
            findings.append(
                DiagnosticFinding(
                    "",
                    "request_parameters_changed",
                    _coordinate(item),
                    _copy_ref(left_ref),
                    _copy_ref(right_ref),
                    "/details/request_parameters",
                    "context_only",
                    "confirmed_observation",
                    "known_request_parameters_changed",
                    {
                        "left": {"state": "known", "value": left_parameters},
                        "right": {"state": "known", "value": right_parameters},
                    },
                    (
                        {
                            "kind": "field_observation",
                            "path": str(parameter_difference.get("path", "/details/request_parameters")),
                            "left": parameter_difference.get("left"),
                            "right": parameter_difference.get("right"),
                            "state": "different",
                            "reason": None,
                        },
                    ),
                )
            )
    left_status = left.trace.status
    right_status = right.trace.status
    if (
        left_status in {"ok", "error"}
        and right_status in {"ok", "error"}
        and left_status != right_status
    ):
        findings.append(
            DiagnosticFinding(
                "",
                "trace_status_changed",
                StructuralCoordinate.trace_summary(),
                None,
                None,
                "/status",
                "context_only",
                "confirmed_observation",
                "observed_trace_status_changed",
                {
                    "left": {"state": "known", "value": left_status},
                    "right": {"state": "known", "value": right_status},
                },
                (
                    {
                        "kind": "field_observation",
                        "path": "/status",
                        "left": left_status,
                        "right": right_status,
                        "state": "different",
                        "reason": None,
                    },
                ),
            )
        )
    return findings


def _finding_sort_key(finding: DiagnosticFinding) -> tuple[Any, ...]:
    return (
        finding.coordinate.sort_key(),
        _FINDING_ORDER[finding.type],
        finding.field_path or "",
        finding.reason_code,
    )


def _barrier_sort_key(barrier: UncertaintyBarrier) -> tuple[Any, ...]:
    return (
        (0,) if barrier.coordinate is None else barrier.coordinate.sort_key(),
        barrier.reason_code,
        barrier.side,
        0 if barrier.blocks_earlier_claim else 1,
        _canonical_json_dumps(list(barrier.evidence)),
    )


def _v03_11_barriers(
    comparison: Mapping[str, Any],
    left: TraceQueryRecord,
    left_spans: Mapping[str, Span],
    right: TraceQueryRecord,
    right_spans: Mapping[str, Span],
    *,
    complete: bool,
) -> tuple[UncertaintyBarrier, ...]:
    """Reuse V03-11 barrier primitives without rebuilding v0.2 comparison."""

    barriers, globally_invalid = _structural_barriers(comparison, left, right)
    _, group_barriers = _group_candidates_and_barriers(
        comparison,
        complete=complete,
        globally_invalid=globally_invalid,
        left_spans=left_spans,
        right_spans=right_spans,
    )
    barriers.extend(group_barriers)
    for item in comparison.get("spans", ()):
        if item.get("alignment") != "exact_match":
            continue
        left_ref = item.get("left")
        right_ref = item.get("right")
        if not isinstance(left_ref, Mapping) or not isinstance(right_ref, Mapping):
            continue
        left_span = left_spans.get(str(left_ref.get("span_id", "")))
        right_span = right_spans.get(str(right_ref.get("span_id", "")))
        if left_span is None or right_span is None:
            continue
        if left_span.type == "tool" and right_span.type == "tool":
            _, content_barriers = _content_candidates_and_barriers(
                item,
                left_span,
                right_span,
            )
            barriers.extend(content_barriers)
    barriers.sort(key=_barrier_sort_key)
    return tuple(barriers)


def collect_findings(
    left: TraceQueryRecord,
    left_spans: Sequence[Span],
    right: TraceQueryRecord,
    right_spans: Sequence[Span],
    *,
    comparison: Mapping[str, Any] | None = None,
) -> DiagnosticFindings:
    """Collect deterministic V03-20 findings from one read-model comparison.

    ``comparison`` is an internal composition hook for the v0.3 HTTP read
    model.  It accepts one already-derived v0.2 comparison without changing
    finding selection, IDs, or public V03-20 semantics.
    """

    if comparison is None:
        comparison = compare_trace_inputs(left, left_spans, right, right_spans)
    left_by_id = _span_map(left_spans)
    right_by_id = _span_map(right_spans)
    complete = _trace_complete(left) and _trace_complete(right)
    globally_invalid = any(
        item.get("reason") in _GLOBAL_BARRIERS
        for item in comparison.get("unavailable_spans", ())
    )
    findings: list[DiagnosticFinding] = []
    side_only_roots: list[tuple[Mapping[str, Any], Span, str]] = []

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
        error = _error_finding(item, left_span, right_span)
        if error is not None:
            findings.append(error)
        if left_span.type == "tool" and right_span.type == "tool":
            for field in ("input", "output"):
                content = _content_finding(item, left_span, right_span, field)
                if content is not None:
                    findings.append(content)

    if complete and not globally_invalid:
        side_only_roots = _one_sided_roots(comparison, left_by_id, right_by_id)
        for item, span, side in side_only_roots:
            findings.append(_presence_finding(item, span, side))
            error = _unique_error_finding(item, span, side)
            if error is not None:
                findings.append(error)

    findings.extend(
        _group_findings(
            comparison,
            complete=complete,
            globally_invalid=globally_invalid,
            side_only_root_paths={
                _path_key(item.get("semantic_path", ()))
                for item, _, _ in side_only_roots
            },
        )
    )
    findings.extend(_context_findings(comparison, left, right, left_by_id, right_by_id))
    findings.sort(key=_finding_sort_key)
    assigned = tuple(
        DiagnosticFinding(
            f"finding-{index:04d}",
            finding.type,
            finding.coordinate,
            finding.left,
            finding.right,
            finding.field_path,
            finding.scope,
            finding.observation_state,
            finding.reason_code,
            finding.observed,
            finding.evidence,
            finding.relationships,
        )
        for index, finding in enumerate(findings, start=1)
    )
    barriers = _v03_11_barriers(
        comparison,
        left,
        left_by_id,
        right,
        right_by_id,
        complete=complete,
    )
    return DiagnosticFindings(assigned, barriers)


def assert_compatible_with_v03_11(
    divergence_state: str,
    findings: DiagnosticFindings,
) -> None:
    """Assert the V03-11/V03-20 behavioral state invariant in tests."""

    behavioral = [finding for finding in findings.findings if finding.scope == "behavioral"]
    if divergence_state == "none" and behavioral:
        raise FindingConsistencyError("V03-20 emitted behavioral evidence while V03-11 returned none")
    if divergence_state == "supported" and not behavioral:
        raise FindingConsistencyError("V03-20 omitted behavioral evidence for a supported V03-11 candidate")


__all__ = [
    "DiagnosticFinding",
    "DiagnosticFindings",
    "FindingConsistencyError",
    "collect_findings",
    "assert_compatible_with_v03_11",
]
