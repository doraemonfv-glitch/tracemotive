import threading
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import agentlens
from agentlens import sdk
from agentlens.canonical import CaptureInfo
from agentlens.integrations.openai_agents import AgentLensOpenAIProcessor
from agentlens.privacy import MAX_CONTENT_BYTES


class Sink:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def emit(self, event):
        with self.lock:
            self.events.append(event)


@dataclass
class TraceFixture:
    trace_id: str
    name: str = "workflow"


@dataclass
class SpanFixture:
    trace_id: str
    span_id: str
    parent_id: str | None
    started_at: str | None
    ended_at: str | None
    span_data: object
    error: object | None = None


@dataclass
class AgentData:
    name: str = "research"
    type: str = "agent"


@dataclass
class GenerationData:
    input: object = None
    output: object = None
    model: str | None = "gpt-test"
    model_config: object = None
    usage: object = None
    finish_reasons: object = None
    type: str = "generation"


@dataclass
class FunctionData:
    name: str = "lookup"
    input: object = None
    output: object = None
    type: str = "function"


@dataclass
class Response:
    id: str = "resp-test"
    model: str = "gpt-test"
    output: object = None


@dataclass
class ResponseData:
    response: object = None
    input: object = None
    usage: object = None
    finish_reasons: object = None
    type: str = "response"


@dataclass
class HandoffData:
    from_agent: str | None = "a"
    to_agent: str | None = "b"
    type: str = "handoff"


@dataclass
class CustomData:
    name: str = "guardrail"
    type: str = "guardrail"


class ErrorFixture(dict):
    def __init__(self):
        super().__init__(message="Bearer abc123", data={"secret": "do-not-store"})


@dataclass
class GenerationWithoutInput:
    model: str | None = "gpt-test"
    model_config: object = None
    usage: object = None
    type: str = "generation"


@dataclass
class OutputText:
    text: str
    type: str = "output_text"


@dataclass
class OutputMessage:
    content: list[object]
    type: str = "message"


class HostileOutputText:
    type = "output_text"
    text = "safe typed output"

    @property
    def metadata(self):
        raise AssertionError("unrelated output metadata was probed")


class TraceWithApiKey(TraceFixture):
    @property
    def tracing_api_key(self):
        raise AssertionError("tracing_api_key must not be probed")


class BrokenSpan:
    @property
    def trace_id(self):
        raise AssertionError("broken framework property")


