import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
import unittest
from unittest.mock import patch

import agentlens
from agentlens import sdk
from agentlens.canonical.models import _canonical_json_dumps
from agentlens.storage import Repository
from agentlens.transport import (
    ATTEMPT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_BATCH_EVENTS,
    MAX_EVENT_BYTES,
    MAX_REQUEST_BYTES,
    QUEUE_CAPACITY,
    RETRY_BACKOFF_SECONDS,
    SHUTDOWN_BUDGET_SECONDS,
    Transport,
)
from tests.test_collector import event, make_trace


class ScriptedTransport(Transport):
    def __init__(self, outcomes=(), *, sleeper=None):
        self.outcomes = list(outcomes)
        self.attempts = []
        self.bodies = []
        self.timeouts = []
        self.sleeps = []
        super().__init__(
            start=False,
            sleeper=(sleeper if sleeper is not None else self.sleeps.append),
        )

    def _send_attempt(self, body, timeout_seconds):
        self.attempts.append(len(self.attempts) + 1)
        self.bodies.append(body)
        self.timeouts.append(timeout_seconds)
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def queued_event(index, *, blob=""):
    return event(
        "trace.started",
        make_trace(
            trace_id=f"{index + 1:032x}",
            metadata={"blob": blob},
        ),
    )


class TransportTests(unittest.TestCase):
    def tearDown(self):
        transport = getattr(self, "transport", None)
        if transport is not None:
            transport.shutdown(timeout_seconds=0.25)

    def _default_flush_probe(self, mode):
        created = []

        class RecordingTransport:
            def __init__(inner_self, endpoint):
                inner_self.endpoint = endpoint
                inner_self.events = []
                inner_self.flush_calls = []
                created.append(inner_self)

            def emit(inner_self, value):
                inner_self.events.append(value)
                return True

            @property
            def flush(inner_self):
                if mode == "lookup":
                    raise RuntimeError("flush lookup failure")

                def invoke(timeout_seconds):
                    inner_self.flush_calls.append(timeout_seconds)
                    if mode == "invoke":
                        raise RuntimeError("flush invocation failure")
                    if mode == "lock":
                        acquired = []

                        def acquire_sdk_lock():
                            with sdk._state_lock:
                                acquired.append(True)

                        thread = threading.Thread(target=acquire_sdk_lock)
                        thread.start()
                        thread.join(1)
                        if thread.is_alive():
                            return False
                    return mode == "true" or mode == "lock"

                return invoke

            def shutdown(inner_self):
                return True

        sdk._reset_for_tests()
        try:
            with patch.object(sdk, "LocalTransport", RecordingTransport):
                agentlens.configure(enabled=True, endpoint="http://127.0.0.1:9876")
                with agentlens.trace("flush-probe"):
                    pass
                result = agentlens.flush(0.5)
            return result, created[0]
        finally:
            sdk._reset_for_tests()

    def test_frozen_constants_are_exact(self):
        self.assertEqual(QUEUE_CAPACITY, 2048)
        self.assertEqual(MAX_BATCH_EVENTS, 64)
        self.assertEqual(MAX_EVENT_BYTES, 1_048_576)
        self.assertEqual(MAX_REQUEST_BYTES, 4_194_304)
        self.assertEqual(ATTEMPT_TIMEOUT_SECONDS, 1.0)
        self.assertEqual(MAX_ATTEMPTS, 3)
        self.assertEqual(RETRY_BACKOFF_SECONDS, (0.1, 0.25))
        self.assertEqual(SHUTDOWN_BUDGET_SECONDS, 2.0)

    def test_queue_capacity_is_exact_and_overflow_drops_newest(self):
        self.transport = Transport(start=False)
        accepted = [self.transport.emit(queued_event(index)) for index in range(QUEUE_CAPACITY)]
        newest = queued_event(QUEUE_CAPACITY)

        self.assertTrue(all(accepted))
        self.assertFalse(self.transport.emit(newest))
        self.assertEqual(self.transport.queue_size, QUEUE_CAPACITY)
        self.assertEqual(self.transport.pending_count, QUEUE_CAPACITY)
        first = json.loads(self.transport._queue[0].serialized)
        last = json.loads(self.transport._queue[-1].serialized)
        self.assertEqual(first["payload"]["trace_id"], f"{1:032x}")
        self.assertEqual(last["payload"]["trace_id"], f"{QUEUE_CAPACITY:032x}")

    def test_concurrent_producers_have_one_atomic_outcome_each(self):
        self.transport = Transport(start=False)
        barrier = threading.Barrier(16)
        results = []
        result_lock = threading.Lock()

        def producer(index):
            barrier.wait()
            accepted = self.transport.emit(queued_event(index))
            with result_lock:
                results.append(accepted)

        threads = [threading.Thread(target=producer, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertEqual(len(results), 16)
        self.assertEqual(sum(results), 16)
        self.assertEqual(self.transport.queue_size, 16)
        positions = [
            json.loads(item.serialized)["payload"]["trace_id"]
            for item in self.transport._queue
        ]
        self.assertEqual(len(set(positions)), 16)

    def test_fifo_batches_are_limited_to_64_and_keep_order(self):
        self.transport = ScriptedTransport()
        for index in range(MAX_BATCH_EVENTS + 1):
            self.assertTrue(self.transport.emit(queued_event(index)))
        self.transport.start()

        self.assertTrue(self.transport.flush(2))
        self.assertEqual(len(self.transport.bodies), 2)
        batches = [json.loads(body)["events"] for body in self.transport.bodies]
        self.assertEqual([len(batch) for batch in batches], [MAX_BATCH_EVENTS, 1])
        ordered_ids = [
            item["payload"]["trace_id"]
            for batch in batches
            for item in batch
        ]
        self.assertEqual(ordered_ids, [f"{index + 1:032x}" for index in range(MAX_BATCH_EVENTS + 1)])

    def test_request_size_limit_starts_the_next_fifo_batch(self):
        blob = "x" * 900_000
        self.transport = ScriptedTransport()
        for index in range(5):
            self.assertTrue(self.transport.emit(queued_event(index, blob=blob)))
        self.transport.start()

        self.assertTrue(self.transport.flush(2))
        batches = [json.loads(body)["events"] for body in self.transport.bodies]
        self.assertEqual([len(batch) for batch in batches], [4, 1])
        self.assertLessEqual(len(self.transport.bodies[0]), MAX_REQUEST_BYTES)
        serialized_events = [
            _canonical_json_dumps(queued_event(index, blob=blob)).encode("utf-8")
            for index in range(5)
        ]
        five_event_body = (
            b'{"events":['
            + b",".join(serialized_events)
            + b'],"protocol_version":1}'
        )
        self.assertGreater(len(five_event_body), MAX_REQUEST_BYTES)

    def test_retryable_statuses_and_network_timeout_use_three_attempts_and_backoff(self):
        self.transport = ScriptedTransport(
            [500, 429, 200, socket.timeout("stall")],
        )
        self.assertTrue(self.transport.emit(queued_event(0)))
        self.transport.start()

        self.assertTrue(self.transport.flush(2))
        self.assertEqual(self.transport.attempts, [1, 2, 3])
        self.assertEqual(self.transport.sleeps, list(RETRY_BACKOFF_SECONDS))
        self.assertEqual(self.transport.timeouts, [ATTEMPT_TIMEOUT_SECONDS] * 3)

    def test_network_failure_and_timeout_are_retryable(self):
        for failure in (ConnectionError("connection"), TimeoutError("timeout")):
            with self.subTest(failure=type(failure).__name__):
                self.transport = ScriptedTransport([failure, 200])
                self.assertTrue(self.transport.emit(queued_event(1)))
                self.transport.start()
                self.assertTrue(self.transport.flush(2))
                self.assertEqual(len(self.transport.attempts), 2)
                self.transport.shutdown(timeout_seconds=0.25)
                self.transport = None

    def test_non_retryable_4xx_is_terminal_after_one_attempt(self):
        self.transport = ScriptedTransport([400, 200])
        self.assertTrue(self.transport.emit(queued_event(0)))
        self.transport.start()

        self.assertTrue(self.transport.flush(2))
        self.assertEqual(self.transport.attempts, [1])
        self.assertEqual(self.transport.sleeps, [])

    def test_public_sdk_flush_uses_default_transport_and_preserves_failure_isolation(self):
        for mode, expected in (
            ("false", False),
            ("true", True),
            ("lookup", False),
            ("invoke", False),
            ("lock", True),
        ):
            with self.subTest(mode=mode):
                result, transport = self._default_flush_probe(mode)
                self.assertEqual(result, expected)
                if mode != "lookup":
                    self.assertEqual(transport.flush_calls, [0.5])

    def test_http_status_is_classified_before_body_read_failure(self):
        for status in (400, 409, 413, 422, 408, 429, 500):
            with self.subTest(status=status):
                attempts = []
                responses = []
                sleeps = []

                class BodyFailureResponse:
                    def __init__(inner_self):
                        inner_self.status = status
                        inner_self.read_calls = 0
                        inner_self.close_calls = 0
                        responses.append(inner_self)

                    def read(inner_self):
                        inner_self.read_calls += 1
                        raise OSError("response body unavailable")

                    def close(inner_self):
                        inner_self.close_calls += 1

                class BodyFailureConnection:
                    sock = None

                    def __init__(inner_self, host, port, timeout):
                        attempts.append(inner_self)
                        inner_self.response = BodyFailureResponse()

                    def request(inner_self, method, path, *, body, headers):
                        return None

                    def getresponse(inner_self):
                        return inner_self.response

                    def close(inner_self):
                        return None

                transport = Transport(start=False, sleeper=sleeps.append)
                try:
                    with patch(
                        "agentlens.transport.http.client.HTTPConnection",
                        BodyFailureConnection,
                    ):
                        self.assertTrue(transport.emit(queued_event(status)))
                        transport.start()
                        self.assertTrue(transport.flush(2))
                finally:
                    transport.shutdown(timeout_seconds=0.25)

                expected_attempts = 1 if status in {400, 409, 413, 422} else 3
                self.assertEqual(len(attempts), expected_attempts)
                self.assertEqual(sleeps, [] if expected_attempts == 1 else list(RETRY_BACKOFF_SECONDS))
                self.assertTrue(all(response.read_calls == 0 for response in responses))
                self.assertTrue(all(response.close_calls == 1 for response in responses))

    def test_third_retryable_failure_is_terminal_without_fourth_attempt(self):
        self.transport = ScriptedTransport([408, 500, 599, 200])
        self.assertTrue(self.transport.emit(queued_event(0)))
        self.transport.start()

        self.assertTrue(self.transport.flush(2))
        self.assertEqual(self.transport.attempts, [1, 2, 3])
        self.assertEqual(self.transport.sleeps, list(RETRY_BACKOFF_SECONDS))

    def test_flush_cutoff_excludes_events_enqueued_after_cutoff(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingTransport(ScriptedTransport):
            def _send_attempt(inner_self, body, timeout_seconds):
                inner_self.attempts.append(len(inner_self.attempts) + 1)
                if len(inner_self.attempts) == 1:
                    started.set()
                    release.wait(2)
                else:
                    second_release.wait(10)
                return 200

        self.transport = BlockingTransport()
        second_release = threading.Event()
        cutoff_ready = threading.Event()
        self.transport._flush_cutoff_hook = lambda cutoff: cutoff_ready.set()
        self.assertTrue(self.transport.emit(queued_event(0)))
        self.transport.start()
        self.assertTrue(started.wait(2))

        result = []

        def do_flush():
            result.append(self.transport.flush(2))

        flush_thread = threading.Thread(target=do_flush)
        flush_thread.start()
        self.assertTrue(cutoff_ready.wait(2))
        self.assertTrue(self.transport.emit(queued_event(1)))
        release.set()
        flush_thread.join(2)

        self.assertEqual(result, [True])
        self.assertEqual(self.transport.pending_count, 1)
        second_release.set()
        self.assertTrue(self.transport.flush(2))

    def test_repeated_and_concurrent_flushes_are_safe(self):
        self.transport = ScriptedTransport()
        for index in range(3):
            self.assertTrue(self.transport.emit(queued_event(index)))
        self.transport.start()
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(self.transport.flush(2)))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(results, [True] * 4)
        self.assertTrue(self.transport.flush(0))

    def test_shutdown_closes_acceptance_and_is_bounded(self):
        self.transport = ScriptedTransport([ConnectionError("offline")] * 20)
        self.assertTrue(self.transport.emit(queued_event(0)))
        self.transport.start()
        started = time.monotonic()
        self.assertTrue(self.transport.shutdown(timeout_seconds=0.25))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertFalse(self.transport.emit(queued_event(1)))
        self.assertEqual(self.transport.state, "stopped")
        self.assertEqual(self.transport.pending_count, 0)

    def test_transport_keeps_only_sanitized_serialized_content(self):
        secret = "sk-live-RECOGNIZABLE-SECRET"
        sanitized = "[REDACTED]"
        value = queued_event(0, blob=sanitized)
        self.transport = Transport(start=False)
        self.assertTrue(self.transport.emit(value))
        serialized = self.transport._queue[0].serialized
        self.assertIn(sanitized.encode("utf-8"), serialized)
        self.assertNotIn(secret.encode("utf-8"), serialized)

    def test_loopback_endpoint_validation_rejects_remote_and_https_targets(self):
        for endpoint in (
            "https://127.0.0.1:8765",
            "http://192.0.2.1:8765",
            "http://collector.example:8765",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    Transport(endpoint, start=False)

    def test_worker_startup_failure_isolated_and_does_not_leave_pending_events(self):
        with patch("agentlens.transport.threading.Thread.start", side_effect=RuntimeError("startup")):
            self.transport = Transport()
        self.assertEqual(self.transport.state, "stopped")
        self.assertFalse(self.transport.emit(queued_event(0)))
        self.assertTrue(self.transport.flush(0))

    def test_enqueue_serialization_failure_is_dropped_without_queue_ownership(self):
        self.transport = Transport(start=False)
        invalid = {"event_id": object(), "event_type": "trace.started"}
        self.assertFalse(self.transport.emit(invalid))
        self.assertEqual(self.transport.queue_size, 0)
        self.assertEqual(self.transport.pending_count, 0)

    def test_response_parsing_failure_isolated_and_retried(self):
        self.transport = ScriptedTransport([ValueError("invalid response"), 200])
        self.assertTrue(self.transport.emit(queued_event(0)))
        self.transport.start()
        self.assertTrue(self.transport.flush(2))
        self.assertEqual(len(self.transport.attempts), 2)

    def test_worker_shutdown_failure_is_bounded_and_does_not_escape(self):
        self.transport = ScriptedTransport()
        self.transport.start()
        worker = self.transport._worker
        self.assertIsNotNone(worker)
        with patch.object(worker, "join", side_effect=RuntimeError("join")):
            self.assertFalse(self.transport.shutdown(timeout_seconds=0.1))
        self.assertEqual(self.transport.state, "stopped")
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_sdk_default_sink_connects_to_issue_07_transport_without_raw_sink(self):
        result, transport = self._default_flush_probe("true")
        self.assertTrue(result)
        self.assertEqual(transport.endpoint, "http://127.0.0.1:9876")
        self.assertEqual(
            [item["event_type"] for item in transport.events],
            ["trace.started", "trace.ended"],
        )

    def test_attempt_timeout_is_passed_to_standard_library_http_client(self):
        calls = []

        class FakeSocket:
            def settimeout(self, value):
                calls.append(("socket-timeout", value))

        class FakeResponse:
            status = 200

            def read(self):
                return b'{"accepted":1,"duplicates":0,"stale":0}'

        class FakeConnection:
            sock = FakeSocket()

            def __init__(self, host, port, timeout):
                calls.append(("connection", host, port, timeout))

            def request(self, method, path, *, body, headers):
                calls.append((method, path, body, headers))

            def getresponse(self):
                return FakeResponse()

            def close(self):
                calls.append(("close",))

        self.transport = Transport(start=False)
        with patch("agentlens.transport.http.client.HTTPConnection", FakeConnection):
            status = self.transport._send_attempt(b"{}", ATTEMPT_TIMEOUT_SECONDS)
        self.assertEqual(status, 200)
        connection_call = next(call for call in calls if call[0] == "connection")
        self.assertEqual(connection_call[3], ATTEMPT_TIMEOUT_SECONDS)
        self.assertEqual(next(call for call in calls if call[0] == "POST")[1], "/api/v1/ingest")

    def test_actual_collector_accepts_transport_body(self):
        repository = Repository()
        received = []
        collector = None

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                body = self.rfile.read(length)
                received.append((self.path, body))
                result = collector.ingest_json(body)
                response = _canonical_json_dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format, *args):
                return

        from agentlens.collector import Collector

        collector = Collector(repository)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.transport = Transport(f"http://127.0.0.1:{server.server_port}")
            self.assertTrue(self.transport.emit(event("trace.started", make_trace())))
            self.assertTrue(self.transport.flush(2))
            self.assertEqual(received[0][0], "/api/v1/ingest")
            self.assertIsNotNone(repository.get_trace(make_trace().trace_id))
            self.assertEqual(repository.connection.execute("SELECT COUNT(*) FROM traces").fetchone()[0], 1)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)
            collector.close()
            self.transport.shutdown(timeout_seconds=0.25)
            self.transport = None


if __name__ == "__main__":
    unittest.main()
