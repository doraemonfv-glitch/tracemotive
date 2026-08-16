"""Internal V03-30 composition for the additive insight-first HTTP read model.

This module consumes one persisted v0.2 comparison snapshot and the approved
V03-11, V03-20, and V03-21 layers.  It does not write to storage, export a
Python SDK surface, or alter the existing v0.2 response.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import quote

from tracemotive.canonical.models import Span, _canonical_json_dumps
from tracemotive.comparison import (
    MAX_COMPARISON_RESPONSE_BYTES,
    MAX_DIFFERENCE_RECORDS,
    ComparisonTooLargeError,
    compare_trace_inputs,
)
from tracemotive.divergence import StructuralCoordinate, UncertaintyBarrier, analyze_divergence
from tracemotive.findings import DiagnosticFinding, DiagnosticFindings, collect_findings
from tracemotive.investigation import InvestigationSummary, build_investigation_summary
from tracemotive.storage.repository import TraceQueryRecord


COMPARISON_VERSION = "0.3"

_API_INVESTIGATION_STATE = {
    "NO_BEHAVIORAL_DIVERGENCE": "none",
    "INVESTIGATION_POINT": "identified",
    "UNCERTAIN": "uncertain",
}


class V3CompositionError(RuntimeError):
    """Raised when authoritative v0.3 layers cannot be serialized safely."""


def _trace_identity(record: TraceQueryRecord) -> dict[str, str]:
    """Return the compact persisted identity needed for a comparison header."""

    return {
        "trace_id": record.trace.trace_id,
        "name": record.trace.name,
        "status": record.trace.status,
    }


def _coordinate_dict(coordinate: StructuralCoordinate) -> dict[str, Any]:
    return coordinate.to_dict()


def _finding_dict(finding: DiagnosticFinding) -> dict[str, Any]:
    """Serialize one complete §11 finding in the v3 read model."""

    return {
        "finding_id": finding.finding_id,
        "type": finding.type,
        "coordinate": _coordinate_dict(finding.coordinate),
        "left": finding.left,
        "right": finding.right,
        "field_path": finding.field_path,
        "scope": finding.scope,
        "observation_state": finding.observation_state,
        "reason_code": finding.reason_code,
        "observed": finding.observed,
        "evidence": list(finding.evidence),
        "relationships": list(finding.relationships),
    }


def _barrier_semantic_key(barrier: UncertaintyBarrier) -> tuple[Any, ...]:
    return (
        barrier.reason_code,
        barrier.side,
        barrier.coordinate,
        barrier.blocks_earlier_claim,
        _canonical_json_dumps(list(barrier.evidence)),
    )


def _barrier_sort_key(barrier: UncertaintyBarrier) -> tuple[Any, ...]:
    return (
        (0,) if barrier.coordinate is None else barrier.coordinate.sort_key(),
        barrier.reason_code,
        barrier.side,
        0 if barrier.blocks_earlier_claim else 1,
        _canonical_json_dumps(list(barrier.evidence)),
    )


def _uncertainty_dicts(findings: DiagnosticFindings) -> list[dict[str, Any]]:
    """Assign v3-local IDs with V03-21's exact duplicate semantics."""

    unique: dict[tuple[Any, ...], UncertaintyBarrier] = {}
    for barrier in findings.uncertainties:
        unique.setdefault(_barrier_semantic_key(barrier), barrier)
    return [
        {
            "uncertainty_id": f"uncertainty-{index:04d}",
            "coordinate": None if barrier.coordinate is None else _coordinate_dict(barrier.coordinate),
            "reason_code": barrier.reason_code,
            "side": barrier.side,
            "blocks_earlier_claim": barrier.blocks_earlier_claim,
            "evidence": list(barrier.evidence),
        }
        for index, barrier in enumerate(
            sorted(unique.values(), key=_barrier_sort_key),
            start=1,
        )
    ]


def _starting_point(summary: InvestigationSummary) -> dict[str, Any] | None:
    primary = summary.primary
    if primary is None:
        return None
    if len(primary.finding_ids) != 1:
        raise V3CompositionError("primary investigation point must reference one finding")
    coordinate = _coordinate_dict(primary.coordinate)
    return {
        "kind": coordinate["kind"],
        "semantic_path": coordinate["semantic_path"],
        "group_signature": coordinate["group_signature"],
        "left": primary.left,
        "right": primary.right,
        "finding_id": primary.finding_ids[0],
        "label": _starting_point_label(primary.divergence_kind),
    }


def _starting_point_label(divergence_kind: str) -> str:
    labels = {
        "aligned_span_error_changed": "Inspect observed span error change",
        "aligned_tool_input_changed": "Inspect observed tool input change",
        "aligned_tool_output_changed": "Inspect observed tool output change",
        "unique_tool_added": "Inspect observed tool addition",
        "unique_tool_removed": "Inspect observed tool removal",
        "execution_subtree_added": "Inspect observed execution subtree addition",
        "execution_subtree_removed": "Inspect observed execution subtree removal",
        "repeated_tool_group_cardinality_changed": "Inspect observed tool repetition change",
    }
    try:
        return labels[divergence_kind]
    except KeyError as exc:
        raise V3CompositionError("unsupported primary divergence kind") from exc


