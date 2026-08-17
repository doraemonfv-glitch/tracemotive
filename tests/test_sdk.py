import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

import tracemotive
from tracemotive import sdk
from tracemotive.canonical import LLMDetails, LLMUsage
from tracemotive.privacy import MAX_CONTENT_BYTES


TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,6}Z$"
)


class Sink:
    def __init__(self):
        self.events = []
        self.flush_calls = []
        self.lock = threading.Lock()

    def emit(self, event):
        with self.lock:
            self.events.append(event)

    def flush(self, timeout_seconds):
        self.flush_calls.append(timeout_seconds)
        return True


class FailingSink:
    def emit(self, event):
        raise RuntimeError("sink failed")

    def flush(self, timeout_seconds):
        raise RuntimeError("flush failed")


class LookupFailSink:
    @property
    def flush(self):
        raise RuntimeError("flush lookup failed")


class RecordThenFailSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)
        raise RuntimeError("emit failed")


class BadError(Exception):
    def __str__(self):
        raise RuntimeError("stringification failed")


class NameFailsMeta(type):
    def __getattribute__(cls, name):
        if name == "__name__":
            raise RuntimeError("name lookup failed")
        return super().__getattribute__(name)


class NameFailsError(Exception, metaclass=NameFailsMeta):
    pass


class NameAndStringFailError(Exception, metaclass=NameFailsMeta):
    def __str__(self):
        raise RuntimeError("stringification failed")


class ExplosiveSource:
    def __str__(self):
        raise AssertionError("TraceMotive inspected disabled content")

    def __repr__(self):
        raise AssertionError("TraceMotive represented disabled content")

    def __iter__(self):
        raise AssertionError("TraceMotive iterated disabled content")


