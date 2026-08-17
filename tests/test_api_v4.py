from __future__ import annotations

from decimal import Decimal
import json
import unittest

from tests.divergence_evaluation import build_evaluation_corpus, capture_public_baseline
from tests.test_api_v3 import _persist
from tests.test_query_api import request
from tracemotive.collector import create_app
from tracemotive.storage import Repository


class V04ComparisonAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = capture_public_baseline()
        cls.scenarios = build_evaluation_corpus()
        cls.by_name = {scenario.name: scenario for scenario in cls.scenarios}

    def _request(self, version: str, scenario_name: str):
        scenario = self.by_name[scenario_name]
        with Repository() as repository:
            _persist(repository, scenario.left)
            _persist(repository, scenario.right)
            app = create_app(repository)
            return request(
                app,
                "GET",
                f"/api/{version}/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}",
            )

    def test_v4_returns_bounded_diff_and_local_actions_for_identified_finding(self) -> None:
        status, body = self._request("v4", "aligned_tool_output_change")
        payload = json.loads(body, parse_float=Decimal)

        self.assertEqual(status, 200)
        self.assertEqual(payload["comparison_version"], "0.4")
        self.assertEqual(
            set(payload),
            {"comparison_version", "left", "right", "summary", "investigation", "findings", "uncertainties"},
        )
        finding = next(item for item in payload["findings"] if item["type"] == "tool_output_changed")
        self.assertTrue(finding["structured_diff_available"])
        self.assertFalse(finding["structured_diff_truncated"])
        self.assertTrue(finding["structured_diff"])
        self.assertTrue(all(item["op"] in {"add", "remove", "replace"} for item in finding["structured_diff"]))
        self.assertTrue(all(item["path"].startswith("/output") for item in finding["structured_diff"]))
        self.assertEqual(
            {item["type"] for item in payload["investigation"]["actions"]},
            {"open_left", "open_right", "full_comparison", "copy_local_reference"},
        )
        self.assertNotIn("confidence", body.decode("utf-8").lower())
        self.assertNotIn("causal", body.decode("utf-8").lower())

    def test_v4_is_deterministic_and_does_not_change_v3_bytes(self) -> None:
        scenario = self.by_name["aligned_tool_input_change"]
        with Repository() as repository:
            _persist(repository, scenario.left)
            _persist(repository, scenario.right)
            app = create_app(repository)
            v3_path = f"/api/v3/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}"
            v4_path = f"/api/v4/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}"
            v3_before = request(app, "GET", v3_path)
            first = request(app, "GET", v4_path)
            second = request(app, "GET", v4_path)
            v3_after = request(app, "GET", v3_path)

        self.assertEqual(first, second)
        self.assertEqual(v3_before, v3_after)
        self.assertEqual(first[0], 200)
        self.assertEqual(json.loads(first[1])["comparison_version"], "0.4")

    def test_capture_barriers_have_no_fabricated_structured_diff(self) -> None:
        for scenario_name, reason in (
            ("capture_disabled_one_side", "capture_unavailable"),
            ("redacted_content", "redacted_observation"),
        ):
            with self.subTest(scenario=scenario_name):
                status, body = self._request("v4", scenario_name)
                payload = json.loads(body)
                self.assertEqual(status, 200)
                self.assertTrue(any(item["reason_code"] == reason for item in payload["uncertainties"]))
                self.assertTrue(all("structured_diff" not in item for item in payload["findings"]))

    def test_repeated_group_does_not_receive_member_navigation(self) -> None:
        status, body = self._request("v4", "repeated_tool_insertion")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        repetition = next(item for item in payload["findings"] if item["type"] == "tool_repetition_changed")
        self.assertIsNone(repetition["left"])
        self.assertIsNone(repetition["right"])
        self.assertNotIn("open_left", {item["type"] for item in payload["investigation"]["actions"]})
        self.assertNotIn("open_right", {item["type"] for item in payload["investigation"]["actions"]})

    def test_v4_route_rejects_invalid_requests_without_raw_details(self) -> None:
        scenario = self.by_name["aligned_tool_output_change"]
        with Repository() as repository:
            _persist(repository, scenario.left)
            _persist(repository, scenario.right)
            app = create_app(repository)
            for path, expected_status in (
                ("/api/v4/compare/not-an-id/00000000000000000000000000000000", 400),
                (f"/api/v4/compare/{scenario.left.trace.trace_id}/{scenario.left.trace.trace_id}", 400),
                ("/api/v4/compare/4bf92f3577b34da6a3ce929d0e0e4736/5bf92f3577b34da6a3ce929d0e0e4736", 404),
                (f"/api/v4/compare/{scenario.left.trace.trace_id}/{scenario.right.trace.trace_id}?extra=1", 400),
            ):
                with self.subTest(path=path):
                    status, body = request(app, "GET", path)
                    self.assertEqual(status, expected_status)
                    self.assertNotIn(b"C:\\", body)
                    self.assertNotIn(b"secret", body.lower())


if __name__ == "__main__":
    unittest.main()
