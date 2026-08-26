"""Production-backed V03-10 regression coverage.

The oracle tests establish corpus expectations.  This module feeds the same
scenarios through TraceMotive's ingest, SQLite read, comparison, divergence,
and investigation implementations before comparing those results with the
oracle.
"""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any
from uuid import uuid4
import unittest

from tests.divergence_evaluation import build_evaluation_corpus, capture_public_baseline
from tracemotive._evaluation.divergence import (
    DivergenceScenario,
    ProductionOutcome,
    count_false_confidence,
)
from tracemotive.api_v3 import build_v3_comparison
from tracemotive.canonical import Capture, CaptureInfo
from tracemotive.collector import Collector, create_app
from tracemotive.divergence import BehavioralCandidate, analyze_divergence
from tracemotive.comparison import compare_trace_inputs
from tracemotive.storage import TraceQueryRecord, TraceStats
from tests.test_query_api import request


_EMITTED_AT = "2026-08-26T00:00:00.000000Z"
_INVALID_DUPLICATE_ID_SCENARIO = "duplicate_structural_id_invalid_structure"


def _event(event_type: str, payload: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "emitted_at": _EMITTED_AT,
        "payload": payload.to_dict(),
    }


def _run_events(run: Any) -> list[dict[str, Any]]:
    """Build a valid lifecycle batch for one evaluation-side Canonical run."""

    started_trace = replace(
        run.trace,
        ended_at=None,
        status="unset",
    )
    events = [_event("trace.started", started_trace)]
    for span in run.spans:
        started_span = replace(
            span,
            ended_at=None,
            status="unset",
            error=None,
            output=None,
            capture=Capture(
                span.capture.input,
                CaptureInfo("not_captured", "not_yet_available", False),
            ),
        )
        # ``replace`` revalidates a Span.  Its second privacy pass cannot
        # rediscover the historical ``redacted`` bit from an already-sanitized
        # value, so restore the trusted started-side CaptureInfo.
        object.__setattr__(
            started_span,
            "capture",
            Capture(
                span.capture.input,
                CaptureInfo("not_captured", "not_yet_available", False),
            ),
        )
        events.append(_event("span.started", started_span))
        events.append(_event("span.ended", span))
    if run.trace.ended_at is not None:
        events.append(_event("trace.ended", run.trace))
    return events


def _stats(run: Any) -> TraceStats:
    input_tokens = sum(
        span.details.usage.input_tokens or 0
        for span in run.spans
        if span.type == "llm"
    )
    output_tokens = sum(
        span.details.usage.output_tokens or 0
        for span in run.spans
        if span.type == "llm"
    )
    return TraceStats(
        len(run.spans),
        sum(span.status == "error" for span in run.spans),
        sum(span.type == "llm" for span in run.spans),
        input_tokens,
        output_tokens,
    )


def _oracle_side(
    scenario: DivergenceScenario,
    reference: dict[str, Any] | None,
) -> Any:
    if reference is None:
        return None
    return (
        scenario.right
        if reference.get("trace_id") == scenario.right.trace.trace_id
        else scenario.left
    )


def _label_for_reference(
    scenario: DivergenceScenario,
    reference: dict[str, Any] | None,
) -> str | None:
    side = _oracle_side(scenario, reference)
    if side is None:
        return None
    span_id = reference.get("span_id") if isinstance(reference, dict) else None
    return side.labels.get(str(span_id))


def _candidate_path_for_scenario(
    scenario: DivergenceScenario,
    candidate: BehavioralCandidate,
) -> str | None:
    """Project the production candidate onto the oracle's stable path label."""

    coordinate = candidate.coordinate
    if coordinate.kind == "sibling_group":
        assert coordinate.group_signature is not None
        return (
            f"group:{coordinate.group_signature.operation}/"
            f"{coordinate.group_signature.name}"
        )

    name = coordinate.semantic_path[-1].name
    if candidate.kind.startswith("execution_subtree"):
        reference = candidate.right or candidate.left
        label = _label_for_reference(scenario, reference)
        subtree_labels = {
            "customer-route": "Customer route",
            "plan": "Plan",
        }
        return f"subtree:{subtree_labels.get(label or '', name)}"
    if candidate.field_path is not None:
        return f"span:{name}{candidate.field_path}"
    return f"span:{name}"


def _finding_path(
    scenario: DivergenceScenario,
    finding: dict[str, Any],
) -> str | None:
    """Project the API starting point using the same conservative labels."""

    finding_type = finding["type"]
    coordinate = finding["coordinate"]
    if coordinate["kind"] == "sibling_group":
        signature = coordinate["group_signature"]
        return f"group:{signature['operation']}/{signature['name']}"

    reference = finding.get("right") or finding.get("left")
    label = _label_for_reference(scenario, reference)
    name = coordinate["semantic_path"][-1]["name"]
    if finding_type.startswith("execution_subtree"):
        subtree_labels = {
            "customer-route": "Customer route",
            "plan": "Plan",
        }
        name = subtree_labels.get(label or "", name)
        return f"subtree:{name}"
    field_path = finding.get("field_path")
    if field_path is not None:
        return f"span:{name}{field_path}"
    return f"span:{name}"


