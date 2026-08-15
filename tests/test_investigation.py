from __future__ import annotations

from dataclasses import replace
import unittest

from tests.divergence_evaluation import build_evaluation_corpus, capture_public_baseline
from tests.test_divergence import _stats
from tracemotive._evaluation.divergence import (
    _captured_output,
    _model_change,
    _parameter_change,
    _redacted_input,
    _tool_signature,
    clone_run,
)
from tracemotive.canonical import CaptureInfo
from tracemotive.divergence import (
    BehavioralCandidate,
    DivergenceResult,
    ReliablyMatchedPoint,
    StructuralCoordinate,
    UncertaintyBarrier,
    analyze_divergence,
)
from tracemotive.findings import DiagnosticFinding, DiagnosticFindings, collect_findings
from tracemotive.investigation import (
    InvestigationSummaryConsistencyError,
    build_investigation_summary,
)
from tracemotive.storage import TraceQueryRecord


def _record(run) -> TraceQueryRecord:
    return TraceQueryRecord(run.trace, _stats(run))


def _results(left, right):
    divergence = analyze_divergence(
        _record(left),
        left.spans,
        _record(right),
        right.spans,
    )
    findings = collect_findings(
        _record(left),
        left.spans,
        _record(right),
        right.spans,
    )
    return divergence, findings, build_investigation_summary(divergence, findings)


def _ref(side: str, span_id: str) -> dict[str, str]:
    return {"trace_id": f"{side}-trace", "span_id": span_id}


def _coordinate(*segments: dict[str, str | int]) -> StructuralCoordinate:
    return StructuralCoordinate.span(segments)


def _output_finding(
    finding_id: str,
    coordinate: StructuralCoordinate,
    left: dict[str, str],
    right: dict[str, str],
) -> DiagnosticFinding:
    return DiagnosticFinding(
        finding_id,
        "tool_output_changed",
        coordinate,
        left,
        right,
        "/output",
        "behavioral",
        "confirmed_observation",
        "captured_values_differ",
        {"left": {"state": "captured"}, "right": {"state": "captured"}},
    )


def _supported_output(
    coordinate: StructuralCoordinate,
    left: dict[str, str],
    right: dict[str, str],
    barriers: tuple[UncertaintyBarrier, ...] = (),
) -> DivergenceResult:
    return DivergenceResult(
        "supported",
        BehavioralCandidate(
            "aligned_tool_output_changed",
            coordinate,
            left,
            right,
            "/output",
            "captured_values_differ",
            (),
        ),
        barriers,
        ReliablyMatchedPoint("none", (), None, None, "no_prior_resolved_point"),
    )


class InvestigationSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_public_baseline()
        cls.scenarios = build_evaluation_corpus()
        cls.by_name = {scenario.name: scenario for scenario in cls.scenarios}

    def test_all_30_scenarios_preserve_v03_11_starting_point_boundary(self) -> None:
        expected_state = {
            "none": "NO_BEHAVIORAL_DIVERGENCE",
            "supported": "INVESTIGATION_POINT",
            "uncertain": "UNCERTAIN",
        }
        primary_count = 0
        withheld_count = 0
        context_only_without_primary = 0
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                divergence, findings, summary = _results(scenario.left, scenario.right)
                expected = (
                    "INVESTIGATION_POINT"
                    if scenario.investigation_starting_point == "supported"
                    else "UNCERTAIN"
                    if scenario.investigation_starting_point == "uncertain"
                    else expected_state[scenario.meaningful_divergence]
                )
                self.assertEqual(summary.state, expected)
                self.assertEqual(
                    summary.last_reliably_matched_point,
                    divergence.last_reliably_matched_point.to_dict(),
                )
                self.assertEqual(
                    set(summary.context_finding_ids),
                    {
                        finding.finding_id
                        for finding in findings.findings
                        if finding.scope == "context_only"
                    },
                )
                supplied_barriers = {
                    (
                        barrier.reason_code,
                        barrier.side,
                        barrier.coordinate,
                        barrier.blocks_earlier_claim,
                    )
                    for barrier in findings.uncertainties
                }
                self.assertTrue(
                    all(
                        (
                            uncertainty.reason_code,
                            uncertainty.side,
                            uncertainty.coordinate,
                            uncertainty.blocks_earlier_claim,
                        )
                        in supplied_barriers
                        for uncertainty in summary.uncertainties
                    )
                )
                if expected == "INVESTIGATION_POINT":
                    primary_count += 1
                    self.assertIsNotNone(summary.primary)
                    self.assertIsNotNone(divergence.candidate)
                    self.assertEqual(
                        summary.primary.coordinate,
                        divergence.candidate.coordinate,
                    )
                    self.assertEqual(
                        summary.primary.divergence_kind,
                        divergence.candidate.kind,
                    )
                    self.assertEqual(len([summary.primary]), 1)
                    self.assertTrue(summary.primary.finding_ids)
                    self.assertEqual(len(summary.primary.finding_ids), 1)
                    primary_finding = next(
                        finding
                        for finding in findings.findings
                        if finding.finding_id == summary.primary.finding_ids[0]
                    )
                    self.assertEqual(primary_finding.scope, "behavioral")
                    self.assertEqual(primary_finding.coordinate, divergence.candidate.coordinate)
                    self.assertEqual(primary_finding.left, divergence.candidate.left)
                    self.assertEqual(primary_finding.right, divergence.candidate.right)
                    self.assertEqual(primary_finding.field_path, divergence.candidate.field_path)
                    self.assertEqual(primary_finding.reason_code, divergence.candidate.reason_code)
                    self.assertNotIn(
                        primary_finding.finding_id,
                        {item.finding_id for item in summary.evidence_summary},
                    )
                else:
                    withheld_count += expected == "UNCERTAIN"
                    self.assertIsNone(summary.primary)
                self.assertEqual(
                    {item.finding_id for item in summary.evidence_summary},
                    {
                        finding.finding_id
                        for finding in findings.findings
                        if finding.scope == "behavioral"
                        and (
                            summary.primary is None
                            or finding.finding_id not in summary.primary.finding_ids
                        )
                    },
                )
                if scenario.meaningful_divergence == "none" and not any(
                    finding.scope == "behavioral" for finding in findings.findings
                ):
                    context_only_without_primary += 1
                    self.assertIsNone(summary.primary)
        self.assertEqual(primary_count, 14)
        self.assertEqual(withheld_count, 7)
        self.assertEqual(context_only_without_primary, 9)

    def test_context_only_scenarios_have_findings_but_no_primary(self) -> None:
        expected_types = {
            "model_only_change": "model_changed",
            "request_parameter_only_change": "request_parameters_changed",
            "trace_status_only_change": "trace_status_changed",
        }
        for name, finding_type in expected_types.items():
            with self.subTest(scenario=name):
                _, findings, summary = _results(
                    self.by_name[name].left,
                    self.by_name[name].right,
                )
                self.assertEqual(summary.state, "NO_BEHAVIORAL_DIVERGENCE")
                self.assertIsNone(summary.primary)
                self.assertEqual(summary.context_finding_ids, ("finding-0001",))
                self.assertEqual(
                    [finding.type for finding in findings.findings], [finding_type]
                )
                self.assertEqual(summary.evidence_summary, ())

    def test_primary_uses_existing_v03_20_finding_ids_without_payload_copy(self) -> None:
        scenario = self.by_name["multiple_independent_divergences"]
        _, findings, summary = _results(scenario.left, scenario.right)
        self.assertIsNotNone(summary.primary)
        self.assertEqual(summary.primary.finding_ids, ("finding-0001",))
        wire = summary.to_dict()
        self.assertNotIn("observed", wire)
        self.assertNotIn("evidence", wire)
        self.assertEqual(
            {item.finding_id for item in summary.evidence_summary},
            {
                finding.finding_id
                for finding in findings.findings
                if finding.scope == "behavioral"
                and finding.finding_id not in summary.primary.finding_ids
            },
        )

    def test_missing_finding_is_not_synthesized(self) -> None:
        scenario = self.by_name["aligned_tool_output_change"]
        divergence, findings, _ = _results(scenario.left, scenario.right)
        reduced = DiagnosticFindings(
            tuple(
                finding
                for finding in findings.findings
                if finding.scope == "context_only"
            ),
            findings.uncertainties,
        )
        with self.assertRaises(InvestigationSummaryConsistencyError):
            build_investigation_summary(divergence, reduced)

    def test_last_match_and_uncertainties_are_structural_records(self) -> None:
        scenario = self.by_name["redacted_content"]
        divergence, findings, summary = _results(scenario.left, scenario.right)
        self.assertEqual(summary.state, "UNCERTAIN")
        self.assertIsNone(summary.primary)
        self.assertEqual(
            summary.last_reliably_matched_point,
            divergence.last_reliably_matched_point.to_dict(),
        )
        self.assertEqual(
            [item.reason_code for item in summary.uncertainties],
            ["redacted_observation"],
        )
        self.assertEqual(
            summary.uncertainties[0].uncertainty_id,
            "uncertainty-0001",
        )
        self.assertEqual(
            len(summary.uncertainties),
            len({item.uncertainty_id for item in summary.uncertainties}),
        )
        self.assertEqual(
            set(summary.blocking_uncertainty_ids),
            {"uncertainty-0001"},
        )
        self.assertEqual(
            summary.uncertainties[0].reason_code,
            findings.uncertainties[0].reason_code,
        )
        self.assertEqual(
            summary.uncertainties[0].coordinate,
            findings.uncertainties[0].coordinate,
        )

    def test_eight_composite_cases_keep_selection_and_relationships_separate(self) -> None:
        def output_change(span):
            return replace(
                span,
                output={"alerts": ["composite"]},
                capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
            )

        def context_change(span):
            span = _model_change("offline-model-composite")(span)
            return _parameter_change({"temperature": 0.7})(span)

        output_left = clone_run(
            self.base,
            "composite-output-left",
            span_mutators={"alerts": _captured_output({"alerts": []})},
        )
        output_right = clone_run(
            self.base,
            "composite-output-right",
            span_mutators={"alerts": output_change},
        )
        later_error_order = (*self.base.order, "customer-tool")
        later_error_extras = (("customer-tool", "alerts", "agent-root"),)
        later_error_left = clone_run(
            output_left,
            "later-error-left",
            order=later_error_order,
            extras=later_error_extras,
            span_mutators={
                "customer-tool": _tool_signature("Lookup customer", "lookup_customer"),
            },
        )
        later_error_right = clone_run(
            output_right,
            "later-error-right",
            order=later_error_order,
            extras=later_error_extras,
            span_mutators={
                "customer-tool": _tool_signature("Lookup customer", "lookup_customer"),
            },
            error_labels=("customer-tool",),
        )
        cases = {
            "model_output_later_error": (
                later_error_left,
                clone_run(
                    later_error_right,
                    "model-output-error-right",
                    span_mutators={"plan": _model_change("offline-model-composite")},
                ),
                "INVESTIGATION_POINT",
            ),
            "earlier_ambiguity_later_addition": (
                self.by_name["missing_parent"].left,
                self.by_name["missing_parent"].right,
                "UNCERTAIN",
            ),
            "output_independent_sibling_error": (
                later_error_left,
                later_error_right,
                "INVESTIGATION_POINT",
            ),
            "output_descendant_error": (
                output_left,
                clone_run(
                    output_right,
                    "output-descendant-error-right",
                    error_labels=("normalize-alerts",),
                ),
                "INVESTIGATION_POINT",
            ),
            "multiple_context_one_behavior": (
                output_left,
                clone_run(
                    output_right,
                    "multiple-context-right",
                    status="error",
                    span_mutators={"plan": context_change, "alerts": output_change},
                ),
                "INVESTIGATION_POINT",
            ),
            "redacted_uncertainty_later_addition": (
                self.base,
                clone_run(
                    self.base,
                    "redacted-later-addition-right",
                    order=(*self.base.order, "customer-tool"),
                    extras=(("customer-tool", "alerts", "agent-root"),),
                    span_mutators={
                        "alerts": _redacted_input("unavailable-original"),
                        "customer-tool": _tool_signature("Lookup customer", "lookup_customer"),
                    },
                ),
                "UNCERTAIN",
            ),
            "repetition_later_context": (
                self.base,
                clone_run(
                    self.base,
                    "repetition-later-context-right",
                    status="error",
                    order=(
                        "agent-root",
                        "plan",
                        "lookup-1",
                        "normalize-1",
                        "lookup-extra",
                        "lookup-2",
                        "normalize-2",
                        "lookup-3",
                        "normalize-3",
                        "alerts",
                        "normalize-alerts",
                        "synthesis",
                    ),
                    extras=(("lookup-extra", "lookup-2", "agent-root"),),
                ),
                "INVESTIGATION_POINT",
            ),
            "incomplete_known_earlier_behavior": (
                output_left,
                clone_run(
                    output_right,
                    "incomplete-known-earlier-right",
                    incomplete=True,
                    span_mutators={"alerts": output_change},
                ),
                "INVESTIGATION_POINT",
            ),
        }
        for name, (left, right, expected_state) in cases.items():
            with self.subTest(case=name):
                divergence, findings, summary = _results(left, right)
                self.assertEqual(summary.state, expected_state)
                if expected_state == "UNCERTAIN":
                    self.assertIsNone(summary.primary)
                else:
                    self.assertIsNotNone(summary.primary)
                    self.assertIn(
                        summary.primary.finding_ids[0],
                        {finding.finding_id for finding in findings.findings},
                    )

        _, _, first = _results(cases["model_output_later_error"][0], cases["model_output_later_error"][1])
        self.assertEqual(first.context_finding_ids, ("finding-0001",))
        self.assertTrue(
            any(
                item.relation == "observed_after"
                for item in first.evidence_summary
            )
        )

        _, _, ambiguous = _results(
            cases["earlier_ambiguity_later_addition"][0],
            cases["earlier_ambiguity_later_addition"][1],
        )
        self.assertIn("uncertainty-0001", ambiguous.blocking_uncertainty_ids)
        self.assertTrue(
            any(
                item.relation == "blocked_by_uncertainty"
                and item.structural_relation == "additional_observation"
                for item in ambiguous.evidence_summary
            )
        )

        _, _, descendant = _results(
            cases["output_descendant_error"][0],
            cases["output_descendant_error"][1],
        )
        self.assertTrue(
            any(
                item.relation == "descendant_evidence"
                and item.structural_relation == "descendant"
                for item in descendant.evidence_summary
            )
        )

        _, _, sibling = _results(
            cases["output_independent_sibling_error"][0],
            cases["output_independent_sibling_error"][1],
        )
        self.assertTrue(
            any(
                item.relation == "observed_after"
                and item.structural_relation == "structurally_later_independent"
                for item in sibling.evidence_summary
            )
        )

        _, _, repetition = _results(
            cases["repetition_later_context"][0],
            cases["repetition_later_context"][1],
        )
        self.assertIn("finding-0002", repetition.context_finding_ids)
        self.assertFalse(repetition.evidence_summary)

    def test_timestamp_and_persistence_order_do_not_change_summary_semantics(self) -> None:
        repeated_tail = (
            "lookup-1",
            "normalize-1",
            "lookup-2",
            "normalize-2",
            "lookup-3",
            "normalize-3",
        )

        def summary_for(name, *, overrides=None, reverse=False):
            left = clone_run(
                self.base,
                f"{name}-left",
                drop=repeated_tail,
                timing_overrides_us=overrides,
            )
            right = clone_run(
                self.base,
                f"{name}-right",
                drop=repeated_tail,
                span_mutators={"alerts": _captured_output({"alerts": ["notice"]})},
                timing_overrides_us=overrides,
            )
            if reverse:
                left = replace(left, spans=tuple(reversed(left.spans)))
                right = replace(right, spans=tuple(reversed(right.spans)))
            return _results(left, right)[2]

        def semantic(summary):
            wire = summary.to_dict()
            if wire["primary"] is not None:
                wire["primary"].pop("left", None)
                wire["primary"].pop("right", None)
            wire["last_reliably_matched_point"].pop("left", None)
            wire["last_reliably_matched_point"].pop("right", None)
            return wire

        normal = summary_for("normal")
        swapped = summary_for(
            "swapped",
            overrides={
                "plan": 1_700_000_900_000_000,
                "alerts": 1_700_000_100_000_000,
            },
        )
        equal = summary_for(
            "equal",
            overrides={label: 1_700_000_500_000_000 for label in self.base.order},
        )
        shifted_reversed = summary_for(
            "shifted-reversed",
            overrides={
                "plan": 1_700_000_900_000_000,
                "alerts": 1_700_000_100_000_000,
            },
            reverse=True,
        )
        for candidate in (swapped, equal, shifted_reversed):
            with self.subTest(candidate=candidate.to_dict()):
                self.assertEqual(candidate.state, normal.state)
                self.assertEqual(semantic(candidate), semantic(normal))
        self.assertNotIn("started_at", normal.to_json())
        self.assertNotIn("ended_at", normal.to_json())

    def test_composition_rejects_inconsistent_behavioral_inputs(self) -> None:
        root = {"type": "agent", "operation": "agent.run", "name": "root", "ordinal": 0}
        earlier = _coordinate(
            root,
            {"type": "tool", "operation": "tool.call", "name": "Lookup alerts", "ordinal": 0},
        )
        primary_coordinate = _coordinate(
            root,
            {"type": "tool", "operation": "tool.call", "name": "Lookup weather", "ordinal": 0},
        )
        earlier_left, earlier_right = _ref("left", "earlier-left"), _ref("right", "earlier-right")
        primary_left, primary_right = _ref("left", "primary-left"), _ref("right", "primary-right")
        divergence = _supported_output(primary_coordinate, primary_left, primary_right)
        primary = _output_finding("finding-primary", primary_coordinate, primary_left, primary_right)
        with self.assertRaises(InvestigationSummaryConsistencyError):
            build_investigation_summary(
                divergence,
                DiagnosticFindings(
                    (_output_finding("finding-earlier", earlier, earlier_left, earlier_right), primary),
                    (),
                ),
            )
        with self.assertRaises(InvestigationSummaryConsistencyError):
            build_investigation_summary(
                divergence,
                DiagnosticFindings(
                    (primary, _output_finding("finding-primary", primary_coordinate, primary_left, primary_right)),
                    (),
                ),
            )
        with self.assertRaises(InvestigationSummaryConsistencyError):
            build_investigation_summary(
                divergence,
                DiagnosticFindings(
                    (primary, _output_finding("finding-second", primary_coordinate, primary_left, primary_right)),
                    (),
                ),
            )
        barrier = UncertaintyBarrier(
            "missing_parent",
            "right",
            None,
            False,
            ({"kind": "structural_limitation", "span": "missing"},),
        )
        with self.assertRaises(InvestigationSummaryConsistencyError):
            build_investigation_summary(
                DivergenceResult(
                    "uncertain",
                    None,
                    (barrier,),
                    ReliablyMatchedPoint("none", (), None, None, "before_uncertainty_barrier"),
                ),
                DiagnosticFindings((), ()),
            )
        with self.assertRaises(InvestigationSummaryConsistencyError):
            build_investigation_summary(
                DivergenceResult(
                    "none",
                    None,
                    (),
                    ReliablyMatchedPoint("none", (), None, None, "no_prior_resolved_point"),
                ),
                DiagnosticFindings((), (barrier,)),
            )

    def test_finding_ids_are_references_not_ordering_or_primary_semantics(self) -> None:
        root = {"type": "agent", "operation": "agent.run", "name": "root", "ordinal": 0}
        primary_coordinate = _coordinate(
            root,
            {"type": "tool", "operation": "tool.call", "name": "Lookup alerts", "ordinal": 0},
        )
        later_coordinate = _coordinate(
            root,
            {"type": "tool", "operation": "tool.call", "name": "Lookup weather", "ordinal": 0},
        )
        left, right = _ref("left", "alerts-left"), _ref("right", "alerts-right")
        primary = _output_finding("finding-9999", primary_coordinate, left, right)
        later = _output_finding(
            "finding-0001",
            later_coordinate,
            _ref("left", "weather-left"),
            _ref("right", "weather-right"),
        )
        context = DiagnosticFinding(
            "finding-1111",
            "model_changed",
            primary_coordinate,
            left,
            right,
            "/details/request_model",
            "context_only",
            "confirmed_observation",
            "known_model_changed",
            {"left": {"state": "known"}, "right": {"state": "known"}},
        )
        divergence = _supported_output(primary_coordinate, left, right)
        summary = build_investigation_summary(
            divergence,
            DiagnosticFindings((later, context, primary), ()),
        )
        self.assertEqual(summary.primary.finding_ids, ("finding-9999",))
        self.assertEqual(
            [item.finding_id for item in summary.evidence_summary],
            ["finding-0001"],
        )
        self.assertEqual(summary.context_finding_ids, ("finding-1111",))
        self.assertEqual(
            [item.relation for item in summary.evidence_summary],
            ["observed_after"],
        )

    def test_group_coordinate_never_identifies_a_member_descendant(self) -> None:
        root = {"type": "agent", "operation": "agent.run", "name": "root", "ordinal": 0}
        weather = {"type": "tool", "operation": "tool.call", "name": "Lookup weather"}
        group_coordinate = StructuralCoordinate.sibling_group((root,), weather)
        member_child = _coordinate(
            root,
            {**weather, "ordinal": 1},
            {"type": "tool", "operation": "tool.call", "name": "Normalize weather", "ordinal": 0},
        )
        divergence = DivergenceResult(
            "supported",
            BehavioralCandidate(
                "repeated_tool_group_cardinality_changed",
                group_coordinate,
                None,
                None,
                None,
                "deterministic_group_count_changed",
                (),
            ),
            (),
            ReliablyMatchedPoint("none", (), None, None, "no_prior_resolved_point"),
        )
        primary = DiagnosticFinding(
            "finding-group",
            "tool_repetition_changed",
            group_coordinate,
            None,
            None,
            None,
            "behavioral",
            "confirmed_observation",
            "deterministic_group_count_changed",
            {"left": {"state": "known"}, "right": {"state": "known"}},
        )
        member = _output_finding(
            "finding-member",
            member_child,
            _ref("left", "member-left"),
            _ref("right", "member-right"),
        )
        summary = build_investigation_summary(
            divergence,
            DiagnosticFindings((member, primary), ()),
        )
        member_reference = next(
            item for item in summary.evidence_summary if item.finding_id == "finding-member"
        )
        self.assertEqual(member_reference.relation, "observed_after")
        self.assertEqual(member_reference.structural_relation, "additional_observation")

    def test_uncertainty_deduplication_preserves_distinct_barriers_and_order(self) -> None:
        coordinate = _coordinate(
            {"type": "agent", "operation": "agent.run", "name": "root", "ordinal": 0}
        )

        def barrier(
            evidence: tuple[dict[str, object], ...],
            *,
            side: str = "both",
            at: StructuralCoordinate | None = None,
            blocks: bool = True,
        ) -> UncertaintyBarrier:
            return UncertaintyBarrier("missing_parent", side, at, blocks, evidence)

        first = barrier(({"kind": "structural_limitation", "span": "one"},))
        duplicate = barrier(({"kind": "structural_limitation", "span": "one"},))
        different_evidence = barrier(({"kind": "structural_limitation", "span": "two"},))
        different_coordinate = barrier(({"kind": "structural_limitation", "span": "one"},), at=coordinate)
        different_side = barrier(({"kind": "structural_limitation", "span": "one"},), side="left")
        different_blocking = barrier(({"kind": "structural_limitation", "span": "one"},), blocks=False)
        barriers = (
            first,
            duplicate,
            different_evidence,
            different_coordinate,
            different_side,
            different_blocking,
        )
        divergence = DivergenceResult(
            "uncertain",
            None,
            barriers,
            ReliablyMatchedPoint("none", (), None, None, "before_uncertainty_barrier"),
        )
        forward = build_investigation_summary(divergence, DiagnosticFindings((), barriers))
        reversed_summary = build_investigation_summary(
            DivergenceResult(
                "uncertain",
                None,
                tuple(reversed(barriers)),
                ReliablyMatchedPoint("none", (), None, None, "before_uncertainty_barrier"),
            ),
            DiagnosticFindings((), tuple(reversed(barriers))),
        )
        self.assertEqual(len(forward.uncertainties), 5)
        self.assertEqual(forward.to_json(), reversed_summary.to_json())
        self.assertEqual(forward.blocking_uncertainty_ids, tuple(item.uncertainty_id for item in forward.uncertainties))

    def test_context_and_later_behavior_remain_non_primary_when_required(self) -> None:
        def all_context(span):
            return _parameter_change({"temperature": 0.7})(
                _model_change("offline-model-context")(span)
            )

        context_right = clone_run(
            self.base,
            "context-only-summary-right",
            status="error",
            span_mutators={"plan": all_context},
        )
        divergence, findings, summary = _results(self.base, context_right)
        self.assertEqual(divergence.state, "none")
        self.assertEqual(summary.state, "NO_BEHAVIORAL_DIVERGENCE")
        self.assertIsNone(summary.primary)
        self.assertEqual(
            {finding.type for finding in findings.findings},
            {"model_changed", "request_parameters_changed", "trace_status_changed"},
        )
        self.assertEqual(
            set(summary.context_finding_ids),
            {finding.finding_id for finding in findings.findings},
        )
        self.assertEqual(summary.evidence_summary, ())

        output_left = clone_run(
            self.base,
            "blocked-later-left",
            span_mutators={"alerts": _captured_output({"alerts": []})},
        )
        output_right = clone_run(
            self.base,
            "blocked-later-right",
            order=(*self.base.order, "customer-tool"),
            extras=(("customer-tool", "alerts", "agent-root"),),
            missing_parent_labels=("normalize-alerts",),
            error_labels=("customer-tool",),
            span_mutators={
                "alerts": _captured_output({"alerts": ["notice"]}),
                "customer-tool": _tool_signature("Lookup customer", "lookup_customer"),
            },
        )
        _, blocked_findings, blocked = _results(output_left, output_right)
        self.assertEqual(blocked.state, "UNCERTAIN")
        self.assertIsNone(blocked.primary)
        self.assertTrue(blocked.blocking_uncertainty_ids)
        self.assertTrue(
            {"tool_output_changed", "tool_added", "new_error"}
            <= {finding.type for finding in blocked_findings.findings}
        )
        self.assertTrue(
            all(
                item.relation == "blocked_by_uncertainty"
                for item in blocked.evidence_summary
            )
        )

    def test_later_ambiguity_and_sibling_behavior_do_not_change_safe_primary(self) -> None:
        output_left = clone_run(
            self.base,
            "safe-primary-left",
            span_mutators={"alerts": _captured_output({"alerts": []})},
        )
        output_right = clone_run(
            self.base,
            "safe-primary-right",
            order=(
                "agent-root",
                "plan",
                "lookup-2",
                "normalize-2",
                "lookup-1",
                "normalize-1",
                "lookup-3",
                "normalize-3",
                "alerts",
                "normalize-alerts",
                "synthesis",
            ),
            span_mutators={"alerts": _captured_output({"alerts": ["notice"]})},
        )
        _, _, summary = _results(output_left, output_right)
        self.assertEqual(summary.state, "INVESTIGATION_POINT")
        self.assertEqual(summary.primary.divergence_kind, "aligned_tool_output_changed")
        self.assertEqual(summary.blocking_uncertainty_ids, ())
        self.assertIn("repeated_sibling_ambiguity", {item.reason_code for item in summary.uncertainties})

        sibling_right = clone_run(
            output_right,
            "sibling-error-right",
            error_labels=("plan",),
        )
        _, sibling_findings, sibling = _results(output_left, sibling_right)
        self.assertEqual(sibling.state, "INVESTIGATION_POINT")
        self.assertEqual(sibling.primary.divergence_kind, "aligned_span_error_changed")
        output_reference = next(
            item
            for item in sibling.evidence_summary
            if next(
                finding.type
                for finding in sibling_findings.findings
                if finding.finding_id == item.finding_id
            )
            == "tool_output_changed"
        )
        self.assertEqual(output_reference.relation, "observed_after")
        self.assertEqual(output_reference.structural_relation, "structurally_later_independent")

    def test_last_reliably_matched_point_remains_neutral_when_it_has_an_error(self) -> None:
        left = clone_run(
            self.base,
            "matched-error-left",
            status="error",
            error_labels=("plan",),
            span_mutators={"alerts": _captured_output({"alerts": []})},
        )
        right = clone_run(
            self.base,
            "matched-error-right",
            status="error",
            error_labels=("plan",),
            span_mutators={"alerts": _captured_output({"alerts": ["notice"]})},
        )
        divergence, _, summary = _results(left, right)
        self.assertEqual(summary.state, "INVESTIGATION_POINT")
        self.assertEqual(
            summary.last_reliably_matched_point,
            divergence.last_reliably_matched_point.to_dict(),
        )
        self.assertEqual(summary.last_reliably_matched_point["state"], "matched")
        self.assertEqual(summary.last_reliably_matched_point["reason"], "before_first_finding")
        self.assertNotIn("healthy", summary.to_json())
        self.assertNotIn("successful", summary.to_json())


if __name__ == "__main__":
    unittest.main()
