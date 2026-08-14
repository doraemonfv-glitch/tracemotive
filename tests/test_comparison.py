from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
import unittest

from tests.alignment_evaluation import EvaluationRun, capture_realistic_run, clone_run
from tests.test_query_api import request
from tracemotive.canonical import Capture, CaptureInfo
from tracemotive.comparison import (
    MAX_COMPARISON_SPANS,
    ComparisonTooLargeError,
    compare_trace_inputs,
)
from tracemotive.collector import create_app
from tracemotive.storage import Repository, TraceQueryRecord


REPEATED_LABELS = tuple(
    [f"lookup-{index}" for index in range(1, 4)]
    + [f"normalize-{index}" for index in range(1, 4)]
)


def _span(run: EvaluationRun, label: str):
    return next(span for span in run.spans if run.labels.get(span.span_id) == label)


def _replace_span(run: EvaluationRun, label: str, replacement) -> EvaluationRun:
    target = _span(run, label)
    return replace(
        run,
        spans=tuple(replacement if span.span_id == target.span_id else span for span in run.spans),
    )


def _persist(repository: Repository, run: EvaluationRun) -> None:
    repository.upsert_trace(run.trace)
    for span in run.spans:
        repository.upsert_span(span)


def _added_tool_run(base: EvaluationRun) -> EvaluationRun:
    return clone_run(
        base,
        "production-added-tool",
        order=(
            "agent-root",
            "plan",
            "lookup-1",
            "normalize-1",
            "lookup-extra",
            "normalize-extra",
            "lookup-2",
            "normalize-2",
            "lookup-3",
            "normalize-3",
            "alerts",
            "normalize-alerts",
            "synthesis",
        ),
        extras=(
            ("lookup-extra", "lookup-2", "agent-root"),
            ("normalize-extra", "normalize-2", "lookup-extra"),
        ),
    )


def _reordered_run(base: EvaluationRun) -> EvaluationRun:
    return clone_run(
        base,
        "production-reordered",
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
    )


class ComparisonAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_realistic_run()

    def _compare_runs(self, left: EvaluationRun, right: EvaluationRun):
        with Repository() as repository:
            _persist(repository, left)
            _persist(repository, right)
            left_input, right_input = repository.get_trace_comparison_inputs(
                left.trace.trace_id,
                right.trace.trace_id,
            )
            assert left_input is not None
            assert right_input is not None
            return compare_trace_inputs(
                left_input.record,
                left_input.spans,
                right_input.record,
                right_input.spans,
            )

    def _api_compare(self, left: EvaluationRun, right: EvaluationRun):
        with Repository() as repository:
            _persist(repository, left)
            _persist(repository, right)
            status, body = request(
                create_app(repository),
                "GET",
                f"/api/v2/compare/{left.trace.trace_id}/{right.trace.trace_id}",
            )
            return status, body, json.loads(body, parse_float=Decimal)

    def test_v02_19_insertion_removal_reorder_have_no_false_exact_pairs(self):
        variants = (
            _added_tool_run(self.base),
            clone_run(self.base, "production-removed-tool", drop=("lookup-2", "normalize-2")),
            _reordered_run(self.base),
        )
        for variant in variants:
            with self.subTest(run=variant.trace.trace_id):
                result = self._compare_runs(self.base, variant)
                exact = [item for item in result["spans"] if item["alignment"] == "exact_match"]
                incorrect = 0
                for item in exact:
                    left_label = self.base.labels.get(item["left"]["span_id"])
                    right_label = variant.labels.get(item["right"]["span_id"])
                    if left_label in REPEATED_LABELS and right_label in REPEATED_LABELS and left_label != right_label:
                        incorrect += 1
                self.assertEqual(incorrect, 0)
                self.assertEqual(result["summary"]["alignment"]["ambiguous_groups"], 1)
                self.assertGreater(result["summary"]["alignment"]["matched_spans"], 0)
                self.assertTrue(
                    all(item["alignment"] != "matched" for item in result["spans"])
                )

    def test_repeated_group_preserves_members_and_local_unavailability(self):
        result = self._compare_runs(self.base, _added_tool_run(self.base))
        group = result["ambiguous_groups"][0]
        self.assertEqual(group["alignment"], "ambiguous_group")
        self.assertEqual(group["group_signature"]["name"], "Lookup weather")
        self.assertEqual((group["left_count"], group["right_count"]), (3, 4))
        self.assertEqual(len(group["ambiguous_members"]["left"]), 3)
        self.assertEqual(len(group["ambiguous_members"]["right"]), 4)
        self.assertEqual(group["resolved_members"], [])
        self.assertIsNone(group["left_only_count"])
        self.assertIsNone(group["right_only_count"])
        self.assertTrue(
            all(record["reason"] == "ambiguous_parent" for record in result["unavailable_spans"])
        )
        self.assertEqual(result["summary"]["alignment"]["matched_spans"], 5)
        exact_labels = {
            self.base.labels[item["left"]["span_id"]]
            for item in result["spans"]
            if item["alignment"] == "exact_match"
        }
        self.assertIn("synthesis", exact_labels)
        self.assertNotIn("lookup-1", exact_labels)

    def test_unique_left_right_and_unavailable_categories_are_distinct(self):
        left_only = clone_run(
            self.base,
            "production-left-only",
            drop=("alerts", "normalize-alerts"),
        )
        result = self._compare_runs(self.base, left_only)
        self.assertIn("left_only", {item["alignment"] for item in result["spans"]})

        right_only = clone_run(
            self.base,
            "production-right-only",
            drop=("alerts", "normalize-alerts"),
            extras=(("extra-custom", "normalize-alerts", "agent-root"),),
        )
        result = self._compare_runs(self.base, right_only)
        self.assertIn("right_only", {item["alignment"] for item in result["spans"]})

        missing_parent = clone_run(
            self.base,
            "production-missing-parent",
            missing_parent_labels=("normalize-alerts",),
        )
        result = self._compare_runs(self.base, missing_parent)
        self.assertIn("unavailable", {item["alignment"] for item in result["unavailable_spans"]})
        self.assertIn("missing_parent", {item["reason"] for item in result["unavailable_spans"]})
        self.assertGreater(result["summary"]["alignment"]["matched_spans"], 0)

    def test_trace_and_span_field_differences_are_lossless_and_structured(self):
        drop = REPEATED_LABELS
        left = clone_run(self.base, "production-fields-left", drop=drop)
        right = clone_run(self.base, "production-fields-right", drop=drop, status="error", error_label="alerts")
        left_alerts = _span(left, "alerts")
        right_alerts = _span(right, "alerts")
        captured = Capture(
            CaptureInfo("captured", None, False),
            CaptureInfo("captured", None, False),
        )
        left = _replace_span(
            left,
            "alerts",
            replace(left_alerts, input={"city": "Tokyo"}, output={"alerts": []}, capture=captured),
        )
        right = _replace_span(
            right,
            "alerts",
            replace(right_alerts, input={"city": "Kyoto"}, output={"alerts": ["rain"]}, capture=captured),
        )
        right_plan = _span(right, "plan")
        right = _replace_span(
            right,
            "plan",
            replace(
                right_plan,
                details=replace(
                    right_plan.details,
                    request_model="different-model",
                    request_parameters={"temperature": 0.7},
                ),
            ),
        )
        result = self._compare_runs(left, right)
        trace_fields = {field["path"]: field for field in result["summary"]["trace_fields"]}
        self.assertEqual(trace_fields["status"]["state"], "different")
        self.assertEqual(trace_fields["error_count"]["state"], "different")
        alert_item = next(
            item
            for item in result["spans"]
            if item["left"] and item["left"]["span_id"] == left_alerts.span_id
        )
        differences = {difference["path"]: difference for difference in alert_item["differences"]}
        self.assertEqual(differences["/input/city"]["state"], "different")
        self.assertEqual(differences["/output/alerts/0"]["state"], "right_only")
        plan_item = next(
            item
            for item in result["spans"]
            if item["left"] and item["left"]["span_id"] == _span(left, "plan").span_id
        )
        plan_paths = {difference["path"] for difference in plan_item["differences"]}
        self.assertIn("/details/request_model", plan_paths)
        self.assertIn("/details/request_parameters/temperature", plan_paths)

    def test_capture_state_is_unknown_not_equal_when_not_captured(self):
        drop = REPEATED_LABELS
        left = clone_run(self.base, "production-capture-left", drop=drop)
        right = clone_run(self.base, "production-capture-right", drop=drop)
        result = self._compare_runs(left, right)
        agent = next(
            item
            for item in result["spans"]
            if item["left"] and item["left"]["span_id"] == _span(left, "agent-root").span_id
        )
        input_uncertainty = next(item for item in agent["uncertainties"] if item["path"] == "/input")
        self.assertEqual(input_uncertainty["state"], "unknown")
        self.assertEqual(input_uncertainty["reason"], "capture_unavailable")

    def test_api_errors_are_explicit_and_safe(self):
        with Repository() as repository:
            app = create_app(repository)
            for path, expected_status in (
                ("/api/v2/compare/not-an-id/00000000000000000000000000000000", 400),
                ("/api/v2/compare/4bf92f3577b34da6a3ce929d0e0e4736/4bf92f3577b34da6a3ce929d0e0e4736", 400),
                ("/api/v2/compare/4bf92f3577b34da6a3ce929d0e0e4736/5bf92f3577b34da6a3ce929d0e0e4736", 404),
            ):
                status, body = request(app, "GET", path)
                self.assertEqual(status, expected_status)
                self.assertNotIn(b"C:\\", body)
                self.assertNotIn(b"secret", body.lower())
            status, body = request(app, "GET", "/api/v1/health")
            self.assertEqual((status, body), (200, b'{"status":"ok"}'))

    def test_api_response_is_deterministic_and_read_only(self):
        left = clone_run(self.base, "production-api-left", drop=REPEATED_LABELS)
        right = clone_run(self.base, "production-api-right", drop=REPEATED_LABELS, timing_offset_us=800_000)
        with Repository() as repository:
            _persist(repository, left)
            _persist(repository, right)
            before_tables = {
                row["name"]
                for row in repository.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            app = create_app(repository)
            path = f"/api/v2/compare/{left.trace.trace_id}/{right.trace.trace_id}"
            first = request(app, "GET", path)
            second = request(app, "GET", path)
            self.assertEqual(first, second)
            self.assertEqual(first[0], 200)
            payload = json.loads(first[1], parse_float=Decimal)
            self.assertEqual(payload["comparison_version"], "0.2")
            self.assertEqual(
                set(payload),
                {
                    "comparison_version",
                    "left_trace",
                    "right_trace",
                    "summary",
                    "spans",
                    "ambiguous_groups",
                    "unavailable_spans",
                },
            )
            after_tables = {
                row["name"]
                for row in repository.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(before_tables, after_tables)
            self.assertEqual(repository.get_trace(left.trace.trace_id), left.trace)

    def test_comparison_span_limit_is_explicit(self):
        left = clone_run(self.base, "production-limit-left", drop=REPEATED_LABELS)
        right = clone_run(self.base, "production-limit-right", drop=REPEATED_LABELS)
        with self.assertRaises(ComparisonTooLargeError):
            compare_trace_inputs(
                TraceQueryRecord(left.trace, self._stats(left)),
                tuple(left.spans) + tuple(left.spans) * MAX_COMPARISON_SPANS,
                TraceQueryRecord(right.trace, self._stats(right)),
                right.spans,
            )

    @staticmethod
    def _stats(run: EvaluationRun):
        from tracemotive.storage import TraceStats

        return TraceStats(
            len(run.spans),
            sum(span.status == "error" for span in run.spans),
            sum(span.type == "llm" for span in run.spans),
            sum(span.details.usage.input_tokens or 0 for span in run.spans if span.type == "llm"),
            sum(span.details.usage.output_tokens or 0 for span in run.spans if span.type == "llm"),
        )


if __name__ == "__main__":
    unittest.main()
