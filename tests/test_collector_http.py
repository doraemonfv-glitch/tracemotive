import asyncio
import unittest

from agentlens.canonical.models import CaptureInfo, _canonical_json_dumps
from agentlens.collector import (
    DEFAULT_BIND_HOST,
    MAX_EVENT_BYTES,
    MAX_REQUEST_BYTES,
    create_app,
)
from agentlens.storage import Repository
from tests.test_collector import (
    OTHER_TRACE_ID,
    TRACE_ID,
    batch,
    event,
    make_span,
    make_trace,
)


async def _asgi_request(app, body, *, method="POST", path="/api/v1/ingest"):
    messages = []
    delivered = False

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
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
    response_start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return response_start["status"], response_body


def http_request(app, body, **kwargs):
    status, response_body = asyncio.run(_asgi_request(app, body, **kwargs))
    return status, response_body, _canonical_json_dumps


class CollectorHTTPTests(unittest.TestCase):
    def test_valid_request_returns_exact_success_contract(self):
        with Repository() as repository:
            app = create_app(repository, clock=lambda: 123)
            status, body, _ = http_request(
                app,
                _canonical_json_dumps(batch(event("trace.started", make_trace()))).encode("utf-8"),
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"accepted":1,"duplicates":0,"stale":0}')

    def test_malformed_json_and_invalid_canonical_event_return_frozen_errors(self):
        with Repository() as repository:
            app = create_app(repository)
            status, body, _ = http_request(app, b"{not-json")
            self.assertEqual(status, 400)
            self.assertEqual(body, b'{"error":{"code":"malformed_json","message":"malformed JSON"}}')

            invalid = event("span.started", make_span())
            invalid["payload"]["span_id"] = "invalid"
            status, body, _ = http_request(
                app,
                _canonical_json_dumps(batch(invalid)).encode("utf-8"),
            )
            self.assertEqual(status, 422)
            self.assertEqual(
                body,
                b'{"error":{"code":"validation_error","message":"invalid span_id","event_index":0,"field":"payload.span_id"}}',
            )

    def test_validation_errors_never_reflect_secret_bearing_unknown_keys_or_values(self):
        secret = "sk-live-RECOGNIZABLE-SECRET"
        unknown_key = f"Bearer {secret}"
        probes = []

        envelope_unknown = batch(event("trace.started", make_trace()))
        envelope_unknown[unknown_key] = "x"
        probes.append(_canonical_json_dumps(envelope_unknown).encode("utf-8"))

        payload_unknown_key = event("trace.started", make_trace())
        payload_unknown_key["payload"][unknown_key] = "x"
        probes.append(_canonical_json_dumps(batch(payload_unknown_key)).encode("utf-8"))

        payload_unknown_value = event("trace.started", make_trace())
        payload_unknown_value["payload"]["user_context"] = f"Bearer {secret}"
        probes.append(_canonical_json_dumps(batch(payload_unknown_value)).encode("utf-8"))

        api_key_probe = event("trace.started", make_trace())
        api_key_probe["payload"]["api_key-like secret"] = f"api_key={secret}"
        probes.append(_canonical_json_dumps(batch(api_key_probe)).encode("utf-8"))

        benign_unknown = event("trace.started", make_trace())
        benign_unknown["payload"]["ordinary_unknown_key"] = "ordinary value"
        probes.append(_canonical_json_dumps(batch(benign_unknown)).encode("utf-8"))

        with Repository() as repository:
            app = create_app(repository)
            for body in probes:
                status, response_body, _ = http_request(app, body)
                self.assertEqual(status, 422)
                self.assertNotIn(secret.encode("utf-8"), response_body)
                self.assertNotIn(unknown_key.encode("utf-8"), response_body)
                for table in ("traces", "spans", "span_io", "ingest_events"):
                    self.assertEqual(
                        repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )

            malformed_secret = f'{{"{unknown_key}":'.encode("utf-8")
            status, response_body, _ = http_request(app, malformed_secret)
            self.assertEqual(status, 400)
            self.assertNotIn(secret.encode("utf-8"), response_body)
            self.assertNotIn(unknown_key.encode("utf-8"), response_body)
            for table in ("traces", "spans", "span_io", "ingest_events"):
                self.assertEqual(
                    repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )

    def test_known_schema_validation_preserves_safe_field_path_without_invalid_value(self):
        invalid = event("span.started", make_span())
        invalid_value = "Bearer sk-live-RECOGNIZABLE-SECRET"
        invalid["payload"]["span_id"] = invalid_value
        with Repository() as repository:
            app = create_app(repository)
            status, body, _ = http_request(
                app,
                _canonical_json_dumps(batch(invalid)).encode("utf-8"),
            )
            self.assertEqual(status, 422)
            self.assertIn(b'"field":"payload.span_id"', body)
            self.assertIn(b'"message":"invalid span_id"', body)
            self.assertNotIn(invalid_value.encode("utf-8"), body)
            self.assertEqual(
                repository.connection.execute("SELECT COUNT(*) FROM ingest_events").fetchone()[0],
                0,
            )

    def test_event_and_entity_conflicts_return_409_without_partial_persistence(self):
        with Repository() as repository:
            app = create_app(repository, clock=lambda: 123)
            first = event(
                "trace.started",
                make_trace(),
                event_id="4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
            )
            status, _, _ = http_request(app, _canonical_json_dumps(batch(first)).encode("utf-8"))
            self.assertEqual(status, 200)

            event_conflict = dict(first)
            event_conflict["emitted_at"] = "2026-08-10T13:00:00.000001Z"
            status, body, _ = http_request(
                app,
                _canonical_json_dumps(batch(event_conflict)).encode("utf-8"),
            )
            self.assertEqual(status, 409)
            self.assertEqual(
                body,
                b'{"error":{"code":"event_id_conflict","message":"event_id was reused with different content","event_index":0,"field":"event_id"}}',
            )

            first_final = event(
                "span.ended",
                make_span(
                    trace_id=OTHER_TRACE_ID,
                    ended=True,
                    input_value={"request": "first"},
                    input_capture=CaptureInfo("captured", None, False),
                ),
            )
            second_final = event(
                "span.ended",
                make_span(
                    trace_id=OTHER_TRACE_ID,
                    ended=True,
                    input_value={"request": "second"},
                    input_capture=CaptureInfo("captured", None, False),
                ),
            )
            status, body, _ = http_request(
                app,
                _canonical_json_dumps(batch(first_final, second_final)).encode("utf-8"),
            )
            self.assertEqual(status, 409)
            self.assertIn(b'"code":"entity_conflict"', body)
            self.assertIsNone(repository.get_span(OTHER_TRACE_ID, "00f067aa0ba902b7"))
            self.assertIsNone(repository.get_ingest_event(second_final["event_id"]))

    def test_exact_individual_limit_is_accepted_and_one_byte_over_rejects_whole_request(self):
        with Repository() as repository:
            app = create_app(repository)
            collector = app.state.agentlens_collector
            base = event("trace.started", make_trace(metadata={"blob": ""}))
            base_size = collector._validate_event(base, 0).serialized_size
            exact = event(
                "trace.started",
                make_trace(metadata={"blob": "x" * (MAX_EVENT_BYTES - base_size)}),
            )
            exact_body = _canonical_json_dumps(batch(exact)).encode("utf-8")
            status, _, _ = http_request(app, exact_body)
            self.assertEqual(status, 200)

            over = event(
                "trace.started",
                make_trace(OTHER_TRACE_ID, metadata={"blob": "x" * (MAX_EVENT_BYTES - base_size + 1)}),
            )
            status, body, _ = http_request(app, _canonical_json_dumps(batch(over)).encode("utf-8"))
            self.assertEqual(status, 413)
            self.assertEqual(body, b"{}")
            self.assertIsNone(repository.get_trace(OTHER_TRACE_ID))

    def test_raw_request_limit_is_exact_and_oversized_batch_does_not_persist_early_event(self):
        with Repository() as repository:
            app = create_app(repository, clock=lambda: 123)
            base = _canonical_json_dumps(batch()).encode("utf-8")
            exact_body = base + b" " * (MAX_REQUEST_BYTES - len(base))
            status, body, _ = http_request(app, exact_body)
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"accepted":0,"duplicates":0,"stale":0}')

            status, body, _ = http_request(app, exact_body + b" ")
            self.assertEqual(status, 413)
            self.assertEqual(body, b"{}")

            valid = event("trace.started", make_trace(OTHER_TRACE_ID))
            base_event = event("trace.started", make_trace(TRACE_ID, metadata={"blob": ""}))
            base_size = app.state.agentlens_collector._validate_event(base_event, 0).serialized_size
            oversized = event(
                "trace.started",
                make_trace(TRACE_ID, metadata={"blob": "x" * (MAX_EVENT_BYTES - base_size + 1)}),
            )
            status, _, _ = http_request(
                app,
                _canonical_json_dumps(batch(valid, oversized)).encode("utf-8"),
            )
            self.assertEqual(status, 413)
            self.assertIsNone(repository.get_trace(OTHER_TRACE_ID))

    def test_invalid_later_event_rolls_back_earlier_valid_event(self):
        with Repository() as repository:
            app = create_app(repository, clock=lambda: 123)
            valid = event("trace.started", make_trace())
            invalid = event("trace.started", make_trace(OTHER_TRACE_ID))
            invalid["payload"]["trace_id"] = "invalid"
            status, body, _ = http_request(
                app,
                _canonical_json_dumps(batch(valid, invalid)).encode("utf-8"),
            )
            self.assertEqual(status, 422)
            self.assertIn(b'"event_index":1', body)
            self.assertIsNone(repository.get_trace(TRACE_ID))
            self.assertIsNone(repository.get_trace(OTHER_TRACE_ID))

    def test_app_rejects_non_loopback_binding(self):
        with self.assertRaises(ValueError):
            create_app(bind_host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
