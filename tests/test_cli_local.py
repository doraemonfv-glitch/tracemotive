from __future__ import annotations

import io
import json
from unittest.mock import Mock, patch
import unittest

from tracemotive.cli import _parser
from tracemotive.local_client import (
    ApiContractError,
    InvalidEndpointError,
    LastSelection,
    LocalClientError,
    ApiResponseStatusError,
    SelectedTrace,
    TransportFailureError,
    JsonResponse,
    compare_traces,
    format_comparison_result,
    select_last_pair,
    validate_endpoint,
)


LEFT_ID = "a" * 32
RIGHT_ID = "b" * 32


def _summary(trace_id: str, name: str, started_at: str) -> dict:
    return {
        "trace_id": trace_id,
        "name": name,
        "started_at": started_at,
        "ended_at": None,
        "status": "ok",
    }


def _comparison_payload(
    *,
    uncertain: bool = False,
    uncertainties: list[dict] | None = None,
) -> dict:
    starting_point = (
        {
            "kind": "span",
            "semantic_path": [
                {
                    "type": "agent",
                    "operation": "agent.run",
                    "name": "Agent",
                    "ordinal": 0,
                }
            ],
            "group_signature": None,
            "left": {"trace_id": LEFT_ID, "span_id": "c" * 16},
            "right": {"trace_id": RIGHT_ID, "span_id": "d" * 16},
            "finding_id": "finding-0001",
            "label": "Inspect observed span error change",
        }
        if not uncertain
        else None
    )
    finding = {
        "finding_id": "finding-0001",
        "type": "new_error",
        "coordinate": {"kind": "span", "semantic_path": [], "group_signature": None},
        "left": {"trace_id": LEFT_ID, "span_id": "c" * 16},
        "right": {"trace_id": RIGHT_ID, "span_id": "d" * 16},
        "field_path": "/status",
        "scope": "behavioral",
        "observation_state": "confirmed_observation",
        "reason_code": "error_observed",
        "observed": {},
        "evidence": [],
        "relationships": [],
    }
    return {
        "comparison_version": "0.3",
        "left_trace": {"trace_id": LEFT_ID, "name": "Older run", "status": "ok"},
        "right_trace": {"trace_id": RIGHT_ID, "name": "Newer run", "status": "error"},
        "summary": {},
        "investigation": {
            "state": "UNCERTAIN" if uncertain else "IDENTIFIED",
            "ordering_basis": "structural_triage_order",
            "starting_point": starting_point,
        },
        "findings": [] if uncertain else [finding],
        "uncertainties": uncertainties or [],
    }


def _http_json(payload: object, status: int = 200):
    body = json.dumps(payload).encode("utf-8")
    response = Mock()
    response.status = status
    response.read.return_value = body
    connection = Mock()
    connection.getresponse.return_value = response
    return connection


def _http_body(body: bytes, status: int = 200):
    response = Mock()
    response.status = status
    response.read.return_value = body
    connection = Mock()
    connection.getresponse.return_value = response
    return connection


def _list_page(items: list[dict], total: int, offset: int = 0) -> dict:
    return {"items": items, "limit": 100, "offset": offset, "total": total}


def _capture_handler(handler) -> tuple[int, bytes, str]:
    output = io.BytesIO()
    text_output = io.TextIOWrapper(output, encoding="utf-8", write_through=True)
    errors = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(text_output), contextlib.redirect_stderr(errors):
        code = handler()
    text_output.detach()
    return code, output.getvalue(), errors.getvalue()


