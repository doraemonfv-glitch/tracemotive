from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import patch
import unittest

from tests.divergence_evaluation import build_evaluation_corpus, capture_public_baseline
from tests.test_divergence import _stats
from tests.test_query_api import request
from tracemotive._evaluation.divergence import _captured_output, clone_run
from tracemotive.api_v3 import build_v3_comparison
from tracemotive.comparison import ComparisonTooLargeError, MAX_COMPARISON_SPANS, compare_trace_inputs
from tracemotive.divergence import analyze_divergence
from tracemotive.findings import collect_findings
from tracemotive.investigation import build_investigation_summary
from tracemotive.collector import create_app
from tracemotive.storage import Repository, TraceQueryRecord
import tracemotive
import tracemotive.api_v3 as api_v3


def _persist(repository: Repository, run) -> None:
    repository.upsert_trace(run.trace)
    for span in run.spans:
        repository.upsert_span(span)


def _record(run) -> TraceQueryRecord:
    return TraceQueryRecord(run.trace, _stats(run))


def _coordinate_from_starting_point(value: dict[str, object]) -> dict[str, object]:
    return {
        "kind": value["kind"],
        "semantic_path": value["semantic_path"],
        "group_signature": value["group_signature"],
    }


class V03ComparisonAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_public_baseline()
        cls.scenarios = build_evaluation_corpus()
        cls.by_name = {scenario.name: scenario for scenario in cls.scenarios}

    def _api_compare(self, left, right):
        with Repository() as repository:
            _persist(repository, left)
            _persist(repository, right)
            response = request(
                create_app(repository),
                "GET",
                f"/api/v3/compare/{left.trace.trace_id}/{right.trace.trace_id}",
            )
        return response[0], response[1], json.loads(response[1], parse_float=Decimal)

    def _expected_summary(self, left, right):
        divergence = analyze_divergence(_record(left), left.spans, _record(right), right.spans)
        findings = collect_findings(_record(left), left.spans, _record(right), right.spans)
        return findings, build_investigation_summary(divergence, findings)

    def test_v3_composition_is_not_a_public_sdk_export(self) -> None:
        self.assertFalse(hasattr(tracemotive, "build_v3_comparison"))
        self.assertFalse(hasattr(tracemotive, "V3CompositionError"))
        self.assertFalse(hasattr(api_v3, "__all__"))

    def test_all_30_scenarios_have_exact_state_primary_and_reference_behavior(self) -> None:
        states = {
            "INVESTIGATION_POINT": 0,
            "UNCERTAIN": 0,
            "NO_BEHAVIORAL_DIVERGENCE": 0,
        }
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                if scenario.name == "duplicate_structural_id_invalid_structure":
                    # The v0.1 repository correctly has a unique primary key
                    # for (trace_id, span_id), so this deliberately malformed
                    # corpus input cannot survive a persistence round trip.
                    # Exercise the same v3 serializer directly from its
                    # persisted-read-model boundary for this one corpus case.
                    payload = build_v3_comparison(
                        _record(scenario.left),
                        scenario.left.spans,
                        _record(scenario.right),
                        scenario.right.spans,
                    )
                else:
                    status, _, payload = self._api_compare(scenario.left, scenario.right)
                    self.assertEqual(status, 200)
                self.assertEqual(payload["comparison_version"], "0.3")
                expected_state = (
                    "INVESTIGATION_POINT"
                    if scenario.investigation_starting_point == "supported"
                    else "UNCERTAIN"
                    if scenario.investigation_starting_point == "uncertain"
                    else "UNCERTAIN"
                    if scenario.meaningful_divergence == "uncertain"
                    else "NO_BEHAVIORAL_DIVERGENCE"
                )
                expected_api_state = {
                    "INVESTIGATION_POINT": "identified",
                    "UNCERTAIN": "uncertain",
                    "NO_BEHAVIORAL_DIVERGENCE": "none",
                }[expected_state]
                self.assertEqual(payload["investigation"]["state"], expected_api_state)
                self.assertEqual(
                    payload["investigation"]["first_meaningful_divergence"]["state"],
                    expected_api_state,
                )
                states[expected_state] += 1
                self.assertEqual(payload["summary"]["finding_count"], len(payload["findings"]))
                self.assertEqual(payload["summary"]["uncertainty_count"], len(payload["uncertainties"]))
                self.assertEqual(
                    payload["detail_endpoint"],
                    {
                        "method": "GET",
                        "path": f"/api/v2/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}",
                        "comparison_version": "0.2",
                    },
                )

                finding_ids = {item["finding_id"] for item in payload["findings"]}
                uncertainty_ids = {item["uncertainty_id"] for item in payload["uncertainties"]}
                self.assertEqual(len(finding_ids), len(payload["findings"]))
                self.assertEqual(len(uncertainty_ids), len(payload["uncertainties"]))
                investigation = payload["investigation"]
                self.assertTrue(
                    set(investigation["context_finding_ids"]) <= finding_ids
                )
                self.assertTrue(
                    {item["finding_id"] for item in investigation["evidence_summary"]} <= finding_ids
                )
                self.assertTrue(set(investigation["blocking_uncertainty_ids"]) <= uncertainty_ids)
                self.assertTrue(
                    {item["uncertainty_id"] for item in investigation["limitations"]}
                    <= uncertainty_ids
                )
                if expected_state == "INVESTIGATION_POINT":
                    findings, summary = self._expected_summary(scenario.left, scenario.right)
                    del findings
                    assert summary.primary is not None
                    starting_point = investigation["starting_point"]
                    self.assertIsNotNone(starting_point)
                    assert starting_point is not None
                    self.assertEqual(
                        _coordinate_from_starting_point(starting_point),
                        summary.primary.coordinate.to_dict(),
                    )
                    self.assertEqual(starting_point["finding_id"], summary.primary.finding_ids[0])
                    self.assertIn(starting_point["finding_id"], finding_ids)
                else:
                    self.assertIsNone(investigation["starting_point"])
                    self.assertIsNone(investigation["first_meaningful_divergence"]["finding_id"])
        self.assertEqual(states, {
            "INVESTIGATION_POINT": 14,
            "UNCERTAIN": 7,
            "NO_BEHAVIORAL_DIVERGENCE": 9,
        })

    def test_context_only_scenarios_remain_nonbehavioral_and_keep_their_findings(self) -> None:
        expected = {
            "model_only_change": "model_changed",
            "request_parameter_only_change": "request_parameters_changed",
            "trace_status_only_change": "trace_status_changed",
        }
        for scenario_name, finding_type in expected.items():
            with self.subTest(scenario=scenario_name):
                scenario = self.by_name[scenario_name]
                status, _, payload = self._api_compare(scenario.left, scenario.right)
                self.assertEqual(status, 200)
                self.assertEqual(payload["investigation"]["state"], "none")
                self.assertIsNone(payload["investigation"]["starting_point"])
                self.assertEqual([item["type"] for item in payload["findings"]], [finding_type])
                self.assertEqual(payload["findings"][0]["scope"], "context_only")
                self.assertEqual(
                    payload["investigation"]["context_finding_ids"],
                    [payload["findings"][0]["finding_id"]],
                )
                self.assertFalse(payload["investigation"]["evidence_summary"])

    def test_finding_relationships_and_compact_evidence_are_preserved(self) -> None:
        scenario = self.by_name["multiple_independent_divergences"]
        status, _, payload = self._api_compare(scenario.left, scenario.right)
        self.assertEqual(status, 200)
        self.assertEqual(payload["investigation"]["state"], "identified")
        self.assertTrue(payload["investigation"]["evidence_summary"])
        self.assertTrue(
            {
                item["structural_relation"]
                for item in payload["investigation"]["evidence_summary"]
            }
            <= {
                "same_coordinate",
                "descendant",
                "structurally_later_independent",
                "unrelated_branch",
                "additional_observation",
            }
        )
        for finding in payload["findings"]:
            self.assertIn("evidence", finding)
            self.assertIn("observed", finding)
            self.assertIn("relationships", finding)
            self.assertNotIn("span", finding)

    def test_uncertain_barriers_prevent_a_primary_and_retain_exact_evidence(self) -> None:
        for scenario_name in (
            "repeated_tool_reordering",
            "capture_disabled_one_side",
            "redacted_content",
            "early_termination_partial_trace",
            "missing_parent",
        ):
            with self.subTest(scenario=scenario_name):
                scenario = self.by_name[scenario_name]
                status, _, payload = self._api_compare(scenario.left, scenario.right)
                self.assertEqual(status, 200)
                self.assertEqual(payload["investigation"]["state"], "uncertain")
                self.assertIsNone(payload["investigation"]["starting_point"])
                self.assertTrue(payload["uncertainties"])
                self.assertTrue(
                    all(
                        set(item) == {
                            "uncertainty_id",
                            "coordinate",
                            "reason_code",
                            "side",
                            "blocks_earlier_claim",
                            "evidence",
                        }
                        for item in payload["uncertainties"]
                    )
                )

    def test_response_is_byte_deterministic_read_only_and_v2_is_unchanged(self) -> None:
        scenario = self.by_name["aligned_tool_output_change"]
        with Repository() as repository:
            _persist(repository, scenario.left)
            _persist(repository, scenario.right)
            app = create_app(repository)
            v2_path = f"/api/v2/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}"
            v3_path = f"/api/v3/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}"
            before_changes = repository.connection.total_changes
            v2_before = request(app, "GET", v2_path)
            first = request(app, "GET", v3_path)
            second = request(app, "GET", v3_path)
            v2_after = request(app, "GET", v2_path)
            self.assertEqual(first, second)
            self.assertEqual(first[0], 200)
            self.assertEqual(v2_before, v2_after)
            self.assertEqual(repository.connection.total_changes, before_changes)
            payload = json.loads(first[1], parse_float=Decimal)
            self.assertEqual(
                set(payload),
                {
                    "comparison_version",
                    "left_trace",
                    "right_trace",
                    "summary",
                    "investigation",
                    "findings",
                    "uncertainties",
                    "detail_endpoint",
                },
            )

    def test_timestamp_and_persistence_order_do_not_change_the_v3_result(self) -> None:
        repeated_tail = (
            "lookup-1",
            "normalize-1",
            "lookup-2",
            "normalize-2",
            "lookup-3",
            "normalize-3",
        )

        def response_for(*, overrides=None, reverse=False):
            left = clone_run(
                self.base,
                "api-v3-timestamp-left",
                drop=repeated_tail,
                timing_overrides_us=overrides,
            )
            right = clone_run(
                self.base,
                "api-v3-timestamp-right",
                drop=repeated_tail,
                span_mutators={"alerts": _captured_output({"alerts": ["notice"]})},
                timing_overrides_us=overrides,
            )
            if reverse:
                left = replace(left, spans=tuple(reversed(left.spans)))
                right = replace(right, spans=tuple(reversed(right.spans)))
            status, _, payload = self._api_compare(left, right)
            self.assertEqual(status, 200)
            return payload

        def structural_payload(payload):
            result = dict(payload)
            summary = dict(result["summary"])
            summary.pop("trace_fields")
            result["summary"] = summary
            return result

        normal = response_for()
        swapped = response_for(
            overrides={
                "plan": 1_700_000_900_000_000,
                "alerts": 1_700_000_100_000_000,
            },
        )
        equal = response_for(
            overrides={label: 1_700_000_500_000_000 for label in self.base.order},
        )
        shifted_reversed = response_for(
            overrides={
                "plan": 1_700_000_900_000_000,
                "alerts": 1_700_000_100_000_000,
            },
            reverse=True,
        )
        for candidate in (swapped, equal, shifted_reversed):
            with self.subTest(candidate=candidate["investigation"]):
                self.assertEqual(structural_payload(candidate), structural_payload(normal))

    def test_uses_one_snapshot_read_and_one_v02_comparison_for_all_v03_layers(self) -> None:
        scenario = self.by_name["aligned_tool_output_change"]
        with Repository() as repository:
            _persist(repository, scenario.left)
            _persist(repository, scenario.right)
            original_read = repository.get_trace_comparison_inputs
            app = create_app(repository)
            with patch.object(repository, "get_trace_comparison_inputs", wraps=original_read) as read_inputs:
                status, _ = request(
                    app,
                    "GET",
                    f"/api/v3/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}",
                )
            self.assertEqual(status, 200)
            self.assertEqual(read_inputs.call_count, 1)

        with patch(
            "tracemotive.api_v3.compare_trace_inputs",
            wraps=compare_trace_inputs,
        ) as v02_compare, patch(
            "tracemotive.divergence.compare_trace_inputs",
            side_effect=AssertionError("V03-11 rebuilt v0.2 comparison"),
        ), patch(
            "tracemotive.findings.compare_trace_inputs",
            side_effect=AssertionError("V03-20 rebuilt v0.2 comparison"),
        ):
            payload = build_v3_comparison(
                _record(scenario.left),
                scenario.left.spans,
                _record(scenario.right),
                scenario.right.spans,
            )
        self.assertEqual(payload["comparison_version"], "0.3")
        self.assertEqual(v02_compare.call_count, 1)

    def test_errors_and_size_limit_use_the_existing_safe_contract(self) -> None:
        scenario = self.by_name["aligned_tool_output_change"]
        with Repository() as repository:
            _persist(repository, scenario.left)
            _persist(repository, scenario.right)
            app = create_app(repository)
            same = scenario.left.trace.trace_id
            for path, status_code, code in (
                ("/api/v3/compare/not-an-id/00000000000000000000000000000000", 400, "invalid_request"),
                (f"/api/v3/compare/{same}/{same}", 400, "invalid_request"),
                ("/api/v3/compare/4bf92f3577b34da6a3ce929d0e0e4736/5bf92f3577b34da6a3ce929d0e0e4736", 404, "not_found"),
                (f"/api/v3/compare/{same}/{scenario.right.trace.trace_id}?threshold=1", 400, "invalid_request"),
            ):
                with self.subTest(path=path):
                    status, body = request(app, "GET", path)
                    self.assertEqual(status, status_code)
                    self.assertEqual(json.loads(body)["error"]["code"], code)
                    self.assertNotIn(b"C:\\", body)

            with patch(
                "tracemotive.query.build_v3_comparison",
                side_effect=ComparisonTooLargeError("internal limit detail"),
            ):
                status, body = request(
                    app,
                    "GET",
                    f"/api/v3/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}",
                )
            self.assertEqual(
                (status, json.loads(body)),
                (413, {"error": {"code": "comparison_too_large", "message": "comparison too large"}}),
            )
            with patch(
                "tracemotive.query.build_v3_comparison",
                side_effect=RuntimeError("C:\\private\\secret-detail"),
            ):
                status, body = request(
                    app,
                    "GET",
                    f"/api/v3/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}",
                )
            self.assertEqual(
                (status, json.loads(body)),
                (500, {"error": {"code": "internal_error", "message": "internal error"}}),
            )
            self.assertNotIn(b"secret-detail", body)

        with self.assertRaises(ComparisonTooLargeError):
            build_v3_comparison(
                _record(scenario.left),
                tuple(scenario.left.spans) * (MAX_COMPARISON_SPANS + 1),
                _record(scenario.right),
                scenario.right.spans,
            )

    def test_captured_strings_remain_inert_json_and_large_evidence_stays_bounded(self) -> None:
        hostile_left = clone_run(
            self.base,
            "api-v3-hostile-left",
            span_mutators={"alerts": _captured_output({"message": "safe"})},
        )
        hostile_right = clone_run(
            self.base,
            "api-v3-hostile-right",
            span_mutators={"alerts": _captured_output({"message": "<script>window.pwned=1</script>"})},
        )
        status, body, payload = self._api_compare(hostile_left, hostile_right)
        self.assertEqual(status, 200)
        self.assertIn(b"<script>window.pwned=1</script>", body)
        output_finding = next(item for item in payload["findings"] if item["type"] == "tool_output_changed")
        self.assertEqual(output_finding["evidence"][0]["right"], "<script>window.pwned=1</script>")

        large_left = clone_run(
            self.base,
            "api-v3-large-left",
            span_mutators={"alerts": _captured_output({"blob": "a" * 32_768})},
        )
        large_right = clone_run(
            self.base,
            "api-v3-large-right",
            span_mutators={"alerts": _captured_output({"blob": "b" * 32_768})},
        )
        with Repository() as repository:
            _persist(repository, large_left)
            _persist(repository, large_right)
            app = create_app(repository)
            path = f"/compare/{large_left.trace.trace_id}/{large_right.trace.trace_id}"
            v2 = request(app, "GET", "/api/v2" + path)
            v3 = request(app, "GET", "/api/v3" + path)
        self.assertEqual((v2[0], v3[0]), (200, 200))
        self.assertLess(len(v3[1]), 4 * 1024 * 1024)
        v3_payload = json.loads(v3[1])
        self.assertNotIn("spans", v3_payload)
        self.assertNotIn("ambiguous_groups", v3_payload)
        self.assertNotIn("unavailable_spans", v3_payload)


if __name__ == "__main__":
    unittest.main()
