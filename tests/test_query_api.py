import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from urllib.parse import urlsplit
import unittest

from agentlens.canonical import CaptureInfo, Error, Trace
from agentlens.canonical.models import _ExactNumber, _canonical_json_dumps
from agentlens.collector import DEFAULT_BIND_HOST, create_app
from agentlens.storage import Repository
from tests.test_collector import (
    CHILD_SPAN_ID,
    OTHER_TRACE_ID,
    SPAN_ID,
    THIRD_TRACE_ID,
    TRACE_ID,
    batch,
    event,
    make_span,
    make_trace,
)


async def _asgi_request(app, *, method, path, body=b""):
    parsed = urlsplit(path)
    query_string = parsed.query.encode("ascii")
    messages = []
    delivered = False
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": query_string,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 50000),
        "server": (DEFAULT_BIND_HOST, 8765),
        "root_path": "",
    }

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return response_start["status"], response_body


def request(app, method, path, body=b""):
    return asyncio.run(_asgi_request(app, method=method, path=path, body=body))


def ingest(app, *events):
    return request(
        app,
        "POST",
        "/api/v1/ingest",
        _canonical_json_dumps(batch(*events)).encode("utf-8"),
    )


def trace_at(
    trace_id,
    started_at,
    ended_at,
    *,
    status="ok",
    name="Agent workflow",
):
    base = make_trace(trace_id, ended=True, status=status, name=name)
    return replace(base, started_at=started_at, ended_at=ended_at)


def span_at(
    trace_id,
    span_id,
    started_at,
    ended_at,
    *,
    parent_span_id=None,
    error=None,
):
    base = make_span(
        trace_id,
        span_id,
        ended=True,
        parent_span_id=parent_span_id,
        error=error,
    )
    return replace(base, started_at=started_at, ended_at=ended_at)


