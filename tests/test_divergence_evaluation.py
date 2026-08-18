from __future__ import annotations

import unittest

from tests.divergence_evaluation import build_evaluation_corpus, capture_public_baseline
from tracemotive._evaluation.alignment import align_traces
from tracemotive._evaluation.divergence import (
    DIVERGENCE_STATES,
    OBSERVED_STATES,
    OUTCOME_CLASSES,
    STARTING_POINT_STATES,
    ProductionOutcome,
    count_false_confidence,
    serialize_corpus,
)


MANDATORY_SCENARIOS = {
    "identical_runs",
    "timing_only_variation",
    "token_only_variation",
    "model_only_change",
    "request_parameter_only_change",
    "aligned_tool_output_change",
    "aligned_tool_input_change",
    "capture_disabled_one_side",
    "redacted_content",
    "new_error_exact_span",
    "resolved_error_exact_span",
    "trace_status_only_change",
    "unique_tool_added",
    "unique_tool_removed",
    "execution_subtree_added",
    "execution_subtree_removed",
    "repeated_tool_insertion",
    "repeated_tool_removal",
    "repeated_tool_reordering",
    "nested_repeated_groups",
    "early_termination_partial_trace",
    "missing_parent",
    "duplicate_structural_id_invalid_structure",
    "both_runs_fail_identically",
    "both_runs_fail_differently",
    "error_before_later_structural_difference",
    "multiple_independent_divergences",
    "chronological_vs_lexicographic_order",
    "framework_metadata_only_difference",
    "large_irrelevant_metadata_difference",
}


def _span(run, label):
    for span_id, span_label in run.labels.items():
        if span_label == label:
            return next(span for span in run.spans if span.span_id == span_id)
    raise AssertionError(f"missing fixture label: {label}")


class DivergenceEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_public_baseline()
        cls.scenarios = build_evaluation_corpus()
        cls.by_name = {scenario.name: scenario for scenario in cls.scenarios}

    def test_public_path_baseline_is_realistic_and_sanitized(self) -> None:
        self.assertEqual(len(self.base.spans), 11)
        self.assertEqual(self.base.trace.schema_version, "0.1")
        self.assertEqual(sum(span.type == "agent" for span in self.base.spans), 1)
        self.assertEqual(sum(span.type == "llm" for span in self.base.spans), 2)
        self.assertEqual(sum(span.type == "tool" for span in self.base.spans), 4)
        self.assertTrue(all(span.input is None for span in self.base.spans))
        self.assertTrue(all(span.output is None for span in self.base.spans))

    def test_all_mandatory_scenarios_exist_and_names_are_unique(self) -> None:
        names = [scenario.name for scenario in self.scenarios]
        self.assertEqual(len(names), 30)
        self.assertEqual(set(names), MANDATORY_SCENARIOS)
        self.assertEqual(len(names), len(set(names)))

    def test_oracle_uses_closed_states_and_explicit_forbidden_candidates(self) -> None:
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                self.assertIn(scenario.observed_difference, OBSERVED_STATES)
                self.assertIn(scenario.meaningful_divergence, DIVERGENCE_STATES)
                self.assertIn(scenario.investigation_starting_point, STARTING_POINT_STATES)
                self.assertTrue(scenario.forbidden_confident_candidates)
                self.assertTrue(set(scenario.allowed_outcomes) <= OUTCOME_CLASSES)

    def test_supported_starting_points_are_exact_oracle_candidates(self) -> None:
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                if scenario.investigation_starting_point == "supported":
                    self.assertEqual(len(scenario.allowed_candidate_paths), 1)
                    self.assertEqual(
                        scenario.expected_starting_point_path,
                        scenario.allowed_candidate_paths[0],
                    )

    def test_oracle_output_contains_no_causal_terminology(self) -> None:
        serialized = serialize_corpus(self.scenarios).lower()
        for forbidden in ("cause", "causal", "root cause", "blame", "culprit", "responsible"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_timing_tokens_model_and_parameters_are_not_behavioral(self) -> None:
        for name in (
            "timing_only_variation",
            "token_only_variation",
            "model_only_change",
            "request_parameter_only_change",
            "trace_status_only_change",
            "framework_metadata_only_difference",
            "large_irrelevant_metadata_difference",
        ):
            with self.subTest(scenario=name):
                scenario = self.by_name[name]
                self.assertEqual(scenario.observed_difference, "present")
                self.assertEqual(scenario.meaningful_divergence, "none")
                self.assertEqual(scenario.investigation_starting_point, "none")

    def test_captured_content_can_support_aligned_input_and_output(self) -> None:
        output = self.by_name["aligned_tool_output_change"]
        self.assertEqual(_span(output.left, "alerts").capture.output.state, "captured")
        self.assertEqual(_span(output.right, "alerts").capture.output.state, "captured")
        self.assertNotEqual(_span(output.left, "alerts").output, _span(output.right, "alerts").output)
        self.assertEqual(output.meaningful_divergence, "supported")
        self.assertEqual(output.expected_starting_point_path, "span:Lookup alerts/output")

        input_scenario = self.by_name["aligned_tool_input_change"]
        self.assertEqual(_span(input_scenario.left, "alerts").capture.input.state, "captured")
        self.assertEqual(_span(input_scenario.right, "alerts").capture.input.state, "captured")
        self.assertNotEqual(_span(input_scenario.left, "alerts").input, _span(input_scenario.right, "alerts").input)
        self.assertEqual(input_scenario.meaningful_divergence, "supported")

    def test_capture_disabled_and_redacted_content_are_not_confident(self) -> None:
        disabled = self.by_name["capture_disabled_one_side"]
        self.assertEqual(_span(disabled.left, "alerts").capture.output.state, "captured")
        self.assertEqual(_span(disabled.right, "alerts").capture.output.state, "not_captured")
        self.assertEqual(disabled.uncertainty_barrier, "capture_unavailable")
        self.assertEqual(disabled.allowed_outcomes, ("ALLOWED_UNCERTAIN",))

        redacted = self.by_name["redacted_content"]
        self.assertTrue(_span(redacted.left, "alerts").capture.input.redacted)
        self.assertEqual(redacted.uncertainty_barrier, "redacted_observation")
        self.assertEqual(redacted.allowed_outcomes, ("ALLOWED_UNCERTAIN",))

    def test_error_and_status_scenarios_keep_observation_roles_distinct(self) -> None:
        new_error = self.by_name["new_error_exact_span"]
        self.assertEqual(_span(new_error.right, "alerts").status, "error")
        self.assertEqual(new_error.meaningful_divergence, "supported")

        resolved = self.by_name["resolved_error_exact_span"]
        self.assertEqual(_span(resolved.left, "alerts").status, "error")
        self.assertEqual(_span(resolved.right, "alerts").status, "ok")

        status_only = self.by_name["trace_status_only_change"]
        self.assertEqual(status_only.left.trace.status, "ok")
        self.assertEqual(status_only.right.trace.status, "error")
        self.assertEqual(status_only.meaningful_divergence, "none")

    def test_unique_structure_and_subtree_scenarios_are_complete(self) -> None:
        for name in (
            "unique_tool_added",
            "unique_tool_removed",
            "execution_subtree_added",
            "execution_subtree_removed",
        ):
            with self.subTest(scenario=name):
                scenario = self.by_name[name]
                self.assertTrue(scenario.left_complete)
                self.assertTrue(scenario.right_complete)
                self.assertEqual(scenario.meaningful_divergence, "supported")

        added = self.by_name["unique_tool_added"]
        added_tools = [
            span
            for span_id, label in added.right.labels.items()
            if label == "customer-tool"
            for span in added.right.spans
            if span.span_id == span_id
        ]
        self.assertEqual(len(added_tools), 1)
        self.assertEqual((added_tools[0].operation, added_tools[0].name), ("tool.call", "Lookup customer"))
        self.assertEqual(
            sum(span.name == "Lookup customer" for span in added.right.spans),
            1,
        )

        removed = self.by_name["execution_subtree_removed"]
        self.assertNotIn("plan", removed.right.labels.values())
        self.assertNotIn("lookup-1", removed.right.labels.values())
        self.assertNotIn("normalize-1", removed.right.labels.values())
        right_ids = {span.span_id for span in removed.right.spans}
        self.assertTrue(
            all(span.parent_span_id is None or span.parent_span_id in right_ids for span in removed.right.spans)
        )

    def test_repeated_siblings_prohibit_ordinal_confidence(self) -> None:
        insertion = self.by_name["repeated_tool_insertion"]
        insertion_report = align_traces(
            insertion.left.trace,
            insertion.left.spans,
            insertion.right.trace,
            insertion.right.spans,
        )
        self.assertGreater(insertion_report.metrics.right_only, 0)
        self.assertIn("span:Lookup weather[1]", insertion.forbidden_confident_candidates)

        removal = self.by_name["repeated_tool_removal"]
        removal_report = align_traces(
            removal.left.trace,
            removal.left.spans,
            removal.right.trace,
            removal.right.spans,
        )
        self.assertGreater(removal_report.metrics.left_only, 0)

        reorder = self.by_name["repeated_tool_reordering"]
        self.assertEqual(reorder.uncertainty_barrier, "repeated_sibling_ambiguity")
        self.assertEqual(len(reorder.forbidden_confident_candidates), 3)
        self.assertTrue(
            all(candidate.startswith("span:Lookup weather") for candidate in reorder.forbidden_confident_candidates)
        )

        nested = self.by_name["nested_repeated_groups"]
        self.assertEqual(nested.uncertainty_barrier, "repeated_sibling_ambiguity")
        self.assertEqual(nested.investigation_starting_point, "uncertain")
        nested_report = align_traces(
            nested.left.trace,
            nested.left.spans,
            nested.right.trace,
            nested.right.spans,
        )
        self.assertEqual(nested_report.metrics.right_only, 1)
        self.assertTrue(
            any(
                item["alignment"] == "right_only"
                and len(item["semantic_path"]) == 3
                for item in nested_report.spans
            )
        )
        self.assertTrue(
            any(
                item["alignment"] == "matched"
                and item["semantic_path"][-1]["name"] == "Lookup alerts"
                for item in nested_report.spans
            )
        )
        self.assertTrue(
            any(
                item["alignment"] == "matched"
                and item["semantic_path"][-1]["name"] == "Synthesize answer"
                for item in nested_report.spans
            )
        )

    def test_partial_missing_parent_and_invalid_structure_are_barriers(self) -> None:
        partial = self.by_name["early_termination_partial_trace"]
        self.assertFalse(partial.right_complete)
        self.assertIsNone(partial.right.trace.ended_at)
        self.assertEqual(partial.right.trace.status, "unset")
        self.assertEqual(partial.uncertainty_barrier, "incomplete_trace")
        self.assertEqual(partial.meaningful_divergence, "uncertain")

        missing = self.by_name["missing_parent"]
        missing_report = align_traces(
            missing.left.trace,
            missing.left.spans,
            missing.right.trace,
            missing.right.spans,
        )
        self.assertGreater(missing_report.metrics.unavailable, 0)
        self.assertEqual(missing.uncertainty_barrier, "missing_parent")
        self.assertEqual(missing.meaningful_divergence, "supported")
        self.assertEqual(missing.investigation_starting_point, "uncertain")
        self.assertEqual(missing.expected_starting_point_path, None)
        self.assertEqual(missing.allowed_candidate_paths, ("span:Lookup customer",))

        invalid = self.by_name["duplicate_structural_id_invalid_structure"]
        invalid_report = align_traces(
            invalid.left.trace,
            invalid.left.spans,
            invalid.right.trace,
            invalid.right.spans,
        )
        self.assertGreater(invalid_report.metrics.unavailable, 0)
        self.assertEqual(invalid.uncertainty_barrier, "invalid_structure")

    def test_failure_and_triage_order_scenarios_are_explicit(self) -> None:
        identical = self.by_name["both_runs_fail_identically"]
        self.assertEqual(identical.meaningful_divergence, "none")

        different = self.by_name["both_runs_fail_differently"]
        self.assertEqual(different.meaningful_divergence, "supported")
        self.assertEqual(len(different.downstream_observations), 2)

        earlier_error = self.by_name["error_before_later_structural_difference"]
        self.assertEqual(earlier_error.expected_starting_point_path, "span:Plan/status")
        self.assertNotEqual(earlier_error.expected_starting_point_path, "span:Lookup customer")

        multiple = self.by_name["multiple_independent_divergences"]
        self.assertEqual(multiple.expected_starting_point_path, "span:Plan/status")
        self.assertEqual(multiple.allowed_candidate_paths, ("span:Plan/status",))

        chronology = self.by_name["chronological_vs_lexicographic_order"]
        self.assertEqual(chronology.expected_starting_point_path, "span:Plan/status")
        plan_time = _span(chronology.left, "plan").started_at
        alerts_time = _span(chronology.left, "alerts").started_at
        self.assertGreater(plan_time, alerts_time)
        self.assertIn("structurally earlier", chronology.notes)

    def test_serialized_oracle_is_deterministic(self) -> None:
        self.assertEqual(
            serialize_corpus(build_evaluation_corpus()),
            serialize_corpus(build_evaluation_corpus()),
        )

    def test_false_confidence_counts_are_separate(self) -> None:
        safe_outcomes = [
            ProductionOutcome(
                scenario.name,
                meaningful_confident=False,
                starting_point_confident=False,
                candidate_path=None,
            )
            for scenario in self.scenarios
        ]
        safe = count_false_confidence(self.scenarios, safe_outcomes)
        self.assertEqual(safe.meaningful_divergence, 0)
        self.assertEqual(safe.investigation_starting_point, 0)
        self.assertEqual(safe.expected_confident_meaningful, 15)
        self.assertEqual(safe.correctly_confident_meaningful, 0)
        self.assertEqual(safe.expected_uncertain_meaningful, 6)
        self.assertEqual(safe.safely_withheld_meaningful, 6)
        self.assertLess(safe.correctly_confident_meaningful, safe.expected_confident_meaningful)

        good_outcomes = [
            ProductionOutcome(
                scenario.name,
                meaningful_confident=scenario.meaningful_divergence == "supported",
                starting_point_confident=scenario.investigation_starting_point == "supported",
                candidate_path=(
                    scenario.expected_starting_point_path
                    or (scenario.allowed_candidate_paths[0] if scenario.allowed_candidate_paths else None)
                ),
            )
            for scenario in self.scenarios
        ]
        good = count_false_confidence(self.scenarios, good_outcomes)
        self.assertEqual(good.meaningful_divergence, 0)
        self.assertEqual(good.investigation_starting_point, 0)
        self.assertEqual(good.correctly_confident_meaningful, good.expected_confident_meaningful)
        self.assertEqual(good.correctly_confident_starting_point, good.expected_confident_starting_point)
        self.assertEqual(good.safely_withheld_meaningful, good.expected_uncertain_meaningful)
        self.assertEqual(good.safely_withheld_starting_point, good.expected_uncertain_starting_point)

        later_candidate = ProductionOutcome(
            "multiple_independent_divergences",
            meaningful_confident=True,
            starting_point_confident=True,
            candidate_path="span:Lookup alerts/output",
        )
        later = count_false_confidence(
            self.scenarios,
            [
                ProductionOutcome(
                    scenario.name,
                    meaningful_confident=False,
                    starting_point_confident=False,
                    candidate_path=None,
                )
                for scenario in self.scenarios
                if scenario.name != later_candidate.scenario_name
            ]
            + [later_candidate],
        )
        self.assertEqual(later.meaningful_divergence, 1)
        self.assertEqual(later.investigation_starting_point, 1)

        bad_outcomes = [
            ProductionOutcome(
                scenario.name,
                meaningful_confident=False,
                starting_point_confident=False,
                candidate_path=None,
            )
            for scenario in self.scenarios
            if scenario.name != "capture_disabled_one_side"
        ] + [
            ProductionOutcome(
                "capture_disabled_one_side",
                meaningful_confident=True,
                starting_point_confident=True,
                candidate_path="span:Lookup alerts/output",
            )
        ]
        bad = count_false_confidence(self.scenarios, bad_outcomes)
        self.assertEqual(bad.meaningful_divergence, 1)
        self.assertEqual(bad.investigation_starting_point, 1)

    def test_v05_07_oracle_honesty_is_derived_from_individual_scenarios(self) -> None:
        meaningful = {state: 0 for state in DIVERGENCE_STATES}
        starting = {state: 0 for state in STARTING_POINT_STATES}
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                self.assertIn(scenario.name, MANDATORY_SCENARIOS)
                meaningful[scenario.meaningful_divergence] += 1
                starting[scenario.investigation_starting_point] += 1
                if scenario.meaningful_divergence != "supported":
                    self.assertNotEqual(scenario.investigation_starting_point, "supported")
                if scenario.name in {
                    "identical_runs",
                    "model_only_change",
                    "request_parameter_only_change",
                    "trace_status_only_change",
                    "framework_metadata_only_difference",
                }:
                    self.assertEqual(scenario.meaningful_divergence, "none")
                    self.assertEqual(scenario.investigation_starting_point, "none")
                if scenario.uncertainty_barrier == "repeated_sibling_ambiguity":
                    self.assertEqual(scenario.meaningful_divergence, "uncertain")
                    self.assertEqual(scenario.investigation_starting_point, "uncertain")

        self.assertEqual(len(self.scenarios), 30)
        self.assertEqual(set(MANDATORY_SCENARIOS), {scenario.name for scenario in self.scenarios})
        self.assertEqual(meaningful, {"supported": 15, "uncertain": 6, "none": 9})
        self.assertEqual(starting, {"supported": 14, "uncertain": 7, "none": 9})
        self.assertLess(starting["supported"], meaningful["supported"])

    def test_supported_early_divergence_does_not_claim_later_outcome_or_recovery(self) -> None:
        scenario = self.by_name["error_before_later_structural_difference"]
        self.assertEqual(scenario.meaningful_divergence, "supported")
        self.assertEqual(scenario.investigation_starting_point, "supported")
        notes = scenario.notes.casefold()
        for token in (
            "cause",
            "root cause",
            "recovered",
            "reconverged",
            "harmless",
            "downstream",
            "led to",
        ):
            self.assertNotIn(token, notes)


if __name__ == "__main__":
    unittest.main()