class SDKTests(unittest.TestCase):
    def setUp(self):
        sdk._reset_for_tests()
        self.sink = Sink()
        sdk._set_event_sink(self.sink)

    def tearDown(self):
        sdk._reset_for_tests()

    def _events(self, event_type):
        return [event for event in self.sink.events if event["event_type"] == event_type]

    def test_disabled_by_default_is_a_noop_and_preserves_user_exceptions(self):
        body_ran = []
        with tracemotive.trace("disabled"):
            body_ran.append(True)
            with tracemotive.span("ignored") as span_handle:
                span_handle.set_output({"password": "do-not-capture"})
        self.assertEqual(body_ran, [True])
        self.assertEqual(self.sink.events, [])
        with self.assertRaisesRegex(RuntimeError, "user failure"):
            with tracemotive.trace("disabled-error"):
                raise RuntimeError("user failure")
        self.assertEqual(self.sink.events, [])

    def test_configure_validates_loopback_and_freezes_after_first_event(self):
        with self.assertRaises(tracemotive.TraceMotiveConfigurationError):
            tracemotive.configure(enabled=True, endpoint="https://127.0.0.1:8765")
        with self.assertRaises(tracemotive.TraceMotiveConfigurationError):
            tracemotive.configure(enabled=True, endpoint="http://192.0.2.1:8765")
        with self.assertRaises(tracemotive.TraceMotiveConfigurationError):
            tracemotive.configure(enabled=True, endpoint="http://localhost.evil:8765")

        tracemotive.configure(enabled=True, endpoint="http://LOCALHOST:8765")
        with tracemotive.trace("freeze"):
            pass
        tracemotive.configure(enabled=True, endpoint="http://LOCALHOST:8765")
        with self.assertRaises(tracemotive.TraceMotiveConfigurationError):
            tracemotive.configure(enabled=True, capture_content=True, endpoint="http://LOCALHOST:8765")

    def test_first_event_and_configure_race_has_one_atomic_winner(self):
        tracemotive.configure(enabled=True)
        entered_event_creation = threading.Event()
        configure_started = threading.Event()
        release_event_creation = threading.Event()
        original_new_trace_id = sdk._new_trace_id
        event_result = []
        configure_result = []

        def blocked_trace_id():
            entered_event_creation.set()
            self.assertTrue(release_event_creation.wait(2))
            return original_new_trace_id()

        def create_event():
            try:
                with tracemotive.trace("race"):
                    pass
                event_result.append("completed")
            except BaseException as error:
                event_result.append(error)

        def reconfigure():
            configure_started.set()
            try:
                tracemotive.configure(enabled=True, capture_content=True)
            except BaseException as error:
                configure_result.append(error)
            else:
                configure_result.append("completed")

        with patch.object(sdk, "_new_trace_id", side_effect=blocked_trace_id):
            event_thread = threading.Thread(target=create_event)
            configure_thread = threading.Thread(target=reconfigure)
            event_thread.start()
            self.assertTrue(entered_event_creation.wait(2))
            configure_thread.start()
            self.assertTrue(configure_started.wait(2))
            release_event_creation.set()
            event_thread.join(2)
            configure_thread.join(2)

        self.assertEqual(event_result, ["completed"])
        self.assertEqual(len(configure_result), 1)
        self.assertIsInstance(configure_result[0], tracemotive.TraceMotiveConfigurationError)
        self.assertEqual(len(self._events("trace.started")), 1)

    def test_concurrent_identical_configure_calls_are_safe(self):
        barrier = threading.Barrier(2)

        def configure_identically():
            barrier.wait()
            tracemotive.configure(enabled=True, endpoint="http://127.0.0.1:8765")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: configure_identically(), (1, 2)))
        self.assertEqual(results, [None, None])
        with tracemotive.trace("after-identical-configure"):
            pass

    def test_concurrent_same_and_conflicting_configure_calls_after_freeze(self):
        tracemotive.configure(enabled=True)
        with tracemotive.trace("freeze-before-race"):
            pass
        barrier = threading.Barrier(2)

        def same_configuration():
            barrier.wait()
            try:
                tracemotive.configure(enabled=True)
            except BaseException as error:
                return error
            return "same"

        def conflicting_configuration():
            barrier.wait()
            try:
                tracemotive.configure(enabled=True, capture_content=True)
            except BaseException as error:
                return error
            return "conflicting"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(same_configuration), pool.submit(conflicting_configuration)]
            results = [future.result() for future in results]
        self.assertIn("same", results)
        conflict = next(result for result in results if result != "same")
        self.assertIsInstance(conflict, tracemotive.TraceMotiveConfigurationError)

    def test_trace_normal_and_exceptional_exit_have_explicit_status(self):
        tracemotive.configure(enabled=True)
        with tracemotive.trace("success", metadata={"request": "safe"}) as current:
            self.assertIsNotNone(current)
            self.assertIs(sdk.current_trace(), current)
        with tracemotive.trace("failure"):
            pass
        with self.assertRaisesRegex(ValueError, "original"):
            with tracemotive.trace("raised"):
                raise ValueError("original")

        started = self._events("trace.started")
        ended = self._events("trace.ended")
        self.assertEqual(len(started), 3)
        self.assertEqual(len(ended), 3)
        self.assertEqual(ended[0]["payload"]["status"], "ok")
        self.assertEqual(ended[1]["payload"]["status"], "ok")
        self.assertEqual(ended[2]["payload"]["status"], "error")
        self.assertEqual(started[0]["payload"]["source"]["integration"], "agentlens.manual")
        self.assertIsNone(started[0]["payload"]["source"]["framework"])

    def test_span_lifecycle_parenting_and_child_error_do_not_change_trace(self):
        tracemotive.configure(enabled=True)
        with tracemotive.trace("workflow") as current_trace:
            with tracemotive.span("outer") as outer:
                self.assertIs(sdk.current_span(), outer)
                with tracemotive.span("inner") as inner:
                    self.assertEqual(inner.parent_span_id, outer.span_id)
                with self.assertRaisesRegex(RuntimeError, "child"):
                    with tracemotive.span("failed-child"):
                        raise RuntimeError("child")
            self.assertIs(sdk.current_trace(), current_trace)
            self.assertIsNone(sdk.current_span())

        span_started = self._events("span.started")
        span_ended = self._events("span.ended")
        self.assertEqual(len(span_started), 3)
        self.assertEqual(len(span_ended), 3)
        self.assertEqual(span_started[0]["payload"]["parent_span_id"], None)
        self.assertEqual(span_started[1]["payload"]["parent_span_id"], outer.span_id)
        failed_end = next(event for event in span_ended if event["payload"]["name"] == "failed-child")
        self.assertEqual(failed_end["payload"]["status"], "error")
        self.assertEqual(failed_end["payload"]["error"]["type"], "RuntimeError")
        self.assertEqual(self._events("trace.ended")[0]["payload"]["status"], "ok")

    def test_span_error_preserves_type_when_exception_stringification_fails(self):
        tracemotive.configure(enabled=True)
        observed = None
        with tracemotive.trace("bad-error"):
            try:
                with tracemotive.span("bad-child"):
                    raise BadError()
            except BadError as error:
                observed = error
        self.assertIsInstance(observed, BadError)
        failed_end = self._events("span.ended")[0]["payload"]
        self.assertEqual(failed_end["status"], "error")
        self.assertEqual(failed_end["error"], {"type": "BadError", "message": None})
        self.assertEqual(self._events("trace.ended")[0]["payload"]["status"], "ok")

    def test_metaclass_name_lookup_failure_preserves_error_type_and_message(self):
        self.assertRaises(RuntimeError, lambda: NameFailsError.__name__)
        tracemotive.configure(enabled=True)
        observed = None
        with tracemotive.trace("metaclass-name"):
            try:
                with tracemotive.span("child"):
                    raise NameFailsError("safe message")
            except NameFailsError as error:
                observed = error
        self.assertIsInstance(observed, NameFailsError)
        failed_end = self._events("span.ended")[0]["payload"]
        self.assertEqual(
            failed_end["error"],
            {"type": "NameFailsError", "message": "safe message"},
        )

    def test_metaclass_and_stringification_failures_preserve_type_with_sink_failure(self):
        sink = RecordThenFailSink()
        sdk._set_event_sink(sink)
        tracemotive.configure(enabled=True)
        observed = None
        with tracemotive.trace("metaclass-and-string"):
            try:
                with tracemotive.span("child"):
                    raise NameAndStringFailError()
            except NameAndStringFailError as error:
                observed = error
        self.assertIsInstance(observed, NameAndStringFailError)
        failed_end = next(
            event["payload"] for event in sink.events
            if event["event_type"] == "span.ended"
        )
        self.assertEqual(
            failed_end["error"],
            {"type": "NameAndStringFailError", "message": None},
        )

    def test_span_error_preserves_type_when_message_sanitization_fails(self):
        tracemotive.configure(enabled=True)
        original_sanitize_text = sdk.privacy.sanitize_text

        def fail_only_for_message(value):
            if value == "message-that-cannot-be-sanitized":
                raise RuntimeError("sanitization failed")
            return original_sanitize_text(value)

        observed = None
        with patch.object(sdk.privacy, "sanitize_text", side_effect=fail_only_for_message):
            with tracemotive.trace("sanitize-error"):
                try:
                    with tracemotive.span("child"):
                        raise ValueError("message-that-cannot-be-sanitized")
                except ValueError as error:
                    observed = error
        self.assertIsInstance(observed, ValueError)
        failed_end = self._events("span.ended")[0]["payload"]
        self.assertEqual(failed_end["error"], {"type": "ValueError", "message": None})

    def test_flush_isolates_lookup_and_invocation_failures(self):
        tracemotive.configure(enabled=True)
        sdk._set_event_sink(LookupFailSink())
        self.assertFalse(tracemotive.flush())

        sdk._set_event_sink(FailingSink())
        self.assertFalse(tracemotive.flush())

        success_sink = Sink()
        sdk._set_event_sink(success_sink)
        self.assertTrue(tracemotive.flush())
        self.assertTrue(tracemotive.flush())
        self.assertEqual(success_sink.flush_calls, [2.0, 2.0])

        sdk._set_event_sink(object())
        self.assertTrue(tracemotive.flush())

    def test_nested_trace_is_independent_and_restores_outer_span(self):
        tracemotive.configure(enabled=True)
        with tracemotive.trace("outer") as outer_trace:
            with tracemotive.span("A") as outer_span:
                with tracemotive.trace("inner") as inner_trace:
                    self.assertNotEqual(inner_trace.trace_id, outer_trace.trace_id)
                    self.assertIs(sdk.current_trace(), inner_trace)
                    self.assertIsNone(sdk.current_span())
                    with tracemotive.span("inner-root") as inner_span:
                        self.assertIsNone(inner_span.parent_span_id)
                self.assertIs(sdk.current_trace(), outer_trace)
                self.assertIs(sdk.current_span(), outer_span)

        traces = self._events("trace.started")
        self.assertEqual(len({event["payload"]["trace_id"] for event in traces}), 2)
        ended = self._events("trace.ended")
        self.assertEqual([event["payload"]["status"] for event in ended], ["ok", "ok"])

    def test_exceptional_nested_trace_restores_exact_outer_context(self):
        tracemotive.configure(enabled=True)
        with tracemotive.trace("outer") as outer_trace:
            with tracemotive.span("outer-span") as outer_span:
                inner_trace_id = None
                try:
                    with tracemotive.trace("inner") as inner_trace:
                        inner_trace_id = inner_trace.trace_id
                        self.assertIsNone(sdk.current_span())
                        raise LookupError("inner failure")
                except LookupError as error:
                    self.assertEqual(str(error), "inner failure")
                self.assertIs(sdk.current_trace(), outer_trace)
                self.assertIs(sdk.current_span(), outer_span)
                with tracemotive.span("outer-child") as outer_child:
                    self.assertEqual(outer_child.parent_span_id, outer_span.span_id)
        self.assertIsNotNone(inner_trace_id)
        trace_started = self._events("trace.started")
        self.assertEqual(len(trace_started), 2)
        self.assertNotEqual(
            trace_started[0]["payload"]["trace_id"],
            trace_started[1]["payload"]["trace_id"],
        )
        inner_end = next(
            event for event in self._events("trace.ended")
            if event["payload"]["trace_id"] == inner_trace_id
        )
        self.assertEqual(inner_end["payload"]["status"], "error")
        outer_child = next(
            event for event in self._events("span.started")
            if event["payload"]["name"] == "outer-child"
        )
        self.assertEqual(outer_child["payload"]["trace_id"], outer_trace.trace_id)
        self.assertEqual(outer_child["payload"]["parent_span_id"], outer_span.span_id)

    def test_capture_is_disabled_by_default_and_redacted_before_sink(self):
        tracemotive.configure(enabled=True, capture_content=False)
        with tracemotive.trace("disabled-capture"):
            with tracemotive.span("span", input={"password": "secret-input"}) as handle:
                handle.set_output({"authorization": "Bearer secret-output"})
                handle.set_attribute("note", "api_key=attribute-secret")
        started = self._events("span.started")[0]["payload"]
        ended = self._events("span.ended")[0]["payload"]
        self.assertIsNone(started["input"])
        self.assertEqual(started["capture"]["input"]["reason"], "disabled")
        self.assertEqual(started["capture"]["output"]["reason"], "not_yet_available")
        self.assertIsNone(ended["output"])
        self.assertEqual(ended["capture"]["output"]["reason"], "disabled")
        serialized = json.dumps(self.sink.events, sort_keys=True)
        for secret in ("secret-input", "secret-output", "attribute-secret"):
            self.assertNotIn(secret, serialized)

    def test_capture_enabled_supports_null_and_exact_lifecycle_output_states(self):
        tracemotive.configure(enabled=True, capture_content=True)
        with tracemotive.trace("capture"):
            with tracemotive.span("captured", input=None) as handle:
                handle.set_output(None)
        started = self._events("span.started")[0]["payload"]
        ended = self._events("span.ended")[0]["payload"]
        self.assertIsNone(started["input"])
        self.assertEqual(started["capture"]["input"], {"state": "captured", "reason": None, "redacted": False})
        self.assertEqual(started["capture"]["output"]["reason"], "not_yet_available")
        self.assertIsNone(ended["output"])
        self.assertEqual(ended["capture"]["output"], {"state": "captured", "reason": None, "redacted": False})

        sdk._reset_for_tests()
        self.sink = Sink()
        sdk._set_event_sink(self.sink)
        tracemotive.configure(enabled=True, capture_content=True)
        with tracemotive.trace("redaction"):
            with tracemotive.span(
                "redacted",
                input={"nested": {"password": "input-secret"}},
                metadata={"authorization": "Bearer metadata-secret"},
            ) as handle:
                handle.set_output([{"api_key": "output-secret"}])
                handle.set_attribute("credential", "client_secret=attribute-secret")
        serialized = json.dumps(self.sink.events, sort_keys=True)
        for secret in ("input-secret", "metadata-secret", "output-secret", "attribute-secret"):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_manual_capture_reports_source_serialization_and_size_reasons(self):
        tracemotive.configure(enabled=True, capture_content=True)
        with tracemotive.trace("capture-reasons"):
            with tracemotive.span("unavailable"):
                pass
            with tracemotive.span("serialization-error", input=object()):
                pass
            with tracemotive.span("size-limit", input="x" * (MAX_CONTENT_BYTES - 1)):
                pass
        started = {
            event["payload"]["name"]: event["payload"]
            for event in self._events("span.started")
        }
        unavailable_end = next(
            event["payload"] for event in self._events("span.ended")
            if event["payload"]["name"] == "unavailable"
        )
        self.assertEqual(unavailable_end["capture"]["output"]["reason"], "source_unavailable")
        self.assertEqual(started["serialization-error"]["capture"]["input"]["reason"], "serialization_error")
        self.assertEqual(started["size-limit"]["capture"]["input"]["reason"], "size_limit")

    def test_disabled_paths_do_not_inspect_content_sources(self):
        with tracemotive.trace(ExplosiveSource(), ExplosiveSource()):
            with tracemotive.span("disabled-span", input=ExplosiveSource()) as handle:
                handle.set_output(ExplosiveSource())
        self.assertEqual(self.sink.events, [])

        tracemotive.configure(enabled=True, capture_content=False)
        with tracemotive.trace("capture-disabled"):
            with tracemotive.span("disabled-content", input=ExplosiveSource()) as handle:
                handle.set_output(ExplosiveSource())
        self.assertEqual(len(self._events("span.started")), 1)
        self.assertEqual(len(self._events("span.ended")), 1)

    def test_enabled_standalone_span_remains_the_accepted_noop(self):
        tracemotive.configure(enabled=True)
        body_ran = []
        with tracemotive.span("standalone") as handle:
            body_ran.append(True)
            handle.set_output("ignored")
        self.assertEqual(body_ran, [True])
        self.assertEqual(self.sink.events, [])
        self.assertIsNone(sdk.current_trace())
        self.assertIsNone(sdk.current_span())

    def test_manual_non_io_snapshot_carries_metadata_attributes_zero_usage_and_parameters(self):
        tracemotive.configure(enabled=True)
        details = LLMDetails(
            "llm",
            None,
            "request-model",
            None,
            None,
            LLMUsage(0, None, None, None, None),
            [],
            {"temperature": 0},
            None,
        )
        with tracemotive.trace("snapshot"):
            with tracemotive.span(
                "generation",
                type="llm",
                operation="llm.generate",
                details=details,
                metadata={"keep": "value"},
            ) as handle:
                handle.set_attribute("added", "later")
        ended = self._events("span.ended")[0]["payload"]
        self.assertEqual(ended["metadata"], {"keep": "value"})
        self.assertEqual(ended["attributes"], {"added": "later"})
        self.assertEqual(ended["details"]["usage"]["input_tokens"], 0)
        self.assertEqual(ended["details"]["request_parameters"], {"temperature": 0})

    def test_ids_and_timestamps_are_canonical(self):
        tracemotive.configure(enabled=True)
        for index in range(20):
            with tracemotive.trace(f"identity-{index}"):
                with tracemotive.span(f"identity-{index}"):
                    pass
        event_ids = set()
        trace_ids = set()
        span_ids = set()
        for event in self.sink.events:
            self.assertRegex(event["emitted_at"], TIMESTAMP_RE)
            self.assertEqual(UUID(event["event_id"]).version, 4)
            self.assertNotIn(event["event_id"], event_ids)
            event_ids.add(event["event_id"])
            payload = event["payload"]
            self.assertRegex(payload["trace_id"], TRACE_ID_RE)
            self.assertNotEqual(payload["trace_id"], "0" * 32)
            trace_ids.add(payload["trace_id"])
            if "span_id" in payload:
                self.assertRegex(payload["span_id"], SPAN_ID_RE)
                self.assertNotEqual(payload["span_id"], "0" * 16)
                span_ids.add(payload["span_id"])
        self.assertEqual(len(trace_ids), 20)
        self.assertEqual(len(span_ids), 20)

    def test_context_isolation_for_async_tasks_and_threads(self):
        tracemotive.configure(enabled=True)

        async def task_body(label, barrier):
            with tracemotive.trace(label):
                trace_id = sdk.current_trace().trace_id
                await barrier.wait()
                with tracemotive.span(label) as handle:
                    parent = handle.parent_span_id
                return trace_id, parent

        async def run_tasks():
            barrier = asyncio.Barrier(2)
            first = asyncio.create_task(task_body("one", barrier))
            second = asyncio.create_task(task_body("two", barrier))
            return await asyncio.gather(first, second)

        task_results = asyncio.run(run_tasks())
        self.assertNotEqual(task_results[0][0], task_results[1][0])
        self.assertEqual(task_results[0][1], None)
        self.assertEqual(task_results[1][1], None)

        barrier = threading.Barrier(2)

        def thread_body(label):
            with tracemotive.trace(label):
                barrier.wait()
                with tracemotive.span(label) as handle:
                    return handle.parent_span_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            thread_results = list(pool.map(thread_body, ("thread-one", "thread-two")))
        self.assertEqual(thread_results, [None, None])

    def test_explicit_async_task_inherits_trace_and_span_context_then_restores_parent(self):
        tracemotive.configure(enabled=True)

        async def child_created_in_trace(parent_trace, result):
            result.append((sdk.current_trace(), sdk.current_span()))
            with tracemotive.trace("task-inner"):
                result.append((sdk.current_trace(), sdk.current_span()))
                with tracemotive.span("task-root") as task_root:
                    result.append(task_root.parent_span_id)
            result.append((sdk.current_trace(), sdk.current_span()))

        async def child_created_in_span(parent_span, result):
            result.append((sdk.current_trace(), sdk.current_span()))
            with tracemotive.span("task-child") as task_child:
                result.append(task_child.parent_span_id)
            result.append((sdk.current_trace(), sdk.current_span()))

        async def run():
            trace_child_result = []
            span_child_result = []
            with tracemotive.trace("parent") as parent_trace:
                trace_task = asyncio.create_task(child_created_in_trace(parent_trace, trace_child_result))
                await trace_task
                self.assertIs(sdk.current_trace(), parent_trace)
                self.assertIsNone(sdk.current_span())
                with tracemotive.span("parent-span") as parent_span:
                    span_task = asyncio.create_task(child_created_in_span(parent_span, span_child_result))
                    await span_task
                    self.assertIs(sdk.current_trace(), parent_trace)
                    self.assertIs(sdk.current_span(), parent_span)
            return trace_child_result, span_child_result, parent_trace

        trace_child_result, span_child_result, parent_trace = asyncio.run(run())
        self.assertIs(trace_child_result[0][0], parent_trace)
        self.assertIsNone(trace_child_result[0][1])
        self.assertNotEqual(trace_child_result[1][0].trace_id, parent_trace.trace_id)
        self.assertIsNone(trace_child_result[1][1])
        self.assertIsNone(trace_child_result[2])
        self.assertIs(trace_child_result[3][0], parent_trace)
        self.assertIsNone(trace_child_result[3][1])
        self.assertIs(span_child_result[0][0], parent_trace)
        self.assertIsNotNone(span_child_result[0][1])
        self.assertEqual(span_child_result[1], span_child_result[0][1].span_id)
        self.assertIs(span_child_result[2][0], parent_trace)
        self.assertIs(span_child_result[2][1], span_child_result[0][1])

    def test_overlapping_threads_restore_only_their_own_context(self):
        tracemotive.configure(enabled=True)
        ready = threading.Barrier(2)
        release = threading.Barrier(2)
        observations = {}

        def worker(label):
            with tracemotive.trace(label) as current_trace:
                with tracemotive.span(label) as current_span:
                    observations[label] = (
                        current_trace.trace_id,
                        current_span.span_id,
                        sdk.current_trace(),
                        sdk.current_span(),
                    )
                    ready.wait()
                    release.wait()
                    self.assertIs(sdk.current_trace(), current_trace)
                    self.assertIs(sdk.current_span(), current_span)
                self.assertIs(sdk.current_trace(), current_trace)
                self.assertIsNone(sdk.current_span())
            self.assertIsNone(sdk.current_trace())
            self.assertIsNone(sdk.current_span())

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, label) for label in ("one", "two")]
            for future in futures:
                future.result()
        self.assertNotEqual(observations["one"][0], observations["two"][0])
        self.assertNotEqual(observations["one"][1], observations["two"][1])
        self.assertEqual(observations["one"][0], observations["one"][2].trace_id)
        self.assertEqual(observations["one"][1], observations["one"][3].span_id)
        self.assertEqual(observations["two"][0], observations["two"][2].trace_id)
        self.assertEqual(observations["two"][1], observations["two"][3].span_id)

    def test_internal_failures_are_isolated_and_user_exceptions_survive(self):
        tracemotive.configure(enabled=True)
        with patch.object(sdk, "_new_trace_id", side_effect=RuntimeError("id failure")):
            with tracemotive.trace("isolated"):
                pass
        self.assertEqual(self.sink.events, [])

        with tracemotive.trace("sink failure"):
            pass
        sdk._set_event_sink(FailingSink())
        self.assertFalse(tracemotive.flush())
        with self.assertRaisesRegex(RuntimeError, "user"):
            with tracemotive.trace("preserve"):
                raise RuntimeError("user")

    def test_flush_isolated_without_issue_07_transport(self):
        self.assertTrue(tracemotive.flush())
        tracemotive.configure(enabled=True)
        self.assertTrue(tracemotive.flush())
        self.assertEqual(self.sink.flush_calls, [2.0])
        sdk._set_event_sink(Sink())
        self.assertTrue(tracemotive.flush(0.25))


if __name__ == "__main__":
    unittest.main()