class OpenAIAdapterTests(unittest.TestCase):
    def setUp(self):
        sdk._reset_for_tests()
        self.sink = Sink()
        sdk._set_event_sink(self.sink)
        agentlens.configure(enabled=True, capture_content=True)
        self.processor = AgentLensOpenAIProcessor()

    def tearDown(self):
        self.processor.shutdown()
        sdk._reset_for_tests()

    def _events(self, event_type):
        return [event["payload"] for event in self.sink.events if event["event_type"] == event_type]

    def _span(self, span_id="s1", parent_id=None, data=None, ended_at="2026-08-11T00:00:01Z"):
        return SpanFixture(
            "native-trace",
            span_id,
            parent_id,
            "2026-08-11T00:00:00Z",
            ended_at,
            data or AgentData(),
        )

    def test_trace_mapping_and_terminal_callback_remains_unset(self):
        trace = TraceFixture("trace-a")
        self.processor.on_trace_start(trace)
        self.processor.on_trace_start(trace)
        self.processor.on_trace_end(trace)
        started = self._events("trace.started")
        ended = self._events("trace.ended")
        self.assertEqual(started[0]["trace_id"], started[1]["trace_id"])
        self.assertEqual(ended[0]["trace_id"], started[0]["trace_id"])
        self.assertEqual(ended[0]["status"], "unset")

    def test_native_span_mapping_is_scoped_by_native_trace(self):
        self.processor.on_span_start(self._span(data=AgentData()))
        other = self._span(data=AgentData())
        other.trace_id = "native-other"
        self.processor.on_span_start(other)
        spans = self._events("span.started")
        self.assertNotEqual(spans[0]["trace_id"], spans[1]["trace_id"])
        self.assertNotEqual(spans[0]["span_id"], spans[1]["span_id"])

    def test_child_before_parent_preallocates_parent_in_trace_scope(self):
        child = self._span(parent_id="parent", data=AgentData())
        self.processor.on_span_start(child)
        parent = self._span(span_id="parent", data=AgentData())
        self.processor.on_span_start(parent)
        events = self._events("span.started")
        self.assertEqual(events[0]["parent_span_id"], events[1]["span_id"])

    def test_span_before_trace_does_not_freeze_placeholder_trace_name(self):
        self.processor.on_span_start(self._span(data=AgentData()))
        self.processor.on_trace_start(TraceFixture("native-trace", name="actual workflow"))
        trace = self._events("trace.started")[0]
        self.assertEqual(trace["name"], "actual workflow")
        self.assertEqual(trace["trace_id"], self._events("span.started")[0]["trace_id"])

    def test_concurrent_same_parent_preallocation_is_atomic(self):
        barrier = threading.Barrier(8)

        def emit(index):
            barrier.wait()
            self.processor.on_span_start(self._span(span_id=f"child-{index}", parent_id="parent"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(emit, range(8)))
        parent_ids = {event["parent_span_id"] for event in self._events("span.started")}
        self.assertEqual(len(parent_ids), 1)

    def test_span_types_and_generation_details(self):
        values = [
            AgentData(),
            GenerationData(
                input=[{"role": "user", "content": "hello"}],
                model_config={"temperature": 0, "metadata": {"password": "secret"}, "bad": object()},
                usage={"input_tokens": 0, "output_tokens": 2},
            ),
            ResponseData(
                Response(
                    output=[
                        OutputMessage([OutputText("done")]),
                    ]
                ),
                input=[{"role": "user"}],
            ),
            FunctionData(input='{"city":"Tokyo"}', output={"password": "secret"}),
            HandoffData(),
            CustomData(),
        ]
        for index, data in enumerate(values):
            self.processor.on_span_start(self._span(span_id=f"s{index}", data=data))
        spans = self._events("span.started")
        self.assertEqual([span["type"] for span in spans], ["agent", "llm", "llm", "tool", "handoff", "custom"])
        generation = spans[1]
        self.assertEqual(generation["details"]["provider"], None)
        self.assertEqual(generation["details"]["request_model"], "gpt-test")
        self.assertEqual(generation["details"]["usage"]["input_tokens"], 0)
        self.assertEqual(generation["details"]["request_parameters"], {"temperature": 0})
        self.assertNotIn("password", generation["details"]["request_parameters"])
        self.assertNotIn("metadata", generation["details"]["request_parameters"])
        self.assertEqual(spans[3]["input"], {"city": "Tokyo"})

    def test_response_output_normalizes_official_typed_items_and_preserves_null(self):
        typed = self._span(
            span_id="typed-response",
            data=ResponseData(
                response=Response(
                    output=[
                        OutputMessage([HostileOutputText()]),
                        {"type": "function_call", "name": "ignored", "arguments": "secret"},
                    ]
                )
            ),
        )
        self.processor.on_span_start(typed)
        self.processor.on_span_end(typed)
        ended = self._events("span.ended")[0]
        self.assertEqual(
            ended["output"],
            [{"type": "message", "content": [{"type": "output_text", "text": "safe typed output"}]}],
        )
        self.assertEqual(ended["capture"]["output"]["state"], "captured")

        null_output = self._span(
            span_id="null-response",
            data=ResponseData(response=Response(output=None)),
        )
        self.processor.on_span_start(null_output)
        self.processor.on_span_end(null_output)
        null_ended = self._events("span.ended")[1]
        self.assertIsNone(null_ended["output"])
        self.assertEqual(null_ended["capture"]["output"]["state"], "captured")

    def test_fake_finish_reasons_are_not_read_from_span_data(self):
        span = self._span(
            data=GenerationData(
                finish_reasons=["stop"],
                usage={"input_tokens": 1, "output_tokens": 1},
            )
        )
        self.processor.on_span_start(span)
        self.processor.on_span_end(span)
        started, ended = self._events("span.started")[0], self._events("span.ended")[0]
        self.assertEqual(started["details"]["finish_reasons"], [])
        self.assertEqual(ended["details"]["finish_reasons"], [])

    def test_content_disabled_does_not_probe_source_and_started_output_is_not_yet_available(self):
        sdk._reset_for_tests()
        sdk._set_event_sink(self.sink)
        agentlens.configure(enabled=True, capture_content=False)
        processor = AgentLensOpenAIProcessor()

        class Explosive:
            def __iter__(self):
                raise AssertionError("content was probed")

        data = GenerationData(input=Explosive(), output=Explosive())
        processor.on_span_start(self._span(data=data))
        payload = self._events("span.started")[0]
        self.assertIsNone(payload["input"])
        self.assertEqual(payload["capture"]["input"]["reason"], "disabled")
        self.assertEqual(payload["capture"]["output"], CaptureInfo("not_captured", "not_yet_available", False).to_dict())
        processor.shutdown()

    def test_end_carries_forward_llm_details_and_maps_error_without_secret(self):
        data = GenerationData(
            model="gpt-start",
            model_config={"temperature": 0, "top_p": 0.5},
            usage={"input_tokens": 0, "output_tokens": 3},
            input={"prompt": "hello"},
        )
        span = self._span(data=data)
        self.processor.on_span_start(span)
        span.span_data = GenerationData(model=None, model_config=None, usage={"input_tokens": None})
        span.error = ErrorFixture()
        self.processor.on_span_end(span)
        ended = self._events("span.ended")[0]
        self.assertEqual(ended["status"], "error")
        self.assertEqual(ended["error"], {"type": None, "message": "Bearer [REDACTED]"})
        self.assertEqual(ended["details"]["request_model"], "gpt-start")
        self.assertEqual(ended["details"]["usage"]["input_tokens"], 0)
        self.assertEqual(ended["details"]["request_parameters"], {"temperature": 0, "top_p": 0.5})
        self.assertNotIn("abc123", str(self.sink.events))

    def test_mapping_error_requires_official_mapping_shape_and_ignores_data(self):
        class AttributeOnlyError:
            message = "Bearer should-not-be-read"

        span = self._span(data=AgentData())
        span.error = AttributeOnlyError()
        self.processor.on_span_end(span)
        ended = self._events("span.ended")[0]
        self.assertEqual(ended["status"], "error")
        self.assertIsNone(ended["error"])

        span = self._span(span_id="mapping-error", data=AgentData())
        span.error = {"message": "Bearer abc123", "data": {"token": "secret"}}
        self.processor.on_span_end(span)
        ended = self._events("span.ended")[1]
        self.assertEqual(ended["error"], {"type": None, "message": "Bearer [REDACTED]"})
        self.assertNotIn("secret", str(self.sink.events))

    def test_end_input_enriches_uncaptured_start_but_start_capture_wins(self):
        unavailable = self._span(span_id="late-input", data=GenerationWithoutInput())
        self.processor.on_span_start(unavailable)
        unavailable.span_data = GenerationData(input={"late": "value"})
        self.processor.on_span_end(unavailable)
        enriched = self._events("span.ended")[0]
        self.assertEqual(enriched["input"], {"late": "value"})
        self.assertEqual(enriched["capture"]["input"]["state"], "captured")

        invalid_start = self._span(
            span_id="serialization-input",
            data=GenerationData(input=object()),
        )
        self.processor.on_span_start(invalid_start)
        invalid_start.span_data = GenerationData(input={"recovered": True})
        self.processor.on_span_end(invalid_start)
        recovered = self._events("span.ended")[1]
        self.assertEqual(recovered["input"], {"recovered": True})
        self.assertEqual(recovered["capture"]["input"]["state"], "captured")

        authoritative = self._span(
            span_id="authoritative-input",
            data=GenerationData(input={"start": "wins"}),
        )
        self.processor.on_span_start(authoritative)
        authoritative.span_data = GenerationData(input={"end": "ignored"})
        self.processor.on_span_end(authoritative)
        ended = self._events("span.ended")[2]
        self.assertEqual(ended["input"], {"start": "wins"})

        unavailable_end = self._span(
            span_id="captured-then-unavailable",
            data=GenerationData(input={"start": "wins"}),
        )
        self.processor.on_span_start(unavailable_end)
        unavailable_end.span_data = GenerationWithoutInput()
        self.processor.on_span_end(unavailable_end)
        preserved = self._events("span.ended")[3]
        self.assertEqual(preserved["input"], {"start": "wins"})

    def test_disabled_configuration_does_not_enrich_end_input(self):
        sdk._reset_for_tests()
        sdk._set_event_sink(self.sink)
        agentlens.configure(enabled=True, capture_content=False)
        processor = AgentLensOpenAIProcessor()
        span = self._span(data=GenerationWithoutInput())
        processor.on_span_start(span)
        span.span_data = GenerationData(input={"must": "stay private"})
        processor.on_span_end(span)
        ended = self._events("span.ended")[0]
        self.assertIsNone(ended["input"])
        self.assertEqual(ended["capture"]["input"]["reason"], "disabled")
        processor.shutdown()

    def test_missing_start_timestamp_warns_and_drops_callback(self):
        missing = self._span(span_id="missing-start", data=AgentData(), ended_at=None)
        missing.started_at = None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.processor.on_span_start(missing)
        self.assertEqual(self._events("span.started"), [])
        self.assertEqual(len(caught), 1)
        self.assertIn("missing or invalid span.started_at", str(caught[0].message))
        self.assertNotIn("missing-start", str(caught[0].message))

        missing_end = self._span(span_id="missing-end-start", data=AgentData(), ended_at=None)
        missing_end.started_at = None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.processor.on_span_end(missing_end)
        self.assertEqual(self._events("span.ended"), [])
        self.assertEqual(len(caught), 1)

    def test_end_callback_validates_current_started_at_before_carry_forward(self):
        span = self._span(
            span_id="current-start-validation",
            data=FunctionData(input={"secret": "do-not-warn"}),
        )
        self.processor.on_span_start(span)
        started = self._events("span.started")[0]
        key = ("native-trace", "current-start-validation")
        self.assertIn(key, self.processor._spans)

        span.started_at = None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.processor.on_span_end(span)
        self.assertEqual(self._events("span.ended"), [])
        self.assertEqual(len(caught), 1)
        warning_text = str(caught[0].message)
        self.assertNotIn("do-not-warn", warning_text)
        self.assertNotIn("current-start-validation", warning_text)
        self.assertIn(key, self.processor._spans)

        span.started_at = "not-a-timestamp"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.processor.on_span_end(span)
        self.assertEqual(self._events("span.ended"), [])
        self.assertEqual(len(caught), 1)
        self.assertNotIn("do-not-warn", str(caught[0].message))
        self.assertIn(key, self.processor._spans)

        span.started_at = "2026-08-11T00:00:00Z"
        span.ended_at = None
        self.processor.on_span_end(span)
        ended = self._events("span.ended")[0]
        self.assertEqual(ended["trace_id"], started["trace_id"])
        self.assertEqual(ended["span_id"], started["span_id"])
        self.assertIsInstance(ended["ended_at"], str)
        self.assertNotIn(key, self.processor._spans)

    def test_completed_span_snapshots_release_while_trace_remains_open(self):
        trace = TraceFixture("open-trace", "long workflow")
        self.processor.on_trace_start(trace)
        data = AgentData()
        span = self._span(span_id="completed-open", data=data)
        self.processor.on_span_start(span)
        self.assertEqual(len(self.processor._spans), 1)
        self.processor.on_span_end(span)
        self.assertEqual(self.processor._spans, {})
        self.assertIn(("native-trace", "completed-open"), self.processor.span_mapping)
        self.assertIn("native-trace", self.processor._traces)
        self.assertFalse(any(value is data or value is span for value in self.processor.__dict__.values()))
        self.processor.on_span_end(span)
        ended = self._events("span.ended")
        self.assertEqual(ended[1]["trace_id"], ended[0]["trace_id"])
        self.assertEqual(ended[1]["span_id"], ended[0]["span_id"])
        self.assertEqual(self.processor._spans, {})

    def test_many_completed_spans_do_not_accumulate_full_snapshots(self):
        self.processor.on_trace_start(TraceFixture("native-trace"))
        for index in range(300):
            data = AgentData(name=f"agent-{index}")
            span = self._span(span_id=f"completed-{index}", data=data)
            self.processor.on_span_start(span)
            self.processor.on_span_end(span)
            self.assertEqual(self.processor._spans, {})
        self.assertEqual(len(self.processor._traces), 1)
        self.assertEqual(len(self.processor.span_mapping), 300)
        self.assertEqual(len(self.processor.trace_mapping), 1)

    def test_terminal_cleanup_releases_snapshots_but_retains_identity_for_late_callbacks(self):
        trace = TraceFixture("native-retained", "retained workflow")
        span = SpanFixture(
            "native-retained",
            "span-retained",
            "parent-retained",
            "2026-08-11T00:00:00Z",
            "2026-08-11T00:00:01Z",
            AgentData(),
        )
        self.processor.on_trace_start(trace)
        self.processor.on_span_start(span)
        self.processor.on_span_end(span)
        self.processor.on_trace_end(trace)
        trace_id = self._events("trace.started")[0]["trace_id"]
        span_id = self._events("span.started")[0]["span_id"]
        parent_id = self._events("span.started")[0]["parent_span_id"]
        self.assertEqual(self.processor._traces, {})
        self.assertEqual(self.processor._spans, {})

        self.processor.on_trace_end(trace)
        self.processor.on_span_end(span)
        late_parent = SpanFixture(
            "native-retained",
            "parent-retained",
            None,
            "2026-08-11T00:00:00Z",
            "2026-08-11T00:00:01Z",
            AgentData(),
        )
        self.processor.on_span_start(late_parent)
        self.assertEqual(self._events("trace.ended")[1]["trace_id"], trace_id)
        self.assertEqual(self._events("span.ended")[1]["span_id"], span_id)
        self.assertEqual(self._events("span.ended")[1]["parent_span_id"], parent_id)
        self.assertEqual(self._events("span.started")[1]["span_id"], parent_id)
        self.assertEqual(self.processor.trace_mapping["native-retained"], trace_id)
        self.assertEqual(self.processor.span_mapping[("native-retained", "span-retained")], span_id)

    def test_api_key_is_not_probed_and_callback_property_failure_isolated(self):
        self.processor.on_trace_start(TraceWithApiKey("safe-trace"))
        self.processor.on_span_start(BrokenSpan())
        self.assertEqual(len(self._events("trace.started")), 1)
        self.assertNotIn("tracing_api_key", str(self.sink.events))

    def test_end_before_start_reuses_span_identity(self):
        span = self._span(data=AgentData())
        self.processor.on_span_end(span)
        end_id = self._events("span.ended")[0]["span_id"]
        self.processor.on_span_start(span)
        start_id = self._events("span.started")[0]["span_id"]
        self.assertEqual(end_id, start_id)

    def test_source_unavailable_and_captured_null_are_distinct(self):
        self.processor.on_span_start(self._span(data=AgentData()))
        null_input = self._span(span_id="s2", data=FunctionData(input={"value": None}))
        self.processor.on_span_start(null_input)
        spans = self._events("span.started")
        self.assertEqual(spans[0]["capture"]["input"]["reason"], "source_unavailable")
        self.assertEqual(spans[1]["capture"]["input"]["state"], "captured")
        self.assertEqual(spans[1]["input"], {"value": None})

    def test_output_redaction_serialization_failure_and_size_limit(self):
        redacted = self._span(span_id="redacted", data=FunctionData(output={"api_key": "secret"}))
        self.processor.on_span_start(redacted)
        self.processor.on_span_end(redacted)
        redacted_end = self._events("span.ended")[0]
        self.assertEqual(redacted_end["output"], {"api_key": "[REDACTED]"})
        self.assertTrue(redacted_end["capture"]["output"]["redacted"])

        failed = self._span(span_id="failed-output", data=FunctionData(output=object()))
        self.processor.on_span_start(failed)
        self.processor.on_span_end(failed)
        failed_end = self._events("span.ended")[1]
        self.assertEqual(failed_end["capture"]["output"]["reason"], "serialization_error")

        oversized = self._span(
            span_id="oversized-output",
            data=FunctionData(output="x" * MAX_CONTENT_BYTES),
        )
        self.processor.on_span_start(oversized)
        self.processor.on_span_end(oversized)
        oversized_end = self._events("span.ended")[2]
        self.assertEqual(oversized_end["capture"]["output"]["reason"], "size_limit")

    def test_disabled_response_output_is_not_probed(self):
        sdk._reset_for_tests()
        sdk._set_event_sink(self.sink)
        agentlens.configure(enabled=True, capture_content=False)
        processor = AgentLensOpenAIProcessor()

        class ResponseWithExplosiveOutput:
            id = "resp-safe"
            model = "gpt-safe"

            @property
            def output(self):
                raise AssertionError("response output was probed while capture was disabled")

        data = ResponseData(response=ResponseWithExplosiveOutput())
        processor.on_span_start(self._span(data=data))
        processor.on_span_end(self._span(data=data))
        self.assertEqual(len(self._events("span.started")), 1)
        self.assertEqual(len(self._events("span.ended")), 1)
        processor.shutdown()

    def test_failures_in_mapping_and_sink_do_not_escape_callback(self):
        class FailingSink:
            def emit(self, event):
                raise RuntimeError("sink failure")

        sdk._set_event_sink(FailingSink())
        self.processor.on_trace_start(TraceFixture("trace-failure"))
        self.processor.on_span_start(self._span(data=GenerationData(model_config={"temperature": object()})))
        self.processor.on_span_end(self._span(data=GenerationData()))

    def test_shutdown_releases_all_adapter_state(self):
        self.processor.on_span_start(self._span(data=AgentData()))
        self.processor.on_trace_end(TraceFixture("native-trace"))
        self.processor.shutdown()
        self.assertEqual(self.processor.trace_mapping, {})
        self.assertEqual(self.processor.span_mapping, {})


if __name__ == "__main__":
    unittest.main()