def _investigation_dict(summary: InvestigationSummary) -> dict[str, Any]:
    primary = summary.primary
    state = _API_INVESTIGATION_STATE[summary.state]
    return {
        "state": state,
        "ordering_basis": summary.ordering_basis,
        "starting_point": _starting_point(summary),
        "first_meaningful_divergence": {
            "state": state,
            "ordering_basis": summary.ordering_basis,
            "finding_id": None if primary is None else primary.finding_ids[0],
            "reason_code": None if primary is None else primary.reason_code,
        },
        "last_reliably_matched_point": summary.last_reliably_matched_point,
        "evidence_summary": [item.to_dict() for item in summary.evidence_summary],
        "context_finding_ids": list(summary.context_finding_ids),
        "blocking_uncertainty_ids": list(summary.blocking_uncertainty_ids),
        "limitations": [item.to_dict() for item in summary.uncertainties],
    }


def _validate_references(
    summary: InvestigationSummary,
    findings: DiagnosticFindings,
    uncertainties: Sequence[Mapping[str, Any]],
) -> None:
    finding_by_id = {finding.finding_id: finding for finding in findings.findings}
    if len(finding_by_id) != len(findings.findings):
        raise V3CompositionError("finding IDs must be unique")
    uncertainty_ids = {str(item["uncertainty_id"]) for item in uncertainties}
    if len(uncertainty_ids) != len(uncertainties):
        raise V3CompositionError("uncertainty IDs must be unique")

    if summary.state == "INVESTIGATION_POINT":
        if summary.primary is None:
            raise V3CompositionError("investigation state requires a primary finding")
        for finding_id in summary.primary.finding_ids:
            finding = finding_by_id.get(finding_id)
            if finding is None or finding.scope != "behavioral":
                raise V3CompositionError("primary reference is not behavioral evidence")
    elif summary.primary is not None:
        raise V3CompositionError("non-primary investigation state cannot expose a primary finding")

    for reference in summary.evidence_summary:
        finding = finding_by_id.get(reference.finding_id)
        if finding is None or finding.scope != "behavioral":
            raise V3CompositionError("behavioral evidence reference is invalid")
    for finding_id in summary.context_finding_ids:
        finding = finding_by_id.get(finding_id)
        if finding is None or finding.scope != "context_only":
            raise V3CompositionError("context reference is invalid")
    if any(finding_id not in uncertainty_ids for finding_id in summary.blocking_uncertainty_ids):
        raise V3CompositionError("blocking uncertainty reference is invalid")
    if any(item.uncertainty_id not in uncertainty_ids for item in summary.uncertainties):
        raise V3CompositionError("limitation reference is invalid")


def build_v3_comparison(
    left: TraceQueryRecord,
    left_spans: Sequence[Span],
    right: TraceQueryRecord,
    right_spans: Sequence[Span],
) -> dict[str, Any]:
    """Compose one deterministic, read-only v3 response from persisted inputs."""

    comparison = compare_trace_inputs(left, left_spans, right, right_spans)
    divergence = analyze_divergence(
        left,
        left_spans,
        right,
        right_spans,
        comparison=comparison,
    )
    findings = collect_findings(
        left,
        left_spans,
        right,
        right_spans,
        comparison=comparison,
    )
    investigation = build_investigation_summary(divergence, findings)
    finding_items = [_finding_dict(finding) for finding in findings.findings]
    uncertainty_items = _uncertainty_dicts(findings)
    if len(finding_items) + len(uncertainty_items) > MAX_DIFFERENCE_RECORDS:
        raise ComparisonTooLargeError("comparison record limit exceeded")
    _validate_references(investigation, findings, uncertainty_items)

    result = {
        "comparison_version": COMPARISON_VERSION,
        "left_trace": _trace_identity(left),
        "right_trace": _trace_identity(right),
        "summary": {
            "alignment": dict(comparison["summary"]["alignment"]),
            "finding_count": len(finding_items),
            "uncertainty_count": len(uncertainty_items),
            "trace_fields": list(comparison["summary"]["trace_fields"]),
        },
        "investigation": _investigation_dict(investigation),
        "findings": finding_items,
        "uncertainties": uncertainty_items,
        "detail_endpoint": {
            "method": "GET",
            "path": "/api/v2/compare/"
            + quote(left.trace.trace_id, safe="")
            + "/"
            + quote(right.trace.trace_id, safe=""),
            "comparison_version": "0.2",
        },
    }
    if len(_canonical_json_dumps(result).encode("utf-8")) > MAX_COMPARISON_RESPONSE_BYTES:
        raise ComparisonTooLargeError("comparison response limit exceeded")
    return result