class LocalEndpointTests(unittest.TestCase):
    def test_loopback_endpoints_follow_the_existing_transport_policy(self) -> None:
        for endpoint in (
            "http://127.0.0.1:8765",
            "http://localhost:8765",
            "http://[::1]:8765",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIsInstance(validate_endpoint(endpoint), object)

    def test_nonlocal_or_rich_urls_are_rejected(self) -> None:
        for endpoint in (
            "http://example.com:8765",
            "http://0.0.0.0:8765",
            "http://user:pass@127.0.0.1:8765",
            "http://127.0.0.1:8765/ui",
            "http://127.0.0.1:8765?x=1",
            "http://127.0.0.1:8765#fragment",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(InvalidEndpointError):
                    validate_endpoint(endpoint)


class CompareClientTests(unittest.TestCase):
    def test_valid_v3_comparison_is_returned_with_raw_bytes(self) -> None:
        payload = _comparison_payload()
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_json(payload),
        ):
            result = compare_traces(LEFT_ID, RIGHT_ID)
        self.assertEqual(result.value, payload)
        self.assertEqual(json.loads(result.raw.decode("utf-8")), payload)

    def test_api_and_transport_failures_are_distinguished(self) -> None:
        cases = [
            ({"error": {"code": "invalid_request"}}, 400, ApiResponseStatusError),
            ({"error": {"code": "not_found"}}, 404, ApiResponseStatusError),
            ({"error": {"code": "comparison_too_large"}}, 413, ApiResponseStatusError),
            ({"error": {"code": "internal_error"}}, 500, ApiResponseStatusError),
        ]
        for payload, status, expected in cases:
            with self.subTest(status=status):
                with patch(
                    "tracemotive.local_client.HTTPConnection",
                    return_value=_http_json(payload, status=status),
                ):
                    with self.assertRaises(expected):
                        compare_traces(LEFT_ID, RIGHT_ID)

    def test_same_ids_are_left_to_the_api_contract(self) -> None:
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_json({"error": {"code": "invalid_request"}}, status=400),
        ):
            with self.assertRaises(LocalClientError):
                compare_traces(LEFT_ID, LEFT_ID)

    def test_malformed_id_is_delegated_without_local_reinterpretation(self) -> None:
        connection = _http_json(
            {"error": {"code": "invalid_request", "message": "invalid request"}},
            status=400,
        )
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=connection,
        ) as connection_factory:
            with self.assertRaises(LocalClientError):
                compare_traces("not-a-trace", RIGHT_ID)
        self.assertEqual(
            connection_factory.call_args.args,
            ("127.0.0.1", 8765),
        )
        self.assertEqual(
            connection_factory.return_value.request.call_args.args[1],
            f"/api/v3/compare/not-a-trace/{RIGHT_ID}",
        )

    def test_unavailable_server_maps_to_transport_failure(self) -> None:
        with patch(
            "tracemotive.local_client.HTTPConnection",
            side_effect=OSError("refused"),
        ):
            with self.assertRaises(TransportFailureError):
                compare_traces(LEFT_ID, RIGHT_ID)

    def test_non_json_success_is_a_contract_failure(self) -> None:
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_body(b"<html>not json</html>"),
        ):
            with self.assertRaises(ApiContractError):
                compare_traces(LEFT_ID, RIGHT_ID)

    def test_oversized_response_is_rejected_before_json_parsing(self) -> None:
        oversized = b"x" * (4 * 1024 * 1024 + 1)
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_body(oversized),
        ):
            with self.assertRaisesRegex(ApiContractError, "4 MiB"):
                compare_traces(LEFT_ID, RIGHT_ID)

    def test_malformed_v3_contract_fails_closed(self) -> None:
        payload = _comparison_payload()
        del payload["investigation"]
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_json(payload),
        ):
            with self.assertRaisesRegex(ApiContractError, "incomplete"):
                compare_traces(LEFT_ID, RIGHT_ID)


