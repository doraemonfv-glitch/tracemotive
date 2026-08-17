"""Additive v0.4 comparison projection.

The v4 route composes the unchanged v3 investigation result with a bounded
structured-diff projection.  It does not alter alignment, finding selection,
Canonical data, or any v1/v2/v3 response.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import quote

from tracemotive.api_v3 import build_v3_comparison
from tracemotive.canonical.models import Span, _canonical_json_dumps
from tracemotive.comparison import (
    MAX_COMPARISON_RESPONSE_BYTES,
    ComparisonTooLargeError,
)
from tracemotive.storage.repository import TraceQueryRecord
from tracemotive.structured_diff import structured_diff


COMPARISON_VERSION = "0.4"


class V4CompositionError(RuntimeError):
    """Raised when the additive v0.4 projection cannot be composed safely."""


def _trace_identity(record: TraceQueryRecord) -> dict[str, str]:
    return {
        "trace_id": record.trace.trace_id,
        "name": record.trace.name,
        "status": record.trace.status,
    }


def _ref(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    trace_id = value.get("trace_id")
    span_id = value.get("span_id")
    if not isinstance(trace_id, str) or not isinstance(span_id, str):
        return None
    return {"trace_id": trace_id, "span_id": span_id}


def _stable_hash_route(left_trace_id: str, right_trace_id: str) -> str:
    return "#/compare/" + quote(left_trace_id, safe="") + "/" + quote(right_trace_id, safe="")


def _last_point(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise V4CompositionError("v3 last matched point is invalid")
    semantic_path = value.get("semantic_path")
    if not isinstance(semantic_path, list):
        raise V4CompositionError("v3 last matched point path is invalid")
    return {
        "state": value.get("state"),
        "left": _ref(value.get("left")),
        "right": _ref(value.get("right")),
        "coordinate": semantic_path,
        "reason": value.get("reason"),
    }


def _alignment_state(v3: Mapping[str, Any]) -> str:
    summary = v3.get("summary")
    investigation = v3.get("investigation")
    if not isinstance(summary, Mapping) or not isinstance(investigation, Mapping):
        raise V4CompositionError("v3 summary is invalid")
    alignment = summary.get("alignment")
    if not isinstance(alignment, Mapping):
        raise V4CompositionError("v3 alignment is invalid")
    if (
        investigation.get("state") == "uncertain"
        or alignment.get("ambiguous_groups", 0) != 0
        or alignment.get("unavailable_spans", 0) != 0
    ):
        return "uncertain"
    return "complete"


def _capture_state_reason(observed: Any) -> str:
    if not isinstance(observed, Mapping):
        return "unsupported_observation"
    states = []
    for side in ("left", "right"):
        item = observed.get(side)
        if not isinstance(item, Mapping):
            return "unsupported_observation"
        state = item.get("state")
        if not isinstance(state, str):
            return "unsupported_observation"
        states.append(state)
    if "redacted" in states:
        return "redacted_observation"
    if any(state not in {"captured"} for state in states):
        return "capture_unavailable"
    return "unsupported_observation"


def _structured_diff_for_finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    finding_type = finding.get("type")
    observed = finding.get("observed")
    eligible = finding_type in {"tool_input_changed", "tool_output_changed"}
    if not eligible:
        return {
            "structured_diff_available": False,
            "structured_diff_reason": "unsupported_observation",
        }
    if not isinstance(observed, Mapping):
        return {
            "structured_diff_available": False,
            "structured_diff_reason": "unsupported_observation",
        }
    left = observed.get("left")
    right = observed.get("right")
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return {
            "structured_diff_available": False,
            "structured_diff_reason": "unsupported_observation",
        }
    if left.get("state") != "captured" or right.get("state") != "captured":
        return {
            "structured_diff_available": False,
            "structured_diff_reason": _capture_state_reason(observed),
        }
    if "value" not in left or "value" not in right:
        return {
            "structured_diff_available": False,
            "structured_diff_reason": "unsupported_observation",
        }
    field_path = finding.get("field_path")
    if not isinstance(field_path, str):
        return {
            "structured_diff_available": False,
            "structured_diff_reason": "unsupported_observation",
        }
    try:
        result = structured_diff(
            left["value"],
            right["value"],
            path_prefix=field_path,
        )
    except (TypeError, ValueError, RecursionError):
        return {
            "structured_diff_available": False,
            "structured_diff_reason": "unsupported_observation",
        }
    return {
        "structured_diff_available": True,
        "structured_diff": list(result.records),
        "structured_diff_truncated": result.truncated,
        "structured_diff_reason": result.reason,
    }


def _relationships(value: Any) -> dict[str, list[dict[str, Any]]]:
    supports: list[dict[str, Any]] = []
    limited_by: list[dict[str, Any]] = []
    if isinstance(value, list):
        for relation in value:
            if not isinstance(relation, Mapping):
                continue
            item = {
                "relation": relation.get("relation"),
                "structural_relation": relation.get("structural_relation"),
            }
            if relation.get("relation") == "blocked_by_uncertainty":
                limited_by.append(item)
            else:
                supports.append(item)
    return {"supports": supports, "limited_by": limited_by}


def _finding_actions(
    finding: Mapping[str, Any],
) -> list[dict[str, Any]]:
    finding_id = finding.get("finding_id")
    if not isinstance(finding_id, str):
        raise V4CompositionError("finding ID is invalid")
    evidence = finding.get("evidence")
    return (
        [{"type": "copy_evidence", "target": {"finding_id": finding_id}}]
        if isinstance(evidence, list) and evidence
        else []
    )


def _finding_dict(finding: Mapping[str, Any]) -> dict[str, Any]:
    finding_id = finding.get("finding_id")
    if not isinstance(finding_id, str):
        raise V4CompositionError("finding ID is invalid")
    required = (
        "type",
        "coordinate",
        "left",
        "right",
        "field_path",
        "scope",
        "observation_state",
        "reason_code",
        "observed",
        "evidence",
        "relationships",
    )
    if any(key not in finding for key in required):
        raise V4CompositionError("v3 finding is incomplete")
    result: dict[str, Any] = {
        "id": finding_id,
        "type": finding["type"],
        "coordinate": finding["coordinate"],
        "left": _ref(finding.get("left")),
        "right": _ref(finding.get("right")),
        "scope": finding["scope"],
        "observation_state": finding["observation_state"],
        "reason_code": finding["reason_code"],
        "field_path": finding["field_path"],
        "observed": finding["observed"],
        "evidence": finding["evidence"],
        "relationships": _relationships(finding["relationships"]),
        "actions": _finding_actions(finding),
    }
    result.update(_structured_diff_for_finding(finding))
    return result


def _investigation_actions(
    v3: Mapping[str, Any],
    left_trace_id: str,
    right_trace_id: str,
) -> list[dict[str, Any]]:
    investigation = v3.get("investigation")
    if not isinstance(investigation, Mapping):
        raise V4CompositionError("v3 investigation is invalid")
    actions: list[dict[str, Any]] = []
    starting = investigation.get("starting_point")
    if isinstance(starting, Mapping) and investigation.get("state") == "identified":
        left = _ref(starting.get("left"))
        right = _ref(starting.get("right"))
        if left is not None:
            actions.append({"type": "open_left", "target": left})
        if right is not None:
            actions.append({"type": "open_right", "target": right})
    hash_route = _stable_hash_route(left_trace_id, right_trace_id)
    actions.extend(
        [
            {"type": "full_comparison", "target": {"hash": hash_route}},
            {"type": "copy_local_reference", "target": {"hash": hash_route}},
        ]
    )
    return actions


def build_v4_comparison(
    left: TraceQueryRecord,
    left_spans: Sequence[Span],
    right: TraceQueryRecord,
    right_spans: Sequence[Span],
) -> dict[str, Any]:
    """Build the additive v0.4 read model from one v3 composition."""

    v3 = build_v3_comparison(left, left_spans, right, right_spans)
    investigation = v3.get("investigation")
    findings = v3.get("findings")
    uncertainties = v3.get("uncertainties")
    if not isinstance(investigation, Mapping) or not isinstance(findings, list) or not isinstance(uncertainties, list):
        raise V4CompositionError("v3 response is incomplete")
    primary = investigation.get("starting_point")
    primary_id = primary.get("finding_id") if isinstance(primary, Mapping) else None
    if primary_id is not None and not isinstance(primary_id, str):
        raise V4CompositionError("v3 primary finding ID is invalid")
    finding_ids = [item.get("finding_id") for item in findings if isinstance(item, Mapping)]
    uncertainty_ids = [item.get("uncertainty_id") for item in uncertainties if isinstance(item, Mapping)]
    if any(not isinstance(item, str) for item in finding_ids + uncertainty_ids):
        raise V4CompositionError("v3 reference ID is invalid")
    left_id = left.trace.trace_id
    right_id = right.trace.trace_id
    result = {
        "comparison_version": COMPARISON_VERSION,
        "left": _trace_identity(left),
        "right": _trace_identity(right),
        "summary": {
            "alignment_state": _alignment_state(v3),
            "investigation_state": investigation.get("state"),
            "last_reliably_matched_point": _last_point(
                investigation.get("last_reliably_matched_point")
            ),
        },
        "investigation": {
            "primary_finding_id": primary_id,
            "finding_ids": finding_ids,
            "uncertainty_ids": uncertainty_ids,
            "actions": _investigation_actions(v3, left_id, right_id),
        },
        "findings": [_finding_dict(item) for item in findings],
        "uncertainties": uncertainties,
    }
    if len(_canonical_json_dumps(result).encode("utf-8")) > MAX_COMPARISON_RESPONSE_BYTES:
        raise ComparisonTooLargeError("comparison response limit exceeded")
    return result


__all__ = ["COMPARISON_VERSION", "V4CompositionError", "build_v4_comparison"]
