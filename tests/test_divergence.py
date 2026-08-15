from __future__ import annotations

from dataclasses import replace
import unittest

from tests.divergence_evaluation import build_evaluation_corpus, capture_public_baseline
from tracemotive.canonical import CaptureInfo, LLMDetails, ToolDetails
from tracemotive.divergence import DivergenceResult, analyze_divergence
from tracemotive.storage import TraceQueryRecord, TraceStats
from tracemotive._evaluation.divergence import (
    BASELINE_EPOCH_US,
    ProductionOutcome,
    clone_run,
    count_false_confidence,
)


def _stats(run) -> TraceStats:
    return TraceStats(
        len(run.spans),
        sum(span.status == "error" for span in run.spans),
        sum(span.type == "llm" for span in run.spans),
        sum(span.details.usage.input_tokens or 0 for span in run.spans if span.type == "llm"),
        sum(span.details.usage.output_tokens or 0 for span in run.spans if span.type == "llm"),
    )


def _analyze(scenario) -> DivergenceResult:
    return _analyze_runs(scenario.left, scenario.right)


def _analyze_runs(left, right) -> DivergenceResult:
    return analyze_divergence(
        TraceQueryRecord(left.trace, _stats(left)),
        left.spans,
        TraceQueryRecord(right.trace, _stats(right)),
        right.spans,
    )


def _candidate_path(scenario, result: DivergenceResult) -> str | None:
    candidate = result.candidate
    if candidate is None:
        return None
    coordinate = candidate.coordinate
    if coordinate.kind == "sibling_group":
        assert coordinate.group_signature is not None
        return f"group:{coordinate.group_signature.operation}/{coordinate.group_signature.name}"
    reference = candidate.right or candidate.left
    if reference is None:
        return None
    run = scenario.right if candidate.right is not None else scenario.left
    span = next(span for span in run.spans if span.span_id == reference["span_id"])
    if candidate.kind.startswith("execution_subtree"):
        label = run.labels[span.span_id]
        subtree_label = {
            "customer-route": "Customer route",
            "plan": "Plan",
        }.get(label, span.name)
        return f"subtree:{subtree_label}"
    if candidate.field_path is not None:
        return f"span:{span.name}{candidate.field_path}"
    return f"span:{span.name}"


def _outcome(scenario) -> ProductionOutcome:
    result = _analyze(scenario)
    path = _candidate_path(scenario, result)
    starting_confident = result.state == "supported" and not any(
        barrier.blocks_earlier_claim or barrier.reason_code == "missing_parent"
        for barrier in result.barriers
    )
    return ProductionOutcome(
        scenario.name,
        meaningful_confident=result.state == "supported",
        starting_point_confident=starting_confident,
        candidate_path=path,
    )


class DivergenceEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_public_baseline()
        cls.scenarios = build_evaluation_corpus()
        cls.by_name = {scenario.name: scenario for scenario in cls.scenarios}

    def test_production_engine_matches_all_oracle_states_and_candidates(self) -> None:
        outcomes = []
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                result = _analyze(scenario)
                path = _candidate_path(scenario, result)
                if scenario.meaningful_divergence == "supported":
                    self.assertEqual(result.state, "supported")
                    self.assertIn(path, scenario.allowed_candidate_paths)
                elif scenario.meaningful_divergence == "uncertain":
                    self.assertEqual(result.state, "uncertain")
                    self.assertIsNone(result.candidate)
                else:
                    self.assertEqual(result.state, "none")
                    self.assertIsNone(result.candidate)
                outcomes.append(_outcome(scenario))

        counts = count_false_confidence(self.scenarios, outcomes)
        self.assertEqual(counts.meaningful_divergence, 0)
        self.assertEqual(counts.investigation_starting_point, 0)
        self.assertEqual(counts.expected_confident_meaningful, 15)
        self.assertEqual(counts.correctly_confident_meaningful, 15)
        self.assertEqual(counts.expected_confident_starting_point, 14)
        self.assertEqual(counts.correctly_confident_starting_point, 14)
        self.assertEqual(counts.expected_uncertain_meaningful, 6)
        self.assertEqual(counts.safely_withheld_meaningful, 6)
        self.assertEqual(counts.expected_uncertain_starting_point, 7)
        self.assertEqual(counts.safely_withheld_starting_point, 7)

    def test_identical_runs_and_context_only_signals_return_none(self) -> None:
        for name in (
            "identical_runs",
            "timing_only_variation",
            "token_only_variation",
            "model_only_change",
            "request_parameter_only_change",
            "trace_status_only_change",
            "framework_metadata_only_difference",
            "large_irrelevant_metadata_difference",
        ):
            with self.subTest(scenario=name):
                result = _analyze(self.by_name[name])
                self.assertEqual(result.state, "none")
                self.assertIsNone(result.candidate)

    def test_aligned_content_and_exact_error_changes_are_behavioral(self) -> None:
        output = _analyze(self.by_name["aligned_tool_output_change"])
        self.assertEqual(output.state, "supported")
        self.assertEqual(output.candidate.kind, "aligned_tool_output_changed")
        self.assertEqual(output.candidate.field_path, "/output")

        input_result = _analyze(self.by_name["aligned_tool_input_change"])
        self.assertEqual(input_result.state, "supported")
        self.assertEqual(input_result.candidate.kind, "aligned_tool_input_changed")
        self.assertEqual(input_result.candidate.field_path, "/input")

        for name, reason in (
            ("new_error_exact_span", "error_observed"),
            ("resolved_error_exact_span", "error_resolved"),
        ):
            with self.subTest(scenario=name):
                result = _analyze(self.by_name[name])
                self.assertEqual(result.state, "supported")
                self.assertEqual(result.candidate.kind, "aligned_span_error_changed")
                self.assertEqual(result.candidate.reason_code, reason)

    def test_structural_add_remove_and_group_cardinality_are_safe(self) -> None:
        for name, kind in (
            ("unique_tool_added", "unique_tool_added"),
            ("unique_tool_removed", "unique_tool_removed"),
            ("execution_subtree_added", "execution_subtree_added"),
            ("execution_subtree_removed", "execution_subtree_removed"),
            ("repeated_tool_insertion", "repeated_tool_group_cardinality_changed"),
            ("repeated_tool_removal", "repeated_tool_group_cardinality_changed"),
        ):
            with self.subTest(scenario=name):
                result = _analyze(self.by_name[name])
                self.assertEqual(result.state, "supported")
                self.assertEqual(result.candidate.kind, kind)
        for name in ("repeated_tool_insertion", "repeated_tool_removal"):
            result = _analyze(self.by_name[name])
            self.assertIsNone(result.candidate.left)
            self.assertIsNone(result.candidate.right)

    def test_repeated_ambiguity_is_localized_and_never_ordinal_paired(self) -> None:
        reorder = _analyze(self.by_name["repeated_tool_reordering"])
        self.assertEqual(reorder.state, "uncertain")
        self.assertIsNone(reorder.candidate)
        self.assertIn("repeated_sibling_ambiguity", {barrier.reason_code for barrier in reorder.barriers})

        nested = _analyze(self.by_name["nested_repeated_groups"])
        self.assertEqual(nested.state, "uncertain")
        self.assertIsNone(nested.candidate)

        insertion = _analyze(self.by_name["repeated_tool_insertion"])
        self.assertEqual(insertion.candidate.coordinate.kind, "sibling_group")
        self.assertNotIn("ordinal", insertion.candidate.coordinate.to_dict()["group_signature"])

    def test_barriers_do_not_become_confident_absence_or_content(self) -> None:
        for name, reason in (
            ("capture_disabled_one_side", "capture_unavailable"),
            ("redacted_content", "redacted_observation"),
            ("early_termination_partial_trace", "incomplete_trace"),
            ("duplicate_structural_id_invalid_structure", "invalid_structure"),
        ):
            with self.subTest(scenario=name):
                result = _analyze(self.by_name[name])
                self.assertEqual(result.state, "uncertain")
                self.assertIsNone(result.candidate)
                self.assertIn(reason, {barrier.reason_code for barrier in result.barriers})

        missing_parent = _analyze(self.by_name["missing_parent"])
        self.assertEqual(missing_parent.state, "supported")
        self.assertEqual(missing_parent.candidate.kind, "unique_tool_added")
        self.assertIn("missing_parent", {barrier.reason_code for barrier in missing_parent.barriers})

    def test_redacted_equal_placeholders_remain_unknown(self) -> None:
        def redacted_placeholder(value):
            def mutate(span):
                return replace(
                    span,
                    input={"api_key": value},
                    capture=replace(span.capture, input=CaptureInfo("captured", None, False)),
                )

            return mutate

        left = clone_run(
            self.base,
            "redacted-equal-left",
            span_mutators={"alerts": redacted_placeholder("left-original")},
        )
        right = clone_run(
            self.base,
            "redacted-equal-right",
            span_mutators={"alerts": redacted_placeholder("right-original")},
        )
        result = _analyze_runs(left, right)
        self.assertEqual(result.state, "uncertain")
        self.assertIsNone(result.candidate)
        self.assertIn("redacted_observation", {barrier.reason_code for barrier in result.barriers})

    def test_equal_cardinality_changed_repeated_member_data_is_not_paired(self) -> None:
        def changed_member(span):
            return replace(
                span,
                output={"temperature": 99},
                capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
            )

        left = clone_run(self.base, "equal-repeat-left")
        right = clone_run(self.base, "equal-repeat-right", span_mutators={"lookup-2": changed_member})
        result = _analyze_runs(left, right)
        self.assertEqual(result.state, "uncertain")
        self.assertIsNone(result.candidate)
        self.assertIn("repeated_sibling_ambiguity", {barrier.reason_code for barrier in result.barriers})

    def test_incomplete_identical_runs_do_not_report_no_difference(self) -> None:
        left = clone_run(self.base, "incomplete-identical-left", order=self.base.order[:6], incomplete=True)
        right = clone_run(self.base, "incomplete-identical-right", order=self.base.order[:6], incomplete=True)
        result = _analyze_runs(left, right)
        self.assertEqual(result.state, "uncertain")
        self.assertIsNone(result.candidate)
        self.assertIn("incomplete_trace", {barrier.reason_code for barrier in result.barriers})

    def test_context_change_before_behavior_does_not_steal_composite_candidate(self) -> None:
        def model_change(span):
            self.assertIsInstance(span.details, LLMDetails)
            return replace(span, details=replace(span.details, request_model="offline-model-composite"))

        def changed_alerts(span):
            return replace(
                span,
                output={"alerts": ["composite-notice"]},
                capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
            )

        # Independent prediction from the frozen rules: model context and
        # timestamps are ignored; the uniquely aligned alerts output is the
        # supported behavioral candidate, while the unchanged repeated group
        # contributes no candidate.
        left = clone_run(
            self.base,
            "composite-left",
            timing_offset_us=0,
            span_mutators={
                "alerts": lambda span: replace(
                    span,
                    output={"alerts": []},
                    capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
                )
            },
        )
        right = clone_run(
            self.base,
            "composite-right",
            timing_offset_us=7_000_000,
            span_mutators={"plan": model_change, "alerts": changed_alerts},
        )
        result = _analyze_runs(left, right)
        self.assertEqual(result.state, "supported")
        self.assertEqual(result.candidate.kind, "aligned_tool_output_changed")
        self.assertEqual(result.candidate.field_path, "/output")
        self.assertEqual(result.candidate.coordinate.semantic_path[-1].name, "Lookup alerts")

    def test_timestamp_variants_do_not_select_between_behavioral_branches(self) -> None:
        scenario = self.by_name["multiple_independent_divergences"]

        def result_for(
            name: str,
            *,
            timing_overrides_us=None,
            left_offset_us: int = 0,
            right_offset_us: int = 0,
            reverse_inputs: bool = False,
        ) -> DivergenceResult:
            left = clone_run(
                scenario.left,
                f"{name}-left",
                timing_offset_us=left_offset_us,
                timing_overrides_us=timing_overrides_us,
            )
            right = clone_run(
                scenario.right,
                f"{name}-right",
                timing_offset_us=right_offset_us,
                timing_overrides_us=timing_overrides_us,
            )
            left_spans = tuple(reversed(left.spans)) if reverse_inputs else left.spans
            right_spans = tuple(reversed(right.spans)) if reverse_inputs else right.spans
            return analyze_divergence(
                TraceQueryRecord(left.trace, _stats(left)),
                left_spans,
                TraceQueryRecord(right.trace, _stats(right)),
                right_spans,
            )

        expected = ("supported", "aligned_span_error_changed", "Plan")
        variants = (
            result_for(
                "timestamp-normal",
                timing_overrides_us={
                    "plan": BASELINE_EPOCH_US + 100_000,
                    "alerts": BASELINE_EPOCH_US + 900_000,
                },
            ),
            result_for(
                "timestamp-swapped",
                timing_overrides_us={
                    "plan": BASELINE_EPOCH_US + 900_000,
                    "alerts": BASELINE_EPOCH_US + 100_000,
                },
            ),
            result_for(
                "timestamp-equal",
                timing_overrides_us={label: BASELINE_EPOCH_US + 500_000 for label in scenario.left.order},
            ),
            result_for("timestamp-shifted", right_offset_us=10**12),
            result_for(
                "timestamp-swapped-reversed-input",
                timing_overrides_us={
                    "plan": BASELINE_EPOCH_US + 900_000,
                    "alerts": BASELINE_EPOCH_US + 100_000,
                },
                reverse_inputs=True,
            ),
        )
        for result in variants:
            with self.subTest(result=result.to_dict()):
                self.assertIsNotNone(result.candidate)
                self.assertEqual(
                    (result.state, result.candidate.kind, result.candidate.coordinate.semantic_path[-1].name),
                    expected,
                )

    def test_timestamp_only_repeated_order_does_not_hide_later_behavior(self) -> None:
        def zebra_output(value):
            def mutate(span):
                self.assertIsInstance(span.details, ToolDetails)
                return replace(
                    span,
                    name="Lookup zebra",
                    details=replace(span.details, tool_name="lookup_zebra"),
                    output={"value": value},
                    capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
                )

            return mutate

        order = (*self.base.order, "zebra-tool")
        extras = (("zebra-tool", "alerts", "agent-root"),)
        left = clone_run(
            self.base,
            "timestamp-group-left",
            order=order,
            extras=extras,
            span_mutators={"zebra-tool": zebra_output("left")},
        )
        normal_right = clone_run(
            self.base,
            "timestamp-group-normal-right",
            order=order,
            extras=extras,
            span_mutators={"zebra-tool": zebra_output("right")},
        )
        timestamp_reordered_right = clone_run(
            self.base,
            "timestamp-group-reordered-right",
            order=order,
            extras=extras,
            timing_overrides_us={
                "lookup-1": BASELINE_EPOCH_US + 300_000,
                "lookup-2": BASELINE_EPOCH_US + 100_000,
                "lookup-3": BASELINE_EPOCH_US + 200_000,
            },
            span_mutators={"zebra-tool": zebra_output("right")},
        )

        normal = _analyze_runs(left, normal_right)
        reordered = _analyze_runs(left, timestamp_reordered_right)
        for result in (normal, reordered):
            self.assertEqual(result.state, "supported")
            self.assertEqual(result.candidate.kind, "aligned_tool_output_changed")
            self.assertEqual(result.candidate.coordinate.semantic_path[-1].name, "Lookup zebra")

    def test_unrelated_missing_parent_barrier_does_not_hide_known_sibling(self) -> None:
        result = _analyze(self.by_name["missing_parent"])
        self.assertEqual(result.state, "supported")
        self.assertEqual(result.candidate.kind, "unique_tool_added")
        self.assertEqual(result.candidate.coordinate.semantic_path[-1].name, "Lookup customer")
        self.assertIn("missing_parent", {barrier.reason_code for barrier in result.barriers})

    def test_multiple_sibling_and_nested_repeated_cases_remain_conservative(self) -> None:
        multiple = _analyze(self.by_name["multiple_independent_divergences"])
        self.assertEqual(multiple.state, "supported")
        self.assertEqual(multiple.candidate.coordinate.semantic_path[-1].name, "Plan")

        nested = _analyze(self.by_name["nested_repeated_groups"])
        self.assertEqual(nested.state, "uncertain")
        self.assertIsNone(nested.candidate)
        self.assertIn("repeated_sibling_ambiguity", {barrier.reason_code for barrier in nested.barriers})

    def test_error_order_is_structural_not_chronological(self) -> None:
        result = _analyze(self.by_name["chronological_vs_lexicographic_order"])
        self.assertEqual(result.state, "supported")
        self.assertEqual(result.candidate.kind, "aligned_span_error_changed")
        self.assertEqual(result.candidate.coordinate.semantic_path[-1].name, "Plan")

    def test_failure_modes_are_not_silent_always_none_or_uncertain(self) -> None:
        outcomes = [
            ProductionOutcome(
                scenario.name,
                meaningful_confident=False,
                starting_point_confident=False,
                candidate_path=None,
            )
            for scenario in self.scenarios
        ]
        counts = count_false_confidence(self.scenarios, outcomes)
        self.assertLess(counts.correctly_confident_meaningful, counts.expected_confident_meaningful)

        real_outcomes = [_outcome(scenario) for scenario in self.scenarios]
        real_counts = count_false_confidence(self.scenarios, real_outcomes)
        self.assertEqual(real_counts.correctly_confident_meaningful, 15)
        self.assertEqual(real_counts.correctly_confident_starting_point, 14)

    def test_serialized_result_is_deterministic(self) -> None:
        scenario = self.by_name["multiple_independent_divergences"]
        self.assertEqual(_analyze(scenario).to_json(), _analyze(scenario).to_json())

    def test_input_span_order_does_not_change_serialized_result(self) -> None:
        scenario = self.by_name["multiple_independent_divergences"]
        forward = analyze_divergence(
            TraceQueryRecord(scenario.left.trace, _stats(scenario.left)),
            scenario.left.spans,
            TraceQueryRecord(scenario.right.trace, _stats(scenario.right)),
            scenario.right.spans,
        )
        reversed_inputs = analyze_divergence(
            TraceQueryRecord(scenario.left.trace, _stats(scenario.left)),
            tuple(reversed(scenario.left.spans)),
            TraceQueryRecord(scenario.right.trace, _stats(scenario.right)),
            tuple(reversed(scenario.right.spans)),
        )
        self.assertEqual(forward.to_json(), reversed_inputs.to_json())


if __name__ == "__main__":
    unittest.main()
