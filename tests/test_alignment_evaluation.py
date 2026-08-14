from __future__ import annotations

from dataclasses import replace
import unittest

from tests.alignment_evaluation import capture_realistic_run, clone_run, evaluate_corpus
from tracemotive._evaluation.alignment import align_traces


class AlignmentEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_realistic_run()
        cls.results = {result.name: result for result in evaluate_corpus()}

    def test_corpus_uses_realistic_public_capture_shape(self) -> None:
        self.assertEqual(len(self.base.spans), 11)
        self.assertEqual(
            [span.type for span in self.base.spans].count("agent"),
            1,
        )
        self.assertEqual(
            [span.type for span in self.base.spans].count("llm"),
            2,
        )
        self.assertEqual(
            [span.type for span in self.base.spans].count("tool"),
            4,
        )
        self.assertTrue(all(span.input is None for span in self.base.spans))
        self.assertTrue(all(span.output is None for span in self.base.spans))

    def test_required_scenarios_are_evaluated(self) -> None:
        self.assertEqual(
            set(self.results),
            {
                "near_identical_rerun",
                "repeated_tool_calls",
                "added_tool_call",
                "removed_tool_call",
                "reordered_siblings",
                "timing_variation",
                "partial_early_error",
                "missing_parent",
                "fan_out_fan_in_like",
                "duplicate_structural_sibling",
            },
        )

    def test_stable_structure_and_timing_match_without_native_ids(self) -> None:
        for name in ("near_identical_rerun", "repeated_tool_calls", "timing_variation", "fan_out_fan_in_like"):
            with self.subTest(name=name):
                result = self.results[name]
                self.assertEqual(result.report.metrics.matched, 11)
                self.assertEqual(result.report.metrics.match_coverage, 1.0)
                self.assertEqual(result.incorrect_pairs, 0)

    def test_insertion_removal_and_reorder_expose_ordinal_limits(self) -> None:
        added = self.results["added_tool_call"]
        self.assertEqual(added.report.metrics.right_only, 2)
        self.assertEqual(added.repeated_shift["later_repeated_shifted"], 2)  # type: ignore[index]
        self.assertGreater(added.incorrect_pairs, 0)

        removed = self.results["removed_tool_call"]
        self.assertEqual(removed.report.metrics.left_only, 2)
        self.assertEqual(removed.repeated_shift["later_repeated_unmatched"], 1)  # type: ignore[index]
        self.assertGreater(removed.incorrect_pairs, 0)

        reordered = self.results["reordered_siblings"]
        self.assertGreater(reordered.incorrect_pairs, 0)
        self.assertEqual(reordered.report.metrics.ambiguous_groups, 0)

    def test_ambiguity_and_unavailable_are_localized(self) -> None:
        missing = self.results["missing_parent"]
        self.assertEqual(missing.report.metrics.unavailable, 1)
        self.assertEqual(missing.branch_local_correct, missing.branch_local_expected)
        self.assertGreater(missing.report.metrics.matched, 0)

        duplicate = self.results["duplicate_structural_sibling"]
        self.assertEqual(duplicate.report.metrics.ambiguous_groups, 1)
        self.assertGreater(duplicate.report.metrics.matched, 0)
        self.assertGreater(duplicate.report.metrics.left_only, 0)

    def test_partial_error_is_structural_not_causal(self) -> None:
        result = self.results["partial_early_error"]
        self.assertEqual(result.report.metrics.left_only, 5)
        self.assertEqual(result.incorrect_pairs, 0)
        for item in result.report.spans:
            self.assertNotIn("first", item)
            self.assertNotIn("cause", item)

    def test_zero_span_coverage_is_one(self) -> None:
        right_trace = replace(self.base.trace, trace_id="f" * 32)
        report = align_traces(self.base.trace, (), right_trace, ())
        self.assertEqual(report.metrics.match_coverage, 1.0)
        self.assertEqual(report.metrics.matched, 0)

    def test_result_order_is_independent_of_input_span_order(self) -> None:
        right = clone_run(self.base, "input-order")
        forward = align_traces(self.base.trace, self.base.spans, right.trace, right.spans)
        reversed_right = align_traces(self.base.trace, self.base.spans, right.trace, tuple(reversed(right.spans)))
        self.assertEqual(
            [item["semantic_path"] for item in forward.spans],
            [item["semantic_path"] for item in reversed_right.spans],
        )
        self.assertEqual(
            [item["alignment"] for item in forward.spans],
            [item["alignment"] for item in reversed_right.spans],
        )


if __name__ == "__main__":
    unittest.main()
