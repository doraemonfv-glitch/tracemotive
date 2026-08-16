from __future__ import annotations

import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen
from unittest.mock import Mock, patch

from tracemotive.demo import (
    DEMO_PRIMARY_COORDINATE,
    DemoError,
    _validated_endpoint,
    _validate_demo_comparison,
    format_demo_result,
    seed_demo,
)


_TRACE_ID = re.compile(r"[0-9a-f]{32}")


def _health(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/api/v1/health", timeout=1) as response:
            return response.status == 200
    except (OSError, URLError, ValueError):
        return False


def _json_get(endpoint: str, path: str) -> dict:
    with urlopen(f"{endpoint}{path}", timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _free_port() -> int:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        return int(blocker.getsockname()[1])


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.endpoint = f"http://127.0.0.1:{cls.port}"
        database = Path(cls.directory.name) / "demo.sqlite3"
        cls.server = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from tracemotive.cli import main; raise SystemExit(main())",
                "serve",
                "--db",
                str(database),
                "--port",
                str(cls.port),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not _health(cls.endpoint):
            if cls.server.poll() is not None:
                raise AssertionError("demo test server exited before becoming ready")
            time.sleep(0.05)
        if not _health(cls.endpoint):
            cls.server.terminate()
            raise AssertionError("demo test server did not become ready")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server.poll() is None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server.kill()
                cls.server.wait(timeout=10)
        cls.directory.cleanup()

    def test_server_unavailable_and_remote_endpoint_fail_actionably(self) -> None:
        with self.assertRaisesRegex(DemoError, "not running on http://127.0.0.1:1"):
            seed_demo("http://127.0.0.1:1")
        with self.assertRaisesRegex(DemoError, "loopback URL"):
            seed_demo("http://example.com:8765")

    def test_endpoint_validation_rejects_credentials_remote_hosts_and_invalid_ports(self) -> None:
        for endpoint in (
            "http://user@127.0.0.1:8765",
            "http://127.0.0.1.example.com:8765",
            "http://0.0.0.0:8765",
            "http://127.0.0.1:0",
            "http://127.0.0.1:8765/path",
            "http://127.0.0.1:8765?query=1",
            "http://127.0.0.1:8765#fragment",
        ):
            with self.assertRaises(DemoError, msg=endpoint):
                _validated_endpoint(endpoint)

    def test_health_check_rejects_a_non_tracemotive_http_200_service(self) -> None:
        fake = Mock()
        fake.getresponse.return_value.status = 200
        fake.getresponse.return_value.read.return_value = b"not TraceMotive"
        with patch("tracemotive.demo.http.client.HTTPConnection", return_value=fake):
            with self.assertRaisesRegex(DemoError, "not running on"):
                seed_demo("http://127.0.0.1:8765")

    def test_demo_result_validation_rejects_an_unidentified_v3_response(self) -> None:
        fake = Mock()
        fake.getresponse.return_value.status = 200
        fake.getresponse.return_value.read.return_value = json.dumps(
            {
                "comparison_version": "0.3",
                "left_trace": {"trace_id": "a" * 32},
                "right_trace": {"trace_id": "b" * 32},
                "investigation": {"state": "uncertain", "starting_point": None},
                "findings": [],
            }
        ).encode("utf-8")
        with patch("tracemotive.demo.http.client.HTTPConnection", return_value=fake):
            with self.assertRaisesRegex(DemoError, "expected supported investigation point"):
                _validate_demo_comparison(
                    "127.0.0.1",
                    8765,
                    "a" * 32,
                    "b" * 32,
                )

    def test_seeded_pair_has_expected_v03_semantics_and_deep_link(self) -> None:
        result = seed_demo(self.endpoint)
        self.assertRegex(result.reference_trace_id, _TRACE_ID)
        self.assertRegex(result.changed_trace_id, _TRACE_ID)
        self.assertNotEqual(result.reference_trace_id, result.changed_trace_id)
        self.assertEqual(
            result.comparison_url,
            f"{self.endpoint}/#/compare/{result.reference_trace_id}/{result.changed_trace_id}",
        )
        printed = format_demo_result(result)
        self.assertIn(result.comparison_url, printed)
        self.assertIn("Each demo invocation creates a fresh pair", printed)

        payload = _json_get(
            self.endpoint,
            f"/api/v3/compare/{result.reference_trace_id}/{result.changed_trace_id}",
        )
        self.assertEqual(payload["investigation"]["state"], "identified")
        primary_id = payload["investigation"]["starting_point"]["finding_id"]
        primary = next(item for item in payload["findings"] if item["finding_id"] == primary_id)
        self.assertEqual(primary["type"], "tool_output_changed")
        self.assertEqual(primary["scope"], "behavioral")
        self.assertEqual(primary["coordinate"]["semantic_path"], list(DEMO_PRIMARY_COORDINATE))
        behavioral_types = {item["type"] for item in payload["findings"] if item["scope"] == "behavioral"}
        self.assertIn("new_error", behavioral_types)
        self.assertIn("tool_added", behavioral_types)
        context_types = {item["type"] for item in payload["findings"] if item["scope"] == "context_only"}
        self.assertEqual(context_types, {"trace_status_changed"})
        self.assertEqual(
            payload["detail_endpoint"]["path"],
            f"/api/v2/compare/{result.reference_trace_id}/{result.changed_trace_id}",
        )
        detail = _json_get(self.endpoint, payload["detail_endpoint"]["path"])
        self.assertEqual(detail["comparison_version"], "0.2")

    def test_repeated_seed_creates_new_pair_without_deleting_existing_traces(self) -> None:
        first = seed_demo(self.endpoint)
        before = _json_get(self.endpoint, "/api/v1/traces?limit=100")
        second = seed_demo(self.endpoint)
        after = _json_get(self.endpoint, "/api/v1/traces?limit=100")

        self.assertNotEqual(first.reference_trace_id, second.reference_trace_id)
        self.assertNotEqual(first.changed_trace_id, second.changed_trace_id)
        self.assertGreaterEqual(after["total"], before["total"] + 2)
        self.assertEqual(
            _json_get(self.endpoint, f"/api/v1/traces/{first.reference_trace_id}")["trace"]["trace_id"],
            first.reference_trace_id,
        )

    def test_semantic_result_is_stable_across_fresh_pairs(self) -> None:
        first = seed_demo(self.endpoint)
        second = seed_demo(self.endpoint)

        def semantic(result):
            payload = _json_get(
                self.endpoint,
                f"/api/v3/compare/{result.reference_trace_id}/{result.changed_trace_id}",
            )
            investigation = payload["investigation"]
            return {
                "state": investigation["state"],
                "primary": investigation["starting_point"],
                "findings": [
                    (item["type"], item["scope"], item["coordinate"])
                    for item in payload["findings"]
                ],
                "uncertainties": [item["reason_code"] for item in payload["uncertainties"]],
            }

        first_semantics = semantic(first)
        second_semantics = semantic(second)
        for value in (first_semantics, second_semantics):
            value["primary"]["left"] = None
            value["primary"]["right"] = None
        self.assertEqual(first_semantics, second_semantics)

    def test_loopback_only_and_no_external_demo_dependency_are_explicit(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("tracemotive", "demo.py").read_text(encoding="utf-8")
        self.assertNotIn("openai", source.casefold())
        self.assertNotIn("anthropic", source.casefold())
        self.assertNotIn("requests", source.casefold())
        self.assertIn("validate_loopback_endpoint", source)


if __name__ == "__main__":
    unittest.main()