class LastSelectionTests(unittest.TestCase):
    def test_two_exact_matches_use_older_left_and_newer_right(self) -> None:
        older = _summary(LEFT_ID, "Order run", "2026-01-01T00:00:00Z")
        newer = _summary(RIGHT_ID, "Order run", "2026-01-02T00:00:00Z")
        connections = [
            _http_json(_list_page([newer, older], 2)),
        ]
        with patch(
            "tracemotive.local_client.HTTPConnection",
            side_effect=connections,
        ) as connections_factory:
            selection = select_last_pair("Order run")
        self.assertEqual(selection.left.trace_id, older["trace_id"])
        self.assertEqual(selection.left.started_at, older["started_at"])
        self.assertEqual(selection.right.trace_id, newer["trace_id"])
        self.assertEqual(selection.right.started_at, newer["started_at"])
        self.assertEqual(connections_factory.call_count, 1)

    def test_more_than_two_exact_matches_stop_at_the_first_two(self) -> None:
        third = _summary("c" * 32, "Order run", "2026-01-01T00:00:00Z")
        second = _summary(LEFT_ID, "Order run", "2026-01-02T00:00:00Z")
        first = _summary(RIGHT_ID, "Order run", "2026-01-03T00:00:00Z")
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_json(_list_page([first, second, third], 3)),
        ):
            selection = select_last_pair("Order run")
        self.assertEqual((selection.left.trace_id, selection.right.trace_id), (LEFT_ID, RIGHT_ID))

    def test_equal_started_at_refuses_to_infer_older_newer(self) -> None:
        first = _summary(LEFT_ID, "Order run", "2026-01-01T00:00:00Z")
        second = _summary(RIGHT_ID, "Order run", "2026-01-01T00:00:00Z")
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_json(_list_page([first, second], 2)),
        ):
            with self.assertRaisesRegex(
                ApiContractError,
                r"same started_at.*cannot infer which is older.*compare LEFT RIGHT",
            ):
                select_last_pair("Order run")

    def test_substring_and_case_matches_are_not_paired(self) -> None:
        items = [
            _summary("c" * 32, "Order run changed", "2026-01-03T00:00:00Z"),
            _summary("d" * 32, "ORDER RUN", "2026-01-02T00:00:00Z"),
            _summary("e" * 32, "order run", "2026-01-01T00:00:00Z"),
        ]
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_json(_list_page(items, len(items))),
        ):
            with self.assertRaisesRegex(ApiContractError, "fewer than two"):
                select_last_pair("Order Run")

    def test_exact_matches_mixed_with_substrings_are_selected(self) -> None:
        exact_new = _summary(RIGHT_ID, "Job", "2026-01-03T00:00:00Z")
        exact_old = _summary(LEFT_ID, "Job", "2026-01-01T00:00:00Z")
        items = [
            _summary("c" * 32, "Job changed", "2026-01-04T00:00:00Z"),
            exact_new,
            _summary("d" * 32, "Other job", "2026-01-02T00:00:00Z"),
            exact_old,
        ]
        with patch(
            "tracemotive.local_client.HTTPConnection",
            return_value=_http_json(_list_page(items, len(items))),
        ):
            selection = select_last_pair("Job")
        self.assertEqual(selection.left.trace_id, LEFT_ID)
        self.assertEqual(selection.right.trace_id, RIGHT_ID)

    def test_exact_matches_beyond_the_first_page_are_found(self) -> None:
        first_page = [
            _summary(f"{index:032x}", f"Other {index}", f"2026-01-01T00:{index:02d}:00Z")
            for index in range(100)
        ]
        second_page = [
            _summary(RIGHT_ID, "Order run", "2026-01-02T00:00:00Z"),
            _summary(LEFT_ID, "Order run", "2026-01-01T00:00:00Z"),
        ]
        connections = [
            _http_json(_list_page(first_page, 102)),
            _http_json(_list_page(second_page, 102, offset=100)),
        ]
        with patch(
            "tracemotive.local_client.HTTPConnection",
            side_effect=connections,
        ) as connections_factory:
            selection = select_last_pair("Order run")
        self.assertEqual((selection.left.trace_id, selection.right.trace_id), (LEFT_ID, RIGHT_ID))
        self.assertEqual(connections_factory.call_count, 2)

    def test_zero_or_one_exact_match_fails_closed(self) -> None:
        none = [_summary("c" * 32, "Other", "2026-01-01T00:00:00Z")]
        one = [_summary(LEFT_ID, "Order run", "2026-01-01T00:00:00Z")]
        for items in (none, one):
            with self.subTest(matches=len([item for item in items if item["name"] == "Order run"])):
                with patch(
                    "tracemotive.local_client.HTTPConnection",
                    return_value=_http_json(_list_page(items, len(items))),
                ):
                    with self.assertRaises(ApiContractError):
                        select_last_pair("Order run")