class QueryAPITests(unittest.TestCase):
    def test_empty_database_health_and_trace_list(self):
        with Repository() as repository:
            app = create_app(repository)
            self.assertEqual(request(app, "GET", "/api/v1/health"), (200, b'{"status":"ok"}'))
            self.assertEqual(
                request(app, "GET", "/api/v1/traces"),
                (200, b'{"items":[],"limit":50,"offset":0,"total":0}'),
            )

    def test_trace_list_pagination_filter_order_and_aggregate_stats(self):
        first = trace_at(
            TRACE_ID,
            "2026-08-10T13:00:00.000000Z",
            "2026-08-10T13:00:01.000000Z",
            name="Alpha workflow",
        )
        second = trace_at(
            OTHER_TRACE_ID,
            "2026-08-10T13:00:02.000000Z",
            "2026-08-10T13:00:03.000000Z",
            name="Beta workflow",
        )
        third = trace_at(
            THIRD_TRACE_ID,
            "2026-08-10T13:00:02.000000Z",
            "2026-08-10T13:00:04.000000Z",
            name="Gamma workflow",
        )
        error_span = span_at(
            TRACE_ID,
            SPAN_ID,
            "2026-08-10T13:00:00.100000Z",
            "2026-08-10T13:00:00.900000Z",
            error=Error("ToolError", "recovered"),
        )
        child = span_at(
            TRACE_ID,
            CHILD_SPAN_ID,
            "2026-08-10T13:00:00.100000Z",
            "2026-08-10T13:00:00.900000Z",
            parent_span_id=SPAN_ID,
        )
        with Repository() as repository:
            app = create_app(repository)
            self.assertEqual(
                ingest(
                    app,
                    event("trace.ended", first),
                    event("trace.ended", second),
                    event("trace.ended", third),
                    event("span.ended", error_span),
                    event("span.ended", child),
                )[0],
                200,
            )
            status, body = request(app, "GET", "/api/v1/traces?limit=2&offset=0")
            self.assertEqual(status, 200)
            payload = json.loads(body, parse_float=Decimal)
            self.assertEqual(payload["total"], 3)
            self.assertEqual(
                [item["trace_id"] for item in payload["items"]],
                [OTHER_TRACE_ID, THIRD_TRACE_ID],
            )
            self.assertEqual(payload["items"][1]["latency_ms"], Decimal("2000"))
            status, body = request(
                app,
                "GET",
                "/api/v1/traces?status=ok&name=WORKFLOW&limit=1&offset=1",
            )
            self.assertEqual(status, 200)
            filtered = json.loads(body)
            self.assertEqual(filtered["total"], 3)
            self.assertEqual(filtered["items"][0]["trace_id"], THIRD_TRACE_ID)

            status, body = request(app, "GET", "/api/v1/traces/" + TRACE_ID)
            self.assertEqual(status, 200)
            detail = json.loads(body)
            self.assertEqual(detail["trace"]["status"], "ok")
            self.assertEqual(detail["stats"]["span_count"], 2)
            self.assertEqual(detail["stats"]["error_count"], 1)
            self.assertEqual(detail["stats"]["llm_call_count"], 2)
            self.assertEqual(detail["stats"]["input_tokens"], 4)
            self.assertEqual(detail["stats"]["output_tokens"], 6)
            status, body = request(
                app,
                "GET",
                "/api/v1/traces/" + OTHER_TRACE_ID + "/spans",
            )
            self.assertEqual((status, json.loads(body)), (200, {"items": []}))

    def test_unset_trace_and_child_error_keep_status_independent(self):
        trace = make_trace(TRACE_ID)
        error_span = make_span(
            TRACE_ID,
            SPAN_ID,
            ended=True,
            error=Error("ToolError", "recovered"),
        )
        with Repository() as repository:
            app = create_app(repository)
            self.assertEqual(ingest(app, event("trace.started", trace), event("span.ended", error_span))[0], 200)
            status, body = request(app, "GET", "/api/v1/traces/" + TRACE_ID)
            self.assertEqual(status, 200)
            detail = json.loads(body)
            self.assertEqual(detail["trace"]["status"], "unset")
            self.assertEqual(detail["stats"]["error_count"], 1)

    def test_span_ordering_composite_identity_and_capture_round_trip(self):
        first = span_at(
            TRACE_ID,
            SPAN_ID,
            "2026-08-10T13:00:00.500000Z",
            "2026-08-10T13:00:00.700000Z",
        )
        second = span_at(
            TRACE_ID,
            CHILD_SPAN_ID,
            "2026-08-10T13:00:00.100000Z",
            "2026-08-10T13:00:00.300000Z",
            parent_span_id=SPAN_ID,
        )
        same_span_other_trace = span_at(
            OTHER_TRACE_ID,
            SPAN_ID,
            "2026-08-10T13:00:00.100000Z",
            "2026-08-10T13:00:00.300000Z",
        )
        with Repository() as repository:
            app = create_app(repository)
            self.assertEqual(
                ingest(
                    app,
                    event("trace.ended", make_trace(ended=True)),
                    event("trace.ended", make_trace(OTHER_TRACE_ID, ended=True)),
                    event("span.ended", first),
                    event("span.ended", second),
                    event("span.ended", same_span_other_trace),
                )[0],
                200,
            )
            status, body = request(app, "GET", "/api/v1/traces/" + TRACE_ID + "/spans")
            self.assertEqual(status, 200)
            items = json.loads(body)["items"]
            self.assertEqual([item["span"]["span_id"] for item in items], [CHILD_SPAN_ID, SPAN_ID])
            self.assertEqual(items[0]["span"]["parent_span_id"], SPAN_ID)
            self.assertEqual(items[0]["latency_ms"], 200)
            status, body = request(
                app,
                "GET",
                "/api/v1/traces/" + OTHER_TRACE_ID + "/spans/" + SPAN_ID,
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["span"]["trace_id"], OTHER_TRACE_ID)

    def test_exact_numbers_unicode_and_privacy_are_preserved_in_query_json(self):
        trace = make_trace(TRACE_ID, ended=True)
        payload = trace.to_dict()
        payload["name"] = "日本語 workflow"
        payload["metadata"] = {
            "huge": 10**80,
            "tiny": _ExactNumber(Decimal("0.00000000000000000001")),
            "nested": {"escaped": "line\\n\"quoted\""},
        }
        trace = Trace.from_dict(payload)
        secret_span = make_span(
            TRACE_ID,
            SPAN_ID,
            ended=True,
            input_value={"password": "secret-value"},
            input_capture=CaptureInfo("captured", None, False),
        )
        with Repository() as repository:
            app = create_app(repository)
            repository.upsert_trace(trace)
            repository.upsert_span(secret_span)
            status, body = request(app, "GET", "/api/v1/traces/" + TRACE_ID)
            self.assertEqual(status, 200)
            self.assertIn(b"1E-20", body)
            self.assertIn(str(10**80).encode("ascii"), body)
            self.assertIn("日本語".encode("utf-8"), body)
            self.assertNotIn(b"secret-value", body)
            span_payload = json.loads(
                request(app, "GET", "/api/v1/traces/" + TRACE_ID + "/spans")[1]
            )["items"][0]["span"]
            self.assertEqual(span_payload["input"], {"password": "[REDACTED]"})
            self.assertTrue(span_payload["capture"]["input"]["redacted"])

    def test_query_validation_not_found_delete_and_no_global_span_route(self):
        with Repository() as repository:
            app = create_app(repository)
            for path in (
                "/api/v1/traces/not-an-id",
                "/api/v1/traces?limit=0",
                "/api/v1/traces?offset=-1",
                "/api/v1/traces?status=invalid",
            ):
                status, body = request(app, "GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
            status, body = request(app, "GET", "/api/v1/traces/" + TRACE_ID)
            self.assertEqual(status, 404)
            self.assertNotIn(b"C:\\", body)
            repository.upsert_trace(make_trace(TRACE_ID, ended=True))
            repository.connection.execute(
                "UPDATE traces SET source_json = ? WHERE trace_id = ?",
                ("{", TRACE_ID),
            )
            status, body = request(app, "GET", "/api/v1/traces/" + TRACE_ID)
            self.assertEqual(status, 500)
            self.assertEqual(json.loads(body), {"error": {"code": "internal_error", "message": "internal error"}})
            status, body = request(app, "GET", "/api/v1/spans/" + SPAN_ID)
            self.assertEqual(status, 404)
            self.assertEqual(request(app, "DELETE", "/api/v1/traces/not-an-id")[0], 400)
            self.assertEqual(request(app, "DELETE", "/api/v1/traces/" + TRACE_ID)[0], 204)
            self.assertEqual(request(app, "DELETE", "/api/v1/traces/" + TRACE_ID)[0], 204)


if __name__ == "__main__":
    unittest.main()
