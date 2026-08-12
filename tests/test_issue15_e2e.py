"""Frozen Issue 15 deterministic full-stack and resilience coverage."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

import tracemotive
from tracemotive import sdk
from tracemotive.canonical import (
    AGENTLENS_SCHEMA_VERSION,
    AgentDetails,
    Capture,
    CaptureInfo,
    LLMDetails,
    LLMUsage,
    Span,
    SpanSource,
    ToolDetails,
    Trace,
    TraceSource,
)
from tracemotive.canonical.models import _canonical_json_dumps
from tracemotive.collector import create_app
from tracemotive.storage import Repository
from tracemotive.transport import (
    MAX_ATTEMPTS,
    QUEUE_CAPACITY,
    RETRY_BACKOFF_SECONDS,
    Transport,
)


_OPENER = build_opener(ProxyHandler({}))
_SECRET = "TEST-SECRET-VALUE"
_TRACE_ID = "8bf92f3577b34da6a3ce929d0e0e4736"
_SPAN_ID = "80f067aa0ba902b7"


def _asgi_request(app, method: str, target: str, body: bytes, headers: list[tuple[str, str]]):
    parsed = urlsplit(target)
    messages: list[dict[str, object]] = []
    body_read = False

    async def receive() -> dict[str, object]:
        nonlocal body_read
        if body_read:
            return {"type": "http.disconnect"}
        body_read = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parsed.path or "/",
        "raw_path": (parsed.path or "/").encode("utf-8"),
        "query_string": parsed.query.encode("ascii"),
        "headers": [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in headers],
        "server": ("127.0.0.1", 0),
        "client": ("127.0.0.1", 0),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    response_start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")  # type: ignore[union-attr]
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(response_start["status"]), response_start.get("headers", []), response_body  # type: ignore[index]


class _LoopbackCollectorServer:
    def __init__(self, database_path: Path) -> None:
        self.repository = Repository(str(database_path))
        self.app = create_app(self.repository)
        self.requests: list[tuple[str, str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _dispatch(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                headers = [(key, value) for key, value in self.headers.items()]
                owner.requests.append((self.command, self.path))
                status, response_headers, response_body = _asgi_request(
                    owner.app,
                    self.command,
                    self.path,
                    body,
                    headers,
                )
                self.send_response(status)
                for key, value in response_headers:  # type: ignore[assignment]
                    self.send_header(key.decode("latin-1"), value.decode("latin-1"))
                self.end_headers()
                self.wfile.write(response_body)

            def do_GET(self) -> None:
                self._dispatch()

            def do_POST(self) -> None:
                self._dispatch()

            def do_DELETE(self) -> None:
                self._dispatch()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="issue15-collector", daemon=True)
        self.thread.start()
        self._closed = False

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> bool:
        if self._closed:
            return not self.thread.is_alive()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.state.tracemotive_collector.close()
        self._closed = True
        return not self.thread.is_alive()


class _DropStartedCollectorServer:
    """Reject the start event, then forward later events to a real Collector."""

    def __init__(self, database_path: Path) -> None:
        self.repository = Repository(str(database_path))
        self.app = create_app(self.repository)
        self.start_rejected = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _forward(self, body: bytes) -> None:
                headers = [(key, value) for key, value in self.headers.items()]
                status, response_headers, response_body = _asgi_request(
                    owner.app,
                    self.command,
                    self.path,
                    body,
                    headers,
                )
                self.send_response(status)
                for key, value in response_headers:  # type: ignore[assignment]
                    self.send_header(key.decode("latin-1"), value.decode("latin-1"))
                self.end_headers()
                self.wfile.write(response_body)

            def do_GET(self) -> None:
                self._forward(b"")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                document = json.loads(body.decode("utf-8"))
                events = document.get("events", [])
                if any(event.get("event_type") == "span.started" for event in events):
                    owner.start_rejected.set()
                    response_body = b"synthetic terminal start rejection"
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return
                self._forward(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="issue15-drop-start", daemon=True)
        self.thread.start()
        self._closed = False

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> bool:
        if self._closed:
            return not self.thread.is_alive()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.state.tracemotive_collector.close()
        self._closed = True
        return not self.thread.is_alive()


class _ScriptedHTTPServer:
    def __init__(self, *, status: int, stall_first_request: bool = False) -> None:
        self.status = status
        self.stall_first_request = stall_first_request
        self.attempts = 0
        self.first_request_started = threading.Event()
        self.release_first_request = threading.Event()
        self.handler_done = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    self.rfile.read(length)
                    owner.attempts += 1
                    if owner.attempts == 1:
                        owner.first_request_started.set()
                        if owner.stall_first_request:
                            owner.release_first_request.wait(timeout=4)
                    response_body = (
                        b'{"accepted":1,"duplicates":0,"stale":0}'
                        if owner.status == 200
                        else b"synthetic failure"
                    )
                    self.send_response(owner.status)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    try:
                        self.wfile.write(response_body)
                    except OSError:
                        # The transport may close a timed-out or terminally
                        # failed attempt before this scripted response writes.
                        pass
                finally:
                    owner.handler_done.set()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="issue15-scripted", daemon=True)
        self.thread.start()
        self._closed = False

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> bool:
        if self._closed:
            return not self.thread.is_alive()
        self.release_first_request.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.handler_done.wait(timeout=2)
        self._closed = True
        return not self.thread.is_alive()


def _http_json(endpoint: str, method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
    data = None if payload is None else _canonical_json_dumps(payload).encode("utf-8")
    request = Request(
        endpoint + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with _OPENER.open(request, timeout=3) as response:
            return int(response.status), response.read()
    except HTTPError as error:
        return int(error.code), error.read()


def _closed_loopback_endpoint() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()
    return f"http://127.0.0.1:{port}"


def _fixture_event(event_type: str, payload: Trace | Span, event_id: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "emitted_at": "2026-08-10T13:00:00Z",
        "payload": payload.to_dict(),
    }


def _fixture_trace(trace_id: str, *, ended: bool = False) -> Trace:
    return Trace(
        AGENTLENS_SCHEMA_VERSION,
        trace_id,
        "Issue 15 out-of-order fixture",
        "2026-08-10T13:00:00.000000Z",
        "2026-08-10T13:00:01.000000Z" if ended else None,
        "ok" if ended else "unset",
        TraceSource("fixture", "1.0", "issue15", "0.1", None),
        {},
        {},
    )


def _fixture_span(trace_id: str, span_id: str) -> Span:
    return Span(
        AGENTLENS_SCHEMA_VERSION,
        trace_id,
        span_id,
        None,
        "llm",
        "llm.generate",
        "Out-of-order span",
        "2026-08-10T13:00:00.100000Z",
        "2026-08-10T13:00:00.900000Z",
        "ok",
        None,
        None,
        None,
        Capture(
            CaptureInfo("not_captured", "disabled", False),
            CaptureInfo("captured", None, False),
        ),
        SpanSource("fixture", "1.0", "issue15", "0.1", None, None, None),
        {},
        {},
        LLMDetails(
            "llm",
            "fixture",
            "offline-model",
            "offline-model",
            None,
            LLMUsage(0, 0, 0, 0, 0),
            ["stop"],
            None,
            None,
        ),
    )


class Issue15E2ETests(unittest.TestCase):
    def setUp(self) -> None:
        sdk._reset_for_tests()

    def tearDown(self) -> None:
        sdk._reset_for_tests()

    @staticmethod
    def _llm_details() -> LLMDetails:
        return LLMDetails(
            "llm",
            "fixture-provider",
            "offline-model",
            "offline-model",
            None,
            LLMUsage(0, 0, 0, 0, 0),
            ["stop"],
            None,
            None,
        )

    def _assert_deterministic_stack(self, database_path: Path, run_number: int) -> str:
        server = _LoopbackCollectorServer(database_path)
        trace_id = None
        incomplete_span_id = None
        try:
            tracemotive.configure(
                enabled=True,
                endpoint=server.endpoint,
                capture_content=True,
            )
            with tracemotive.trace(f"Issue 15 deterministic run {run_number}") as trace_value:
                self.assertIsNotNone(trace_value)
                trace_id = trace_value.trace_id
                with tracemotive.span(
                    "Agent root",
                    type="agent",
                    operation="agent.run",
                    details=AgentDetails("agent", "Fixture agent", "0.1"),
                ):
                    try:
                        with tracemotive.span(
                            "Initial LLM",
                            type="llm",
                            operation="llm.generate",
                            details=self._llm_details(),
                            input={"prompt": f"Bearer {_SECRET}", "api_key": _SECRET},
                        ):
                            raise RuntimeError("synthetic LLM fixture failure")
                    except RuntimeError:
                        pass

                    try:
                        with tracemotive.span(
                            "Lookup tool",
                            type="tool",
                            operation="tool.call",
                            details=ToolDetails("tool", "lookup_fixture", "call-15"),
                            input={"api_key": _SECRET, "city": "Tokyo"},
                        ) as failed_tool:
                            failed_tool.set_output({"result": f"Bearer {_SECRET}"})
                            raise RuntimeError("synthetic Tool fixture failure")
                    except RuntimeError:
                        pass

                    with tracemotive.span(
                        "Final LLM",
                        type="llm",
                        operation="llm.generate",
                        details=self._llm_details(),
                        input={"prompt": "safe final prompt"},
                    ) as final_llm:
                        final_llm.set_output(None)

                    incomplete = tracemotive.span(
                        "Incomplete span",
                        type="custom",
                        input={"pending": True},
                    )
                    incomplete.__enter__()
                    incomplete_span_id = incomplete.span_id

            self.assertTrue(tracemotive.flush(), "public SDK flush did not reach a terminal outcome")
            self.assertIsNotNone(trace_id)
            self.assertGreaterEqual(
                sum(path == "/api/v1/ingest" for method, path in server.requests if method == "POST"),
                1,
            )

            status, list_body = _http_json(server.endpoint, "GET", "/api/v1/traces?name=" + f"Issue%2015%20deterministic%20run%20{run_number}")
            self.assertEqual(status, 200)
            trace_list = json.loads(list_body)
            self.assertEqual(len(trace_list["items"]), 1)
            self.assertEqual(trace_list["items"][0]["trace_id"], trace_id)
            self.assertEqual(trace_list["items"][0]["status"], "ok")
            self.assertEqual(trace_list["items"][0]["span_count"], 5)
            self.assertEqual(trace_list["items"][0]["error_count"], 2)

            status, detail_body = _http_json(server.endpoint, "GET", f"/api/v1/traces/{trace_id}")
            self.assertEqual(status, 200)
            detail = json.loads(detail_body)
            self.assertEqual(detail["trace"]["name"], f"Issue 15 deterministic run {run_number}")
            self.assertEqual(detail["trace"]["status"], "ok")
            self.assertEqual(detail["stats"]["span_count"], 5)
            self.assertEqual(detail["stats"]["error_count"], 2)
            self.assertEqual(detail["stats"]["llm_call_count"], 2)
            self.assertEqual(detail["stats"]["input_tokens"], 0)
            self.assertEqual(detail["stats"]["output_tokens"], 0)

            status, spans_body = _http_json(server.endpoint, "GET", f"/api/v1/traces/{trace_id}/spans")
            self.assertEqual(status, 200)
            spans = [item["span"] for item in json.loads(spans_body)["items"]]
            by_name = {span["name"]: span for span in spans}
            self.assertEqual(set(by_name), {"Agent root", "Initial LLM", "Lookup tool", "Final LLM", "Incomplete span"})
            self.assertIsNone(by_name["Agent root"]["parent_span_id"])
            for child_name in ("Initial LLM", "Lookup tool", "Final LLM", "Incomplete span"):
                self.assertEqual(by_name[child_name]["parent_span_id"], by_name["Agent root"]["span_id"])
            self.assertEqual(by_name["Initial LLM"]["status"], "error")
            self.assertEqual(by_name["Lookup tool"]["status"], "error")
            self.assertTrue(by_name["Lookup tool"]["capture"]["output"]["redacted"])
            self.assertEqual(by_name["Final LLM"]["status"], "ok")
            self.assertIsNone(by_name["Incomplete span"]["ended_at"])
            self.assertEqual(by_name["Incomplete span"]["status"], "unset")
            self.assertEqual(by_name["Incomplete span"]["capture"]["output"], {
                "state": "not_captured",
                "reason": "not_yet_available",
                "redacted": False,
            })
            self.assertTrue(by_name["Initial LLM"]["capture"]["input"]["redacted"])
            self.assertTrue(by_name["Lookup tool"]["capture"]["input"]["redacted"])
            self.assertEqual(by_name["Final LLM"]["output"], None)
            self.assertEqual(by_name["Final LLM"]["capture"]["output"]["state"], "captured")
            self.assertEqual(by_name["Final LLM"]["capture"]["output"]["reason"], None)

            status, incomplete_body = _http_json(
                server.endpoint,
                "GET",
                f"/api/v1/traces/{trace_id}/spans/{incomplete_span_id}",
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(incomplete_body)["span"]["status"], "unset")
            self.assertNotIn(_SECRET.encode("utf-8"), list_body + detail_body + spans_body + incomplete_body)
        finally:
            sdk._reset_for_tests()
            self.assertTrue(server.close())

        restarted = _LoopbackCollectorServer(database_path)
        try:
            status, restarted_body = _http_json(restarted.endpoint, "GET", f"/api/v1/traces/{trace_id}")
            self.assertEqual(status, 200)
            restarted_trace = json.loads(restarted_body)
            self.assertEqual(restarted_trace["trace"]["trace_id"], trace_id)
            self.assertEqual(restarted_trace["trace"]["status"], "ok")
            self.assertEqual(restarted_trace["stats"]["span_count"], 5)
        finally:
            self.assertTrue(restarted.close())
        self.assertIsNotNone(trace_id)
        return trace_id

    def test_deterministic_full_stack_repeats_and_survives_db_restart(self) -> None:
        trace_ids: list[str] = []
        for run_number in (1, 2):
            with tempfile.TemporaryDirectory(prefix="tracemotive-issue15-") as temporary_directory:
                trace_ids.append(
                    self._assert_deterministic_stack(
                        Path(temporary_directory) / "tracemotive.sqlite",
                        run_number,
                    )
                )
        self.assertNotEqual(trace_ids[0], trace_ids[1])

    def test_redaction_provenance_survives_terminally_dropped_start(self) -> None:
        for repeat in range(2):
            with self.subTest(repeat=repeat):
                with tempfile.TemporaryDirectory(prefix="tracemotive-issue15-drop-start-") as temporary_directory:
                    server = _DropStartedCollectorServer(Path(temporary_directory) / "tracemotive.sqlite")
                    trace_id = None
                    span_id = None
                    try:
                        tracemotive.configure(
                            enabled=True,
                            endpoint=server.endpoint,
                            capture_content=True,
                        )
                        with tracemotive.trace(f"Issue 15 dropped start {repeat}") as trace_value:
                            self.assertIsNotNone(trace_value)
                            trace_id = trace_value.trace_id
                            self.assertTrue(tracemotive.flush())
                            with tracemotive.span(
                                "Dropped-start span",
                                type="custom",
                                input={"api_key": _SECRET},
                            ) as span_value:
                                span_id = span_value.span_id
                                self.assertTrue(server.start_rejected.wait(timeout=2))
                        self.assertTrue(tracemotive.flush(2))

                        self.assertIsNotNone(trace_id)
                        self.assertIsNotNone(span_id)
                        status, body = _http_json(
                            server.endpoint,
                            "GET",
                            f"/api/v1/traces/{trace_id}/spans/{span_id}",
                        )
                        self.assertEqual(status, 200)
                        span_body = json.loads(body)["span"]
                        self.assertEqual(span_body["input"], {"api_key": "[REDACTED]"})
                        self.assertEqual(
                            span_body["capture"]["input"],
                            {"state": "captured", "reason": None, "redacted": True},
                        )
                        self.assertNotIn(_SECRET, body.decode("utf-8"))
                    finally:
                        sdk._reset_for_tests()
                        self.assertTrue(server.close())

    def test_real_http_duplicate_and_out_of_order_delivery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tracemotive-issue15-order-") as temporary_directory:
            server = _LoopbackCollectorServer(Path(temporary_directory) / "tracemotive.sqlite")
            try:
                trace = _fixture_trace(_TRACE_ID)
                span = _fixture_span(_TRACE_ID, _SPAN_ID)
                span_event = _fixture_event(
                    "span.ended",
                    span,
                    "00000000-0000-4000-8000-000000000151",
                )
                trace_event = _fixture_event(
                    "trace.started",
                    trace,
                    "00000000-0000-4000-8000-000000000152",
                )
                status, body = _http_json(
                    server.endpoint,
                    "POST",
                    "/api/v1/ingest",
                    {"protocol_version": 1, "events": [span_event]},
                )
                self.assertEqual((status, json.loads(body)), (200, {"accepted": 1, "duplicates": 0, "stale": 0}))
                status, body = _http_json(
                    server.endpoint,
                    "POST",
                    "/api/v1/ingest",
                    {"protocol_version": 1, "events": [span_event]},
                )
                self.assertEqual((status, json.loads(body)), (200, {"accepted": 0, "duplicates": 1, "stale": 0}))
                status, body = _http_json(
                    server.endpoint,
                    "POST",
                    "/api/v1/ingest",
                    {"protocol_version": 1, "events": [trace_event]},
                )
                self.assertEqual((status, json.loads(body)), (200, {"accepted": 1, "duplicates": 0, "stale": 0}))
                status, body = _http_json(server.endpoint, "GET", f"/api/v1/traces/{_TRACE_ID}/spans")
                self.assertEqual(status, 200)
                self.assertEqual(len(json.loads(body)["items"]), 1)
            finally:
                self.assertTrue(server.close())

    def test_collector_unavailable_does_not_change_application_result_or_hang(self) -> None:
        endpoint = _closed_loopback_endpoint()
        tracemotive.configure(enabled=True, endpoint=endpoint)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "application failure"):
            with tracemotive.trace("collector unavailable application"):
                raise RuntimeError("application failure")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn(tracemotive.flush(0.1), (True, False))

    def test_timeout_isolated_from_application_and_flush_deadline_is_bounded(self) -> None:
        server = _ScriptedHTTPServer(status=200, stall_first_request=True)
        try:
            tracemotive.configure(enabled=True, endpoint=server.endpoint)
            started = time.monotonic()
            with tracemotive.trace("timeout application"):
                pass
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(server.first_request_started.wait(timeout=2))
            flush_started = time.monotonic()
            self.assertFalse(tracemotive.flush(0.05))
            self.assertLess(time.monotonic() - flush_started, 0.5)
        finally:
            server.release_first_request.set()
            sdk._reset_for_tests()
            self.assertTrue(server.close())

    def test_queue_overflow_is_drop_newest_and_does_not_fail_application(self) -> None:
        server = _ScriptedHTTPServer(status=500, stall_first_request=True)
        try:
            tracemotive.configure(enabled=True, endpoint=server.endpoint)
            with tracemotive.trace("queue pressure application"):
                self.assertTrue(server.first_request_started.wait(timeout=2))
                for index in range(QUEUE_CAPACITY // 2 + 128):
                    with tracemotive.span(f"overflow-{index}"):
                        pass
                transport = sdk._transport_sink
                self.assertIsNotNone(transport)
                self.assertEqual(transport.queue_size, QUEUE_CAPACITY)
        finally:
            server.release_first_request.set()
            sdk._reset_for_tests()
            self.assertTrue(server.close())

    def _assert_scripted_status(self, status_code: int, expected_attempts: int, expected_sleeps: list[float]) -> None:
        server = _ScriptedHTTPServer(status=status_code)
        sleeps: list[float] = []
        transport = Transport(server.endpoint, sleeper=sleeps.append)
        sdk._set_event_sink(transport)
        try:
            tracemotive.configure(enabled=True)
            with self.assertRaisesRegex(RuntimeError, "application failure"):
                with tracemotive.trace(f"HTTP {status_code} application"):
                    raise RuntimeError("application failure")
            self.assertTrue(tracemotive.flush(2))
            self.assertEqual(server.attempts, expected_attempts)
            self.assertEqual(sleeps, expected_sleeps)
            self.assertLessEqual(server.attempts, MAX_ATTEMPTS)
        finally:
            transport.shutdown(timeout_seconds=2)
            self.assertTrue(server.close())
            sdk._reset_for_tests()

    def test_retryable_http_failure_uses_three_attempts_and_preserves_application_exception(self) -> None:
        self._assert_scripted_status(500, MAX_ATTEMPTS, list(RETRY_BACKOFF_SECONDS))

    def test_terminal_http_failure_is_not_retried_and_preserves_application_exception(self) -> None:
        self._assert_scripted_status(400, 1, [])


if __name__ == "__main__":
    unittest.main()
