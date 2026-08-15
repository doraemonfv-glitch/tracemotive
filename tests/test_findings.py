from __future__ import annotations

from dataclasses import replace
import unittest

from tests.divergence_evaluation import build_evaluation_corpus, capture_public_baseline
from tests.test_divergence import _stats
from tracemotive._evaluation.divergence import (
    _captured_input,
    _captured_output,
    _model_change,
    _parameter_change,
    _redacted_input,
    _tool_signature,
    clone_run,
)
from tracemotive.canonical import CaptureInfo, Error
from tracemotive.divergence import analyze_divergence
from tracemotive.findings import (
    assert_compatible_with_v03_11,
    collect_findings,
)
from tracemotive.storage import TraceQueryRecord


def _record(run) -> TraceQueryRecord:
    return TraceQueryRecord(run.trace, _stats(run))


def _collect(scenario):
    return collect_findings(
        _record(scenario.left),
        scenario.left.spans,
        _record(scenario.right),
        scenario.right.spans,
    )


class DiagnosticFindingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_public_baseline()
        cls.scenarios = build_evaluation_corpus()
        cls.by_name = {scenario.name: scenario for scenario in cls.scenarios}

    def test_required_24_scenario_matrix_has_only_supported_findings(self) -> None:
        expected_types = {
            "aligned_tool_output_change": {"tool_output_changed"},
            "aligned_tool_input_change": {"tool_input_changed"},
            "new_error_exact_span": {"new_error"},
            "resolved_error_exact_span": {"resolved_error"},
            "unique_tool_added": {"tool_added"},
            "unique_tool_removed": {"tool_removed"},
            "execution_subtree_added": {"execution_subtree_added"},
            "execution_subtree_removed": {
                "execution_subtree_removed",
                "tool_repetition_changed",
            },
            "repeated_tool_insertion": {"tool_repetition_changed"},
            "repeated_tool_removal": {"tool_repetition_changed"},
        }
        no_behavior = {
            "identical_runs",
            "timing_only_variation",
            "token_only_variation",
            "model_only_change",
            "request_parameter_only_change",
            "trace_status_only_change",
            "framework_metadata_only_difference",
            "large_irrelevant_metadata_difference",
            "capture_disabled_one_side",
            "redacted_content",
            "repeated_tool_reordering",
            "nested_repeated_groups",
            "early_termination_partial_trace",
            "duplicate_structural_id_invalid_structure",
        }
        matrix = tuple(expected_types) + tuple(no_behavior)
        self.assertEqual(len(matrix), 24)
        for name in matrix:
            with self.subTest(scenario=name):
                result = _collect(self.by_name[name])
                behavioral = {
                    finding.type
                    for finding in result.findings
                    if finding.scope == "behavioral"
                }
                self.assertEqual(behavioral, expected_types.get(name, set()))

    def test_all_30_scenarios_preserve_v03_11_behavioral_boundary(self) -> None:
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                result = _collect(scenario)
                divergence = analyze_divergence(
                    _record(scenario.left),
                    scenario.left.spans,
                    _record(scenario.right),
                    scenario.right.spans,
                )
                assert_compatible_with_v03_11(divergence.state, result)
                self.assertEqual(
                    sorted(str(item.to_dict()) for item in result.uncertainties),
                    sorted(str(item.to_dict()) for item in divergence.barriers),
                )
                if scenario.meaningful_divergence == "none":
                    self.assertFalse(any(f.scope == "behavioral" for f in result.findings))
                if scenario.meaningful_divergence == "supported":
                    self.assertTrue(any(f.scope == "behavioral" for f in result.findings))

    def test_finding_shape_ids_and_order_are_deterministic(self) -> None:
        scenario = self.by_name["multiple_independent_divergences"]
        first = _collect(scenario)
        second = _collect(scenario)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(
            [finding.finding_id for finding in first.findings],
            [f"finding-{index:04d}" for index in range(1, len(first.findings) + 1)],
        )
        for finding in first.findings:
            wire = finding.to_dict()
            self.assertEqual(
                set(wire),
                {
                    "finding_id",
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
                },
            )
            self.assertNotIn("started_at", str(finding.to_dict()))
            self.assertNotIn("ended_at", str(finding.to_dict()))

    def test_context_only_findings_never_become_behavioral(self) -> None:
        expected = {
            "model_only_change": "model_changed",
            "request_parameter_only_change": "request_parameters_changed",
            "trace_status_only_change": "trace_status_changed",
        }
        for name, finding_type in expected.items():
            with self.subTest(scenario=name):
                result = _collect(self.by_name[name])
                self.assertEqual([f.type for f in result.findings], [finding_type])
                self.assertEqual(result.findings[0].scope, "context_only")
                self.assertFalse(any(f.scope == "behavioral" for f in result.findings))

        def all_context_changes(span):
            span = _model_change("offline-model-b")(span)
            return _parameter_change({"temperature": 0.7})(span)

        right = clone_run(
            self.base,
            "all-context-only",
            status="error",
            span_mutators={"plan": all_context_changes},
        )
        result = collect_findings(_record(self.base), self.base.spans, _record(right), right.spans)
        self.assertEqual(
            {finding.type for finding in result.findings},
            {"model_changed", "request_parameters_changed", "trace_status_changed"},
        )
        self.assertFalse(any(finding.scope == "behavioral" for finding in result.findings))

    def test_context_findings_can_coexist_with_behavioral_observations(self) -> None:
        def model_change(span):
            return replace(span, details=replace(span.details, request_model="offline-model-b"))

        def output_change(span):
            return replace(
                span,
                output={"alerts": ["notice"]},
                capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
            )

        def output_baseline(span):
            return replace(
                span,
                output={"alerts": []},
                capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
            )

        left = clone_run(
            self.base,
            "context-behavior-left",
            span_mutators={"alerts": output_baseline},
        )
        right = clone_run(
            self.base,
            "context-behavior-right",
            span_mutators={"plan": model_change, "alerts": output_change},
        )
        result = collect_findings(_record(left), left.spans, _record(right), right.spans)
        self.assertEqual(
            {finding.type for finding in result.findings},
            {"model_changed", "tool_output_changed"},
        )
        self.assertEqual(
            {finding.scope for finding in result.findings},
            {"context_only", "behavioral"},
        )

    def test_error_findings_include_status_and_error_evidence(self) -> None:
        for name, finding_type in (
            ("new_error_exact_span", "new_error"),
            ("resolved_error_exact_span", "resolved_error"),
        ):
            with self.subTest(scenario=name):
                result = _collect(self.by_name[name])
                finding = next(item for item in result.findings if item.type == finding_type)
                paths = {item.get("path") for item in finding.evidence}
                self.assertIn("/status", paths)
                self.assertIn("/error/type", paths)
                self.assertIn("/error/message", paths)

        right = clone_run(
            self.base,
            "unique-error-right",
            order=(*self.base.order, "customer-tool"),
            extras=(("customer-tool", "alerts", "agent-root"),),
            span_mutators={"customer-tool": _tool_signature("Lookup customer", "lookup_customer")},
            error_labels=("customer-tool",),
        )
        result = collect_findings(_record(self.base), self.base.spans, _record(right), right.spans)
        finding = next(item for item in result.findings if item.type == "new_error")
        self.assertEqual(
            {item.get("path") for item in finding.evidence},
            {"/status", "/error/type", "/error/message"},
        )

    def test_error_payload_changes_do_not_invent_lifecycle_findings(self) -> None:
        def changed_error(span):
            return replace(span, error=Error("OtherError", "different observed message"))

        same_status_left = clone_run(
            self.base,
            "same-error-left",
            error_labels=("alerts",),
        )
        same_status_right = clone_run(
            self.base,
            "same-error-right",
            error_labels=("alerts",),
            span_mutators={"alerts": changed_error},
        )
        result = collect_findings(
            _record(same_status_left),
            same_status_left.spans,
            _record(same_status_right),
            same_status_right.spans,
        )
        self.assertFalse(any(item.type in {"new_error", "resolved_error"} for item in result.findings))

        def missing_error_payload(span):
            return replace(span, error=None)

        malformed_right = clone_run(
            self.base,
            "error-status-without-payload",
            error_labels=("alerts",),
            span_mutators={"alerts": missing_error_payload},
        )
        result = collect_findings(
            _record(same_status_left),
            same_status_left.spans,
            _record(malformed_right),
            malformed_right.spans,
        )
        self.assertFalse(any(item.type in {"new_error", "resolved_error"} for item in result.findings))

        def incomplete_span(span):
            return replace(
                span,
                ended_at=None,
                status="unset",
                error=None,
                capture=replace(span.capture, output=CaptureInfo("not_captured", "not_yet_available", False)),
            )

        incomplete_right = clone_run(
            self.base,
            "incomplete-error-right",
            incomplete=True,
            span_mutators={"alerts": incomplete_span},
        )
        result = collect_findings(
            _record(same_status_left),
            same_status_left.spans,
            _record(incomplete_right),
            incomplete_right.spans,
        )
        self.assertFalse(any(item.type in {"new_error", "resolved_error"} for item in result.findings))

    def test_unique_structure_is_root_deduplicated(self) -> None:
        for name, expected_type in (
            ("unique_tool_added", "tool_added"),
            ("unique_tool_removed", "tool_removed"),
            ("execution_subtree_added", "execution_subtree_added"),
            ("execution_subtree_removed", "execution_subtree_removed"),
        ):
            with self.subTest(scenario=name):
                result = _collect(self.by_name[name])
                types = [finding.type for finding in result.findings]
                self.assertIn(expected_type, types)
                self.assertEqual(
                    sum(item == expected_type for item in types),
                    1,
                )
                if expected_type.startswith("execution_subtree"):
                    self.assertEqual(
                        sum(item.startswith("execution_subtree") for item in types),
                        1,
                    )

        nested_added = clone_run(
            self.base,
            "nested-side-only-subtree",
            order=(
                *self.base.order,
                "customer-route",
                "nested-tool-1",
                "nested-tool-2",
                "nested-tool-3",
            ),
            extras=(
                ("customer-route", "normalize-alerts", "agent-root"),
                ("nested-tool-1", "lookup-1", "customer-route"),
                ("nested-tool-2", "lookup-1", "customer-route"),
                ("nested-tool-3", "lookup-1", "customer-route"),
            ),
            span_mutators={
                "nested-tool-1": _tool_signature("Nested lookup", "nested_lookup"),
                "nested-tool-2": _tool_signature("Nested lookup", "nested_lookup"),
                "nested-tool-3": _tool_signature("Nested lookup", "nested_lookup"),
            },
        )
        result = collect_findings(
            _record(self.base),
            self.base.spans,
            _record(nested_added),
            nested_added.spans,
        )
        self.assertEqual([finding.type for finding in result.findings], ["execution_subtree_added"])

        removed = _collect(self.by_name["execution_subtree_removed"])
        self.assertEqual(
            [finding.type for finding in removed.findings],
            ["execution_subtree_removed", "tool_repetition_changed"],
        )

    def test_repetition_reports_counts_without_member_identity(self) -> None:
        for name, left_count, right_count in (
            ("repeated_tool_insertion", 3, 4),
            ("repeated_tool_removal", 3, 2),
        ):
            with self.subTest(scenario=name):
                result = _collect(self.by_name[name])
                self.assertEqual(len(result.findings), 1)
                finding = result.findings[0]
                self.assertEqual(finding.type, "tool_repetition_changed")
                self.assertIsNone(finding.left)
                self.assertIsNone(finding.right)
                self.assertEqual(finding.observed["left"]["value"], left_count)
                self.assertEqual(finding.observed["right"]["value"], right_count)

    def test_uncertainty_barriers_suppress_unsupported_findings(self) -> None:
        expected_barriers = {
            "capture_disabled_one_side": "capture_unavailable",
            "redacted_content": "redacted_observation",
            "repeated_tool_reordering": "repeated_sibling_ambiguity",
            "early_termination_partial_trace": "incomplete_trace",
            "duplicate_structural_id_invalid_structure": "invalid_structure",
        }
        for name, barrier_reason in expected_barriers.items():
            with self.subTest(scenario=name):
                result = _collect(self.by_name[name])
                self.assertFalse(any(f.scope == "behavioral" for f in result.findings))
                self.assertIn(barrier_reason, {barrier.reason_code for barrier in result.uncertainties})

    def test_context_metadata_differences_do_not_create_findings(self) -> None:
        for name in (
            "timing_only_variation",
            "token_only_variation",
            "framework_metadata_only_difference",
            "large_irrelevant_metadata_difference",
        ):
            with self.subTest(scenario=name):
                self.assertEqual(_collect(self.by_name[name]).findings, ())

    def test_eight_new_composite_cases_match_predictions(self) -> None:
        def customer_tool_run(base, name, *, span_mutators=None, error_labels=()):
            return clone_run(
                base,
                name,
                order=(*base.order, "customer-tool"),
                extras=(("customer-tool", "alerts", "agent-root"),),
                span_mutators={
                    "customer-tool": _tool_signature("Lookup customer", "lookup_customer"),
                    **(span_mutators or {}),
                },
                error_labels=error_labels,
            )

        def nested_subtree_run(name):
            return clone_run(
                self.base,
                name,
                order=(
                    *self.base.order,
                    "customer-route",
                    "nested-tool-1",
                    "nested-tool-2",
                    "nested-tool-3",
                ),
                extras=(
                    ("customer-route", "normalize-alerts", "agent-root"),
                    ("nested-tool-1", "lookup-1", "customer-route"),
                    ("nested-tool-2", "lookup-1", "customer-route"),
                    ("nested-tool-3", "lookup-1", "customer-route"),
                ),
                span_mutators={
                    "nested-tool-1": _tool_signature("Nested lookup", "nested_lookup"),
                    "nested-tool-2": _tool_signature("Nested lookup", "nested_lookup"),
                    "nested-tool-3": _tool_signature("Nested lookup", "nested_lookup"),
                },
            )

        def combined_context(span):
            span = _model_change("offline-model-b")(span)
            return _parameter_change({"temperature": 0.7})(span)

        left_1 = clone_run(
            self.base,
            "composite-1-left",
            span_mutators={"alerts": _captured_output({"alerts": []})},
        )
        right_1 = clone_run(
            self.base,
            "composite-1-right",
            span_mutators={
                "alerts": _captured_output({"alerts": ["notice"]}),
                "plan": _model_change("offline-model-b"),
            },
        )
        status_only = self.by_name["trace_status_only_change"]
        left_3 = self.base
        right_3 = customer_tool_run(
            self.base,
            "composite-3-right",
            error_labels=("alerts",),
        )
        left_4 = self.base
        right_4 = nested_subtree_run("composite-4-right")
        left_5 = clone_run(
            self.base,
            "composite-5-left",
            span_mutators={"alerts": _redacted_input("secret-left")},
        )
        right_5 = customer_tool_run(
            self.base,
            "composite-5-right",
            span_mutators={"alerts": _captured_input({"result": "safe"})},
        )
        left_6 = self.base
        right_6 = clone_run(
            self.base,
            "composite-6-right",
            incomplete=True,
            span_mutators={"plan": _model_change("offline-model-b")},
        )
        left_7 = clone_run(
            self.base,
            "composite-7-left",
            span_mutators={"alerts": _captured_output({"alerts": []})},
        )
        right_7 = clone_run(
            self.base,
            "composite-7-right",
            status="error",
            span_mutators={
                "alerts": _captured_output({"alerts": ["notice"]}),
                "plan": combined_context,
            },
        )
        repeat = self.by_name["repeated_tool_insertion"]
        left_8 = repeat.left
        right_8 = clone_run(repeat.right, "composite-8-right", error_labels=("plan",))

        cases = (
            ("model+output", left_1, right_1, {"model_changed", "tool_output_changed"}, set()),
            ("trace-status-only", status_only.left, status_only.right, {"trace_status_changed"}, set()),
            ("error+tool-added", left_3, right_3, {"new_error", "tool_added"}, set()),
            ("subtree+repeated-descendants", left_4, right_4, {"execution_subtree_added"}, set()),
            ("redacted+later-tool", left_5, right_5, {"tool_added"}, {"redacted_observation"}),
            ("incomplete+model", left_6, right_6, {"model_changed"}, {"incomplete_trace"}),
            (
                "three-context+output",
                left_7,
                right_7,
                {"model_changed", "request_parameters_changed", "trace_status_changed", "tool_output_changed"},
                set(),
            ),
            ("repetition+error", left_8, right_8, {"tool_repetition_changed", "new_error"}, set()),
        )
        for name, left, right, expected_findings, expected_barriers in cases:
            with self.subTest(case=name):
                result = collect_findings(_record(left), left.spans, _record(right), right.spans)
                self.assertEqual({item.type for item in result.findings}, expected_findings)
                self.assertTrue(expected_barriers.issubset({item.reason_code for item in result.uncertainties}))

    def test_semantic_finding_order_is_stable_across_ids_times_and_persistence_order(self) -> None:
        mutations_left = {"alerts": _captured_output({"alerts": []})}
        mutations_right = {"alerts": _captured_output({"alerts": ["notice"]})}

        def pair(prefix, order, offset):
            left = clone_run(
                self.base,
                f"{prefix}-left",
                order=order,
                timing_offset_us=offset,
                span_mutators=mutations_left,
            )
            right = clone_run(
                self.base,
                f"{prefix}-right",
                order=order,
                timing_offset_us=offset + 500_000,
                span_mutators=mutations_right,
                error_labels=("plan",),
            )
            return collect_findings(_record(left), left.spans, _record(right), right.spans)

        first = pair("stable-a", self.base.order, 0)
        second = pair("stable-b", tuple(reversed(self.base.order)), 9_000_000)

        def semantic_shape(result):
            return [
                (item.type, item.coordinate.to_dict(), item.field_path, item.reason_code)
                for item in result.findings
            ]

        self.assertEqual(semantic_shape(first), semantic_shape(second))
        self.assertEqual(
            [item.finding_id for item in first.findings],
            [item.finding_id for item in second.findings],
        )


if __name__ == "__main__":
    unittest.main()
