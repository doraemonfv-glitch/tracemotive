"""Deterministic internal V03-21 investigation summary composition.

This module combines already-authoritative V03-11 and V03-20 results.  It
does not compare traces, rebuild alignment, select new observations, or infer
task relevance, failure relevance, or an explanation for later observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tracemotive.canonical.models import _canonical_json_dumps
from tracemotive.divergence import (
    BehavioralCandidate,
    DivergenceResult,
    StructuralCoordinate,
    StructuralSegment,
    UncertaintyBarrier,
)
from tracemotive.findings import DiagnosticFinding, DiagnosticFindings


SummaryState = Literal[
    "NO_BEHAVIORAL_DIVERGENCE",
    "INVESTIGATION_POINT",
    "UNCERTAIN",
]
SummaryRelation = Literal[
    "same_structural_region",
    "descendant_evidence",
    "observed_after",
    "blocked_by_uncertainty",
]
StructuralRelation = Literal[
    "same_coordinate",
    "descendant",
    "structurally_later_independent",
    "unrelated_branch",
    "additional_observation",
]


class InvestigationSummaryConsistencyError(ValueError):
    """Raised when the supplied authoritative results cannot be composed safely."""


@dataclass(frozen=True, slots=True)
class PrimaryInvestigationPoint:
    """The one safe structural point selected from the V03-11 candidate."""

    divergence_kind: str
    coordinate: StructuralCoordinate
    left: dict[str, str] | None
    right: dict[str, str] | None
    field_path: str | None
    reason_code: str
    finding_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "divergence_kind": self.divergence_kind,
            "coordinate": self.coordinate.to_dict(),
            "left": self.left,
            "right": self.right,
            "field_path": self.field_path,
            "reason_code": self.reason_code,
            "finding_ids": list(self.finding_ids),
        }


@dataclass(frozen=True, slots=True)
class EvidenceFindingReference:
    """A compact reference to a supplied finding, without copying its payload."""

    finding_id: str
    relation: SummaryRelation
    structural_relation: StructuralRelation

    def to_dict(self) -> dict[str, str]:
        return {
            "finding_id": self.finding_id,
            "relation": self.relation,
            "structural_relation": self.structural_relation,
        }


@dataclass(frozen=True, slots=True)
class SummaryUncertainty:
    """A compact, deduplicated reference to an existing V03-11 barrier."""

    uncertainty_id: str
    reason_code: str
    side: Literal["left", "right", "both"]
    coordinate: StructuralCoordinate | None
    blocks_earlier_claim: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "uncertainty_id": self.uncertainty_id,
            "reason_code": self.reason_code,
            "side": self.side,
            "coordinate": None if self.coordinate is None else self.coordinate.to_dict(),
            "blocks_earlier_claim": self.blocks_earlier_claim,
        }


@dataclass(frozen=True, slots=True)
class InvestigationSummary:
    """The machine-readable V03-21 composition result."""

    state: SummaryState
    ordering_basis: Literal["structural_triage_order"]
    primary: PrimaryInvestigationPoint | None
    last_reliably_matched_point: dict[str, Any]
    evidence_summary: tuple[EvidenceFindingReference, ...]
    context_finding_ids: tuple[str, ...]
    uncertainties: tuple[SummaryUncertainty, ...]
    blocking_uncertainty_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ordering_basis": self.ordering_basis,
            "primary": None if self.primary is None else self.primary.to_dict(),
            "last_reliably_matched_point": self.last_reliably_matched_point,
            "evidence_summary": [item.to_dict() for item in self.evidence_summary],
            "context_finding_ids": list(self.context_finding_ids),
            "uncertainties": [item.to_dict() for item in self.uncertainties],
            "blocking_uncertainty_ids": list(self.blocking_uncertainty_ids),
        }

    def to_json(self) -> str:
        return _canonical_json_dumps(self.to_dict())


_CANDIDATE_FINDING_TYPES = {
    "aligned_tool_input_changed": "tool_input_changed",
    "aligned_tool_output_changed": "tool_output_changed",
    "unique_tool_added": "tool_added",
    "unique_tool_removed": "tool_removed",
    "execution_subtree_added": "execution_subtree_added",
    "execution_subtree_removed": "execution_subtree_removed",
    "repeated_tool_group_cardinality_changed": "tool_repetition_changed",
}
_FINDING_TYPE_ORDER = {
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


def _candidate_finding_type(candidate: BehavioralCandidate) -> str:
    if candidate.kind == "aligned_span_error_changed":
        if candidate.reason_code == "error_observed":
            return "new_error"
        if candidate.reason_code == "error_resolved":
            return "resolved_error"
        raise InvestigationSummaryConsistencyError(
            "V03-11 error candidate has no V03-20 lifecycle mapping"
        )
    try:
        return _CANDIDATE_FINDING_TYPES[candidate.kind]
    except KeyError as exc:
        raise InvestigationSummaryConsistencyError(
            "V03-11 candidate has no V03-20 finding mapping"
        ) from exc


def _candidate_finding_matches(
    finding: DiagnosticFinding,
    candidate: BehavioralCandidate,
) -> bool:
    return (
        finding.scope == "behavioral"
        and finding.type == _candidate_finding_type(candidate)
        and finding.coordinate == candidate.coordinate
        and finding.left == candidate.left
        and finding.right == candidate.right
        and finding.field_path == candidate.field_path
        and finding.reason_code == candidate.reason_code
    )


def _coordinate_path(coordinate: StructuralCoordinate) -> tuple[StructuralSegment, ...]:
    if coordinate.kind == "sibling_group" and coordinate.group_signature is not None:
        return (*coordinate.semantic_path, coordinate.group_signature)
    return coordinate.semantic_path


def _is_descendant(
    ancestor: StructuralCoordinate,
    descendant: StructuralCoordinate,
) -> bool:
    if (
        ancestor.kind in {"trace_summary", "sibling_group"}
        or descendant.kind == "trace_summary"
    ):
        # A sibling-group coordinate intentionally has no member identity.
        # V03-21 therefore cannot assert that an individual member record is
        # descendant evidence of that group-level observation.
        return False
    ancestor_path = _coordinate_path(ancestor)
    descendant_path = _coordinate_path(descendant)
    return (
        len(descendant_path) > len(ancestor_path)
        and descendant_path[: len(ancestor_path)] == ancestor_path
    )


def _same_parent(
    left: StructuralCoordinate,
    right: StructuralCoordinate,
) -> bool:
    left_path = _coordinate_path(left)
    right_path = _coordinate_path(right)
    return bool(left_path and right_path and left_path[:-1] == right_path[:-1])


def _barrier_precedes_candidate(
    barrier: UncertaintyBarrier,
    candidate: BehavioralCandidate,
) -> bool:
    if not barrier.blocks_earlier_claim:
        return False
    if barrier.coordinate is None:
        return True
    return barrier.coordinate.sort_key() <= candidate.coordinate.sort_key()


def _blocks_starting_point(
    barrier: UncertaintyBarrier,
    candidate: BehavioralCandidate,
) -> bool:
    # V03-11 treats a missing parent as withholding a global starting point
    # even when a later unique observation remains reportable.
    return (
        barrier.reason_code == "missing_parent"
        or _barrier_precedes_candidate(barrier, candidate)
    )


def _barrier_key(barrier: UncertaintyBarrier) -> tuple[Any, ...]:
    return (
        (0,) if barrier.coordinate is None else barrier.coordinate.sort_key(),
        barrier.reason_code,
        barrier.side,
        0 if barrier.blocks_earlier_claim else 1,
        _canonical_json_dumps(list(barrier.evidence)),
    )


def _barrier_semantic_key(barrier: UncertaintyBarrier) -> tuple[Any, ...]:
    return (
        barrier.reason_code,
        barrier.side,
        barrier.coordinate,
        barrier.blocks_earlier_claim,
        _canonical_json_dumps(list(barrier.evidence)),
    )


def _barrier_summary_key(barrier: UncertaintyBarrier) -> tuple[Any, ...]:
    """Return the compact barrier fields retained by ``SummaryUncertainty``."""

    return (
        barrier.reason_code,
        barrier.side,
        barrier.coordinate,
        barrier.blocks_earlier_claim,
    )


def _deduplicated_uncertainties(
    barriers: tuple[UncertaintyBarrier, ...],
) -> tuple[SummaryUncertainty, ...]:
    unique: dict[tuple[Any, ...], UncertaintyBarrier] = {}
    for barrier in barriers:
        key = _barrier_semantic_key(barrier)
        unique.setdefault(key, barrier)
    ordered = sorted(unique.values(), key=_barrier_key)
    return tuple(
        SummaryUncertainty(
            f"uncertainty-{index:04d}",
            barrier.reason_code,
            barrier.side,
            barrier.coordinate,
            barrier.blocks_earlier_claim,
        )
        for index, barrier in enumerate(ordered, start=1)
    )


def _validate_uncertainty_consistency(
    divergence: DivergenceResult,
    findings: DiagnosticFindings,
) -> None:
    divergence_keys = {_barrier_semantic_key(barrier) for barrier in divergence.barriers}
    finding_keys = {_barrier_semantic_key(barrier) for barrier in findings.uncertainties}
    if divergence.state == "none" and (divergence_keys or finding_keys):
        raise InvestigationSummaryConsistencyError(
            "V03-11 none cannot be composed with uncertainty records"
        )
    if divergence.state == "uncertain" and not divergence_keys:
        raise InvestigationSummaryConsistencyError(
            "V03-11 uncertain result has no uncertainty record"
        )
    if divergence_keys != finding_keys:
        raise InvestigationSummaryConsistencyError(
            "V03-11 and V03-20 uncertainty records differ"
        )


def _finding_sort_key(finding: DiagnosticFinding) -> tuple[Any, ...]:
    return (
        finding.coordinate.sort_key(),
        _FINDING_TYPE_ORDER[finding.type],
        finding.field_path or "",
        finding.reason_code,
    )


def _reference_for_finding(
    finding: DiagnosticFinding,
    *,
    summary_state: SummaryState,
    primary: PrimaryInvestigationPoint | None,
) -> EvidenceFindingReference:
    if primary is None:
        return EvidenceFindingReference(
            finding.finding_id,
            "blocked_by_uncertainty",
            "additional_observation",
        )

    if finding.coordinate == primary.coordinate:
        return EvidenceFindingReference(
            finding.finding_id,
            "same_structural_region",
            "same_coordinate",
        )
    if _is_descendant(primary.coordinate, finding.coordinate):
        return EvidenceFindingReference(
            finding.finding_id,
            "descendant_evidence",
            "descendant",
        )

    if finding.coordinate.sort_key() > primary.coordinate.sort_key():
        if primary.coordinate.kind == "sibling_group":
            return EvidenceFindingReference(
                finding.finding_id,
                "observed_after",
                "additional_observation",
            )
        structural_relation: StructuralRelation = (
            "structurally_later_independent"
            if _same_parent(primary.coordinate, finding.coordinate)
            else "unrelated_branch"
        )
        return EvidenceFindingReference(
            finding.finding_id,
            "observed_after",
            structural_relation,
        )

    return EvidenceFindingReference(
        finding.finding_id,
        "blocked_by_uncertainty" if summary_state == "UNCERTAIN" else "same_structural_region",
        "additional_observation",
    )


def build_investigation_summary(
    divergence: DivergenceResult,
    findings: DiagnosticFindings,
) -> InvestigationSummary:
    """Compose V03-21 from one V03-11 result and one V03-20 result.

    The inputs are already authoritative.  This function only maps the V03-11
    candidate to existing V03-20 finding IDs and classifies supplied records
    using their existing structural coordinates.
    """

    _validate_uncertainty_consistency(divergence, findings)
    ordered_findings = tuple(sorted(findings.findings, key=_finding_sort_key))
    finding_ids = [finding.finding_id for finding in ordered_findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise InvestigationSummaryConsistencyError("finding IDs must be unique")
    sort_keys = [_finding_sort_key(finding) for finding in ordered_findings]
    if len(sort_keys) != len(set(sort_keys)):
        raise InvestigationSummaryConsistencyError(
            "V03-20 findings have an ambiguous ID-independent sort key"
        )

    if divergence.state == "none":
        if any(finding.scope == "behavioral" for finding in ordered_findings):
            raise InvestigationSummaryConsistencyError(
                "V03-11 none cannot be composed with behavioral V03-20 findings"
            )
        summary_state: SummaryState = "NO_BEHAVIORAL_DIVERGENCE"
        primary = None
    elif divergence.state == "uncertain":
        summary_state = "UNCERTAIN"
        primary = None
    else:
        if divergence.candidate is None:
            raise InvestigationSummaryConsistencyError(
                "V03-11 supported result has no candidate"
            )
        matching_ids = tuple(
            finding.finding_id
            for finding in ordered_findings
            if _candidate_finding_matches(finding, divergence.candidate)
        )
        if len(matching_ids) != 1:
            raise InvestigationSummaryConsistencyError(
                "V03-11 candidate must match exactly one supplied V03-20 finding"
            )
        if any(
            _blocks_starting_point(barrier, divergence.candidate)
            for barrier in divergence.barriers
        ):
            summary_state = "UNCERTAIN"
            primary = None
        else:
            summary_state = "INVESTIGATION_POINT"
            primary = PrimaryInvestigationPoint(
                divergence.candidate.kind,
                divergence.candidate.coordinate,
                divergence.candidate.left,
                divergence.candidate.right,
                divergence.candidate.field_path,
                divergence.candidate.reason_code,
                matching_ids,
            )

    if primary is not None and any(
        finding.scope == "behavioral"
        and finding.coordinate.sort_key() < primary.coordinate.sort_key()
        for finding in ordered_findings
    ):
        raise InvestigationSummaryConsistencyError(
            "V03-20 contains behavioral evidence before the V03-11 primary"
        )

    uncertainties = _deduplicated_uncertainties(findings.uncertainties)
    if divergence.state == "none":
        blocking_uncertainty_ids: tuple[str, ...] = ()
    elif divergence.candidate is None:
        blocking_uncertainty_ids = tuple(item.uncertainty_id for item in uncertainties)
    else:
        blocking_keys = {
            _barrier_summary_key(barrier)
            for barrier in divergence.barriers
            if divergence.candidate is not None
            and _blocks_starting_point(barrier, divergence.candidate)
        }
        blocking_uncertainty_ids = tuple(
            item.uncertainty_id
            for item in uncertainties
            if (
                item.reason_code,
                item.side,
                item.coordinate,
                item.blocks_earlier_claim,
            )
            in blocking_keys
        )

    primary_finding_ids = () if primary is None else primary.finding_ids
    evidence_summary = tuple(
        _reference_for_finding(
            finding,
            summary_state=summary_state,
            primary=primary,
        )
        for finding in ordered_findings
        if finding.scope == "behavioral" and finding.finding_id not in primary_finding_ids
    )
    context_finding_ids = tuple(
        finding.finding_id
        for finding in ordered_findings
        if finding.scope == "context_only"
    )
    return InvestigationSummary(
        summary_state,
        "structural_triage_order",
        primary,
        divergence.last_reliably_matched_point.to_dict(),
        evidence_summary,
        context_finding_ids,
        uncertainties,
        blocking_uncertainty_ids,
    )


__all__ = [
    "EvidenceFindingReference",
    "InvestigationSummary",
    "InvestigationSummaryConsistencyError",
    "PrimaryInvestigationPoint",
    "SummaryUncertainty",
    "SummaryState",
    "build_investigation_summary",
]