class V0310ProductionRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_public_baseline()
        cls.scenarios = build_evaluation_corpus()

    def _production_outcome(
        self,
        scenario: DivergenceScenario,
    ) -> tuple[ProductionOutcome, str | None, set[str]]:
        repository_ingested = scenario.name != _INVALID_DUPLICATE_ID_SCENARIO
        if repository_ingested:
            with Collector() as collector:
                for run in (scenario.left, scenario.right):
                    result = collector.ingest(
                        {"protocol_version": 1, "events": _run_events(run)}
                    )
                    self.assertEqual(result["stale"], 0)
                left_input, right_input = collector.repository.get_trace_comparison_inputs(
                    scenario.left.trace.trace_id,
                    scenario.right.trace.trace_id,
                )
                assert left_input is not None and right_input is not None
                left_record = left_input.record
                right_record = right_input.record
                left_spans = left_input.spans
                right_spans = right_input.spans
                app = create_app(collector.repository)
                status, body = request(
                    app,
                    "GET",
                    "/api/v3/compare/"
                    f"{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}",
                )
                self.assertEqual(status, 200)
                api_result = json.loads(body)
        else:
            # The invalid fixture repeats a Canonical span_id on the right.
            # Normal SQLite/API persistence cannot retain that duplicated
            # member: the v0.1 primary key is (trace_id, span_id), and an
            # identical repeated lifecycle snapshot collapses instead of
            # preserving malformed structure.  Use the direct persisted-read-
            # model builder here so the comparator's invalid-structure barrier
            # remains covered; every other scenario uses ingest/SQLite.
            left_record = TraceQueryRecord(scenario.left.trace, _stats(scenario.left))
            right_record = TraceQueryRecord(
                scenario.right.trace,
                _stats(scenario.right),
            )
            left_spans = scenario.left.spans
            right_spans = scenario.right.spans
            api_result = build_v3_comparison(
                left_record,
                left_spans,
                right_record,
                right_spans,
            )

        comparison = compare_trace_inputs(
            left_record,
            left_spans,
            right_record,
            right_spans,
        )
        divergence = analyze_divergence(
            left_record,
            left_spans,
            right_record,
            right_spans,
            comparison=comparison,
        )
        selected_path = (
            _candidate_path_for_scenario(scenario, divergence.candidate)
            if divergence.candidate
            else None
        )
        starting_point = api_result["investigation"]["starting_point"]
        starting_path: str | None = None
        if starting_point is not None:
            finding_id = starting_point["finding_id"]
            finding = next(
                item for item in api_result["findings"] if item["finding_id"] == finding_id
            )
            starting_path = _finding_path(scenario, finding)

        barriers = {barrier.reason_code for barrier in divergence.barriers}

        outcome = ProductionOutcome(
            scenario.name,
            meaningful_confident=divergence.state == "supported",
            starting_point_confident=api_result["investigation"]["state"] == "identified",
            candidate_path=selected_path,
        )
        return outcome, starting_path, barriers

    def test_all_30_scenarios_use_production_engine_and_have_zero_false_confidence(self) -> None:
        outcomes: list[ProductionOutcome] = []
        self.assertEqual(len(self.scenarios), 30)

        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                outcome, starting_path, production_barriers = self._production_outcome(
                    scenario
                )
                outcomes.append(outcome)

                expected_meaningful = {
                    "supported": "supported",
                    "uncertain": "uncertain",
                    "none": "none",
                }[scenario.meaningful_divergence]
                self.assertEqual(outcome.meaningful_confident, expected_meaningful == "supported")
                if expected_meaningful == "supported":
                    self.assertIn(
                        outcome.candidate_path,
                        scenario.allowed_candidate_paths,
                    )
                else:
                    self.assertIsNone(outcome.candidate_path)

                expected_starting_state = {
                    "supported": "identified",
                    "uncertain": "uncertain",
                    "none": "none",
                }[scenario.investigation_starting_point]
                self.assertEqual(outcome.starting_point_confident, expected_starting_state == "identified")
                if scenario.investigation_starting_point == "supported":
                    self.assertEqual(starting_path, scenario.expected_starting_point_path)
                else:
                    self.assertIsNone(starting_path)

                if scenario.uncertainty_barrier is None:
                    self.assertEqual(production_barriers, set())
                else:
                    self.assertIn(scenario.uncertainty_barrier, production_barriers)

                forbidden = set(scenario.forbidden_confident_candidates)
                if outcome.candidate_path is not None:
                    self.assertNotIn(outcome.candidate_path, forbidden)
                if starting_path is not None:
                    self.assertNotIn(starting_path, forbidden)

        counts = count_false_confidence(self.scenarios, outcomes)
        self.assertEqual(counts.meaningful_divergence, 0)
        self.assertEqual(counts.investigation_starting_point, 0)
        self.assertEqual(counts.expected_confident_meaningful, 15)
        self.assertEqual(counts.correctly_confident_meaningful, 15)
        self.assertEqual(counts.expected_uncertain_meaningful, 6)
        self.assertEqual(counts.safely_withheld_meaningful, 6)
        self.assertEqual(counts.expected_confident_starting_point, 14)
        self.assertEqual(counts.correctly_confident_starting_point, 14)
        self.assertEqual(counts.expected_uncertain_starting_point, 7)
        self.assertEqual(counts.safely_withheld_starting_point, 7)


if __name__ == "__main__":
    unittest.main()