class CompareCliContractTests(unittest.TestCase):
    def test_parser_requires_last_name_and_uses_argparse_exit_two(self) -> None:
        with self.assertRaises(SystemExit) as context:
            _parser().parse_args(["last"])
        self.assertEqual(context.exception.code, 2)

    def test_human_supported_result_projects_only_returned_facts(self) -> None:
        payload = _comparison_payload()
        result = JsonResponse(payload, json.dumps(payload).encode("utf-8"))
        output = format_comparison_result(result, endpoint="http://127.0.0.1:8765")
        self.assertIn("Look here: Inspect observed span error change", output)
        self.assertIn("reason_code=error_observed", output)
        self.assertIn("No uncertainty records were returned.", output)
        self.assertIn(f"#/compare/{LEFT_ID}/{RIGHT_ID}", output)
        self.assertIn("does not establish cause", output)

    def test_human_uncertain_and_unknown_codes_do_not_invent_meaning(self) -> None:
        uncertainties = [
            {
                "reason_code": "capture_unavailable",
                "side": "left",
                "blocks_earlier_claim": False,
            },
            {
                "reason_code": "redacted_observation",
                "side": "both",
                "blocks_earlier_claim": True,
            },
            {
                "reason_code": "future_unknown_reason",
                "side": "right",
                "blocks_earlier_claim": False,
            },
        ]
        payload = _comparison_payload(uncertain=True, uncertainties=uncertainties)
        result = JsonResponse(payload, json.dumps(payload).encode("utf-8"))
        output = format_comparison_result(result, endpoint="http://127.0.0.1:8765")
        self.assertIn("No starting point was returned by /api/v3.", output)
        self.assertIn("Captured content was unavailable on at least one side.", output)
        self.assertIn("Redaction prevented a content-level comparison.", output)
        self.assertIn("reason_code=future_unknown_reason side=right blocks_earlier_claim=False", output)

    def test_compare_json_stdout_contains_only_the_v3_body(self) -> None:
        payload = _comparison_payload()
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        arguments = _parser().parse_args(["compare", LEFT_ID, RIGHT_ID, "--json"])
        with patch(
            "tracemotive.cli.compare_traces_http",
            return_value=JsonResponse(payload, raw),
        ):
            code, stdout, errors = _capture_handler(lambda: arguments.handler(arguments))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, raw + b"\n")
        self.assertEqual(errors, "")

    def test_last_json_stdout_contains_only_the_v3_body(self) -> None:
        payload = _comparison_payload()
        raw = b'{"comparison_version":"0.3"}'
        selection = LastSelection(
            SelectedTrace(LEFT_ID, "Order run", "2026-01-01T00:00:00Z", None),
            SelectedTrace(RIGHT_ID, "Order run", "2026-01-02T00:00:00Z", None),
        )
        arguments = _parser().parse_args(["last", "Order run", "--json"])
        with patch(
            "tracemotive.cli.last_comparison",
            return_value=(selection, JsonResponse(payload, raw)),
        ):
            code, stdout, errors = _capture_handler(lambda: arguments.handler(arguments))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, raw + b"\n")
        self.assertEqual(errors, "")

    def test_open_success_uses_webbrowser_with_local_hash_url(self) -> None:
        payload = _comparison_payload()
        arguments = _parser().parse_args(["compare", LEFT_ID, RIGHT_ID, "--open"])
        with patch(
            "tracemotive.cli.compare_traces_http",
            return_value=JsonResponse(payload, b"{}"),
        ), patch(
            "tracemotive.local_client.webbrowser.open",
            return_value=True,
        ) as browser:
            code, _, errors = _capture_handler(lambda: arguments.handler(arguments))
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        browser.assert_called_once_with(
            f"http://127.0.0.1:8765/#/compare/{LEFT_ID}/{RIGHT_ID}",
            new=0,
            autoraise=False,
        )

    def test_browser_failure_reports_status_six_after_successful_comparison(self) -> None:
        payload = _comparison_payload()
        arguments = _parser().parse_args(["compare", LEFT_ID, RIGHT_ID, "--open"])
        with patch(
            "tracemotive.cli.compare_traces_http",
            return_value=JsonResponse(payload, b"{}"),
        ), patch(
            "tracemotive.local_client.webbrowser.open",
            return_value=False,
        ):
            code, _, errors = _capture_handler(lambda: arguments.handler(arguments))
        self.assertEqual(code, 6)
        self.assertIn("no browser", errors)

    def test_cli_error_status_mapping(self) -> None:
        from tracemotive.cli import main
        from tracemotive.local_client import TransportFailureError

        code, _, errors = _capture_handler(
            lambda: main(["compare", LEFT_ID, RIGHT_ID, "--endpoint", "http://example.com"])
        )
        self.assertEqual(code, 3)
        self.assertNotEqual(errors, "")

        with patch(
            "tracemotive.local_client.HTTPConnection",
            side_effect=OSError("refused"),
        ):
            code, _, errors = _capture_handler(
                lambda: main(["compare", LEFT_ID, RIGHT_ID])
            )
        self.assertEqual(code, 4)
        self.assertNotEqual(errors, "")

        for failure, expected in (
            (ApiContractError("bad contract"), 5),
            (RuntimeError("unexpected"), 1),
        ):
            with self.subTest(expected=expected):
                with patch(
                    "tracemotive.cli.compare_traces_http",
                    side_effect=failure,
                ):
                    code, _, errors = _capture_handler(
                        lambda: main(["compare", LEFT_ID, RIGHT_ID])
                    )
                self.assertEqual(code, expected)
                self.assertNotEqual(errors, "")


if __name__ == "__main__":
    unittest.main()
