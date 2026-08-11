from itertools import count
from hashlib import sha256
import unittest

from agentlens.canonical import (
    AGENTLENS_SCHEMA_VERSION,
    Capture,
    CaptureInfo,
    Error,
    LLMDetails,
    LLMUsage,
    Span,
    SpanSource,
    Trace,
    TraceSource,
)
from agentlens.canonical.models import _canonical_json_dumps
from agentlens.collector import (
    Collector,
    MAX_EVENT_BYTES,
    MAX_REQUEST_BYTES,
    IngestError,
)
from agentlens.storage import Repository
from agentlens.storage.repository import timestamp_to_us


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
OTHER_TRACE_ID = "5bf92f3577b34da6a3ce929d0e0e4736"
THIRD_TRACE_ID = "6bf92f3577b34da6a3ce929d0e0e4736"
FOURTH_TRACE_ID = "7bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
CHILD_SPAN_ID = "10f067aa0ba902b7"
EVENT_COUNTER = count(1)

TRACE_SOURCE = TraceSource("framework", "1.0", "integration", "1.0", "native-trace")
SPAN_SOURCE = SpanSource(
    "framework", "1.0", "integration", "1.0", "native-trace", "native-span", None
)


def event(event_type, payload, *, event_id=None, emitted_at="2026-08-10T13:00:00Z"):
    return {
        "event_id": (
            f"00000000-0000-4000-8000-{next(EVENT_COUNTER):012x}"
            if event_id is None
            else event_id
        ),
        "event_type": event_type,
        "emitted_at": emitted_at,
        "payload": payload.to_dict(),
    }


def batch(*events):
    return {"protocol_version": 1, "events": list(events)}


def make_trace(
    trace_id=TRACE_ID,
    *,
    ended=False,
    status=None,
    metadata=None,
    name="Agent workflow",
):
    return Trace(
        AGENTLENS_SCHEMA_VERSION,
        trace_id,
        name,
        "2026-08-10T13:00:00.000000Z",
        "2026-08-10T13:00:01.000000Z" if ended else None,
        (status if status is not None else ("ok" if ended else "unset")),
        TRACE_SOURCE,
        {} if metadata is None else metadata,
        {},
    )


def make_span(
    trace_id=TRACE_ID,
    span_id=SPAN_ID,
    *,
    ended=False,
    parent_span_id=None,
    status=None,
    error=None,
    input_value=None,
    input_capture=None,
    output_value=None,
    output_capture=None,
    metadata=None,
):
    input_capture = input_capture or CaptureInfo("not_captured", "disabled", False)
    output_capture = output_capture or CaptureInfo(
        "captured" if ended else "not_captured",
        None if ended else "not_yet_available",
        False,
    )
    return Span(
        AGENTLENS_SCHEMA_VERSION,
        trace_id,
        span_id,
        parent_span_id,
        "llm",
        "llm.generate",
        "generation",
        "2026-08-10T13:00:00.100000Z",
        "2026-08-10T13:00:00.900000Z" if ended else None,
        status if status is not None else ("error" if ended and error else ("ok" if ended else "unset")),
        error,
        input_value,
        output_value,
        Capture(input_capture, output_capture),
        SPAN_SOURCE,
        {} if metadata is None else metadata,
        {},
        LLMDetails(
            "llm",
            "provider",
            "request-model",
            "response-model" if ended else None,
            "response-id" if ended else None,
            LLMUsage(2, 3 if ended else None, None, None, None),
            ["stop"] if ended else [],
            {"temperature": 0} if ended else None,
            None,
        ),
    )


def count_error_spans(repository, trace_id):
    return repository.connection.execute(
        "SELECT COUNT(*) FROM spans WHERE trace_id = ? AND status = 'error'",
        (trace_id,),
    ).fetchone()[0]


class CollectorTests(unittest.TestCase):
    def test_trace_and_span_lifecycle_and_child_error_count_are_independent(self):
        with Repository() as repository, Collector(repository, clock=lambda: 123) as collector:
            trace_started = event("trace.started", make_trace())
            child_started = event(
                "span.started",
                make_span(span_id=CHILD_SPAN_ID, parent_span_id=SPAN_ID),
            )
            child_error = event(
                "span.ended",
                make_span(
                    span_id=CHILD_SPAN_ID,
                    parent_span_id=SPAN_ID,
                    ended=True,
                    error=Error("ToolError", "recovered"),
                ),
            )
            trace_ended = event(
                "trace.ended",
                make_trace(ended=True, status="ok"),
            )

            result = collector.ingest(batch(child_started, child_error, trace_started, trace_ended))

            self.assertEqual(result, {"accepted": 4, "duplicates": 0, "stale": 0})
            self.assertEqual(repository.get_trace(TRACE_ID).status, "ok")
            self.assertEqual(count_error_spans(repository, TRACE_ID), 1)
            self.assertEqual(repository.get_span(TRACE_ID, CHILD_SPAN_ID).status, "error")

    def test_explicit_top_level_error_is_preserved_and_unset_is_not_inferred(self):
        with Repository() as repository, Collector(repository, clock=lambda: 123) as collector:
            collector.ingest(batch(event("trace.ended", make_trace(ended=True, status="unset"))))
            self.assertEqual(repository.get_trace(TRACE_ID).status, "unset")
            collector.ingest(
                batch(
                    event(
                        "trace.ended",
                        make_trace(OTHER_TRACE_ID, ended=True, status="error"),
                    )
                )
            )
            self.assertEqual(repository.get_trace(OTHER_TRACE_ID).status, "error")

    def test_span_io_merge_preserves_started_input_and_completed_output(self):
        started = make_span(
            input_value={"prompt": "original"},
            input_capture=CaptureInfo("captured", None, False),
        )
        ended = make_span(
            ended=True,
            input_value=None,
            input_capture=CaptureInfo("not_captured", "source_unavailable", False),
            output_value={"answer": "done"},
        )
        with Repository() as repository, Collector(repository, clock=lambda: 123) as collector:
            result = collector.ingest(batch(event("span.started", started), event("span.ended", ended)))
            restored = repository.get_span(TRACE_ID, SPAN_ID)
            self.assertEqual(result, {"accepted": 2, "duplicates": 0, "stale": 0})
            self.assertEqual(restored.input, {"prompt": "original"})
            self.assertEqual(restored.output, {"answer": "done"})
            self.assertEqual(restored.capture.input.state, "captured")
            self.assertEqual(restored.capture.output.state, "captured")
            self.assertEqual(
                collector.ingest(batch(event("span.ended", ended))),
                {"accepted": 1, "duplicates": 0, "stale": 0},
            )

    def test_end_before_start_is_valid_and_started_input_can_enrich_stale_entity(self):
        ended = make_span(
            ended=True,
            input_value=None,
            input_capture=CaptureInfo("not_captured", "source_unavailable", False),
            output_value={"answer": "done"},
        )
        started = make_span(
            input_value={"prompt": "late observation"},
            input_capture=CaptureInfo("captured", None, False),
        )
        with Repository() as repository, Collector(repository, clock=lambda: 123) as collector:
            self.assertEqual(
                collector.ingest(batch(event("span.ended", ended))),
                {"accepted": 1, "duplicates": 0, "stale": 0},
            )
            self.assertEqual(
                collector.ingest(batch(event("span.started", started))),
                {"accepted": 0, "duplicates": 0, "stale": 1},
            )
            restored = repository.get_span(TRACE_ID, SPAN_ID)
            self.assertEqual(restored.input, {"prompt": "late observation"})
            self.assertEqual(restored.output, {"answer": "done"})
            self.assertEqual(restored.status, "ok")

            captured_end = make_span(
                trace_id=OTHER_TRACE_ID,
                ended=True,
                input_value={"prompt": "end observation"},
                input_capture=CaptureInfo("captured", None, False),
                output_value={"answer": "done"},
            )
            captured_start = make_span(
                trace_id=OTHER_TRACE_ID,
                input_value={"prompt": "start observation"},
                input_capture=CaptureInfo("captured", None, False),
            )
            collector.ingest(batch(event("span.ended", captured_end)))
            collector.ingest(batch(event("span.started", captured_start)))
            self.assertEqual(
                repository.get_span(OTHER_TRACE_ID, SPAN_ID).input,
                {"prompt": "start observation"},
            )

    def test_repeated_events_are_idempotent_and_stale_events_do_not_update_timestamp(self):
        started = event("span.started", make_span(), event_id="4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4")
        with Repository() as repository, Collector(repository, clock=lambda: 100) as collector:
            self.assertEqual(
                collector.ingest(batch(started)),
                {"accepted": 1, "duplicates": 0, "stale": 0},
            )
            self.assertEqual(
                collector.ingest(batch(started)),
                {"accepted": 0, "duplicates": 1, "stale": 0},
            )
            ended = event("span.ended", make_span(ended=True, output_value={"done": True}))
            collector.ingest(batch(ended))
            stale = event("span.started", make_span(), event_id="5dbd9b3f-9c54-42ed-b0c0-529e99c35ca4")
            self.assertEqual(
                collector.ingest(batch(stale)),
                {"accepted": 0, "duplicates": 0, "stale": 1},
            )
            updated_at = repository.connection.execute(
                "SELECT updated_at_us FROM spans WHERE trace_id = ? AND span_id = ?",
                (TRACE_ID, SPAN_ID),
            ).fetchone()[0]
            self.assertEqual(updated_at, 100)
            self.assertEqual(repository.get_span(TRACE_ID, SPAN_ID).output, {"done": True})

            same_batch_event = event("span.started", make_span(trace_id=OTHER_TRACE_ID))
            self.assertEqual(
                collector.ingest(batch(same_batch_event, same_batch_event)),
                {"accepted": 1, "duplicates": 1, "stale": 0},
            )

    def test_immutable_span_conflict_reports_the_frozen_field(self):
        with Repository() as repository, Collector(repository) as collector:
            collector.ingest(batch(event("span.started", make_span())))
            conflicting = make_span(parent_span_id=CHILD_SPAN_ID)
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(event("span.started", conflicting)))
            self.assertEqual(context.exception.status_code, 409)
            self.assertEqual(context.exception.code, "entity_conflict")
            self.assertEqual(context.exception.field, "payload.parent_span_id")

    def test_composite_span_identity_allows_same_span_id_under_different_traces(self):
        with Repository() as repository, Collector(repository, clock=lambda: 100) as collector:
            first = event("span.started", make_span(span_id=SPAN_ID))
            second = event("span.started", make_span(trace_id=OTHER_TRACE_ID, span_id=SPAN_ID))
            self.assertEqual(
                collector.ingest(batch(first, second)),
                {"accepted": 2, "duplicates": 0, "stale": 0},
            )
            self.assertIsNotNone(repository.get_span(TRACE_ID, SPAN_ID))
            self.assertIsNotNone(repository.get_span(OTHER_TRACE_ID, SPAN_ID))

    def test_repeated_start_and_end_conflicts_roll_back_the_entire_batch(self):
        with Repository() as repository, Collector(repository, clock=lambda: 100) as collector:
            first = event("span.started", make_span())
            conflicting_start = event(
                "span.started",
                make_span(metadata={"changed": True}),
            )
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(first, conflicting_start))
            self.assertEqual(context.exception.status_code, 409)
            self.assertEqual(context.exception.code, "entity_conflict")
            self.assertIsNone(repository.get_span(TRACE_ID, SPAN_ID))

            ended = event("span.ended", make_span(ended=True))
            conflicting_end = event(
                "span.ended",
                make_span(ended=True, output_value={"different": True}),
            )
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(ended, conflicting_end))
            self.assertEqual(context.exception.code, "entity_conflict")
            self.assertIsNone(repository.get_span(TRACE_ID, SPAN_ID))
            self.assertEqual(
                repository.connection.execute("SELECT COUNT(*) FROM ingest_events").fetchone()[0],
                0,
            )

    def test_repeated_final_snapshot_conflicts_without_start_authority(self):
        first = make_span(
            ended=True,
            input_value={"request": "first"},
            input_capture=CaptureInfo("captured", None, False),
        )
        second = make_span(
            ended=True,
            input_value={"request": "second"},
            input_capture=CaptureInfo("captured", None, False),
        )
        with Repository() as repository, Collector(repository, clock=lambda: 100) as collector:
            first_event = event("span.ended", first)
            collector.ingest(batch(first_event))
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(event("span.ended", second)))
            self.assertEqual(context.exception.code, "entity_conflict")
            self.assertEqual(repository.get_span(TRACE_ID, SPAN_ID).input, {"request": "first"})
            self.assertEqual(
                repository.connection.execute("SELECT COUNT(*) FROM ingest_events").fetchone()[0],
                1,
            )

            equivalent = event("span.ended", first)
            self.assertEqual(
                collector.ingest(batch(equivalent)),
                {"accepted": 1, "duplicates": 0, "stale": 0},
            )
            self.assertEqual(
                repository.connection.execute("SELECT COUNT(*) FROM ingest_events").fetchone()[0],
                2,
            )

            other_trace = make_span(
                trace_id=OTHER_TRACE_ID,
                ended=True,
                input_value={"request": "isolated"},
                input_capture=CaptureInfo("captured", None, False),
            )
            self.assertEqual(
                collector.ingest(batch(event("span.ended", other_trace))),
                {"accepted": 1, "duplicates": 0, "stale": 0},
            )

    def test_event_hash_uses_canonical_payload_and_emitted_at_microseconds(self):
        first_id = "4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4"
        second_id = "5dbd9b3f-9c54-42ed-b0c0-529e99c35ca4"
        first = event(
            "trace.started",
            make_trace(metadata={"z": 1, "a": "value"}),
            event_id=first_id,
            emitted_at="2026-08-10T13:00:00Z",
        )
        second = event(
            "trace.started",
            make_trace(metadata={"a": "value", "z": 1}),
            event_id=second_id,
            emitted_at="2026-08-10T13:00:00.000000Z",
        )
        with Repository() as repository, Collector(repository, clock=lambda: 123) as collector:
            collector.ingest(batch(first))
            collector.ingest(batch(second))
            first_record = repository.get_ingest_event(first_id)
            second_record = repository.get_ingest_event(second_id)
            self.assertEqual(
                first_record["event_content_sha256"], second_record["event_content_sha256"]
            )
            self.assertEqual(first_record["received_at_us"], 123)
            expected_hash = sha256(
                _canonical_json_dumps(
                    {
                        "event_type": "trace.started",
                        "emitted_at_us": timestamp_to_us("2026-08-10T13:00:00Z"),
                        "payload": make_trace(metadata={"a": "value", "z": 1}).to_dict(),
                    }
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(first_record["event_content_sha256"], expected_hash)

            conflicting_retry = event(
                "trace.started",
                make_trace(metadata={"z": 1, "a": "value"}),
                event_id=first_id,
                emitted_at="2026-08-10T13:00:00.000001Z",
            )
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(conflicting_retry))
            self.assertEqual(context.exception.code, "event_id_conflict")

            new_trace = event(
                "trace.started",
                make_trace(THIRD_TRACE_ID),
                event_id="6dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
            )
            conflicting_batch_event = event(
                "trace.started",
                make_trace(metadata={"different": True}),
                event_id=first_id,
            )
            with self.assertRaises(IngestError):
                collector.ingest(batch(new_trace, conflicting_batch_event))
            self.assertIsNone(repository.get_trace(THIRD_TRACE_ID))

    def test_span_error_count_is_exact_and_does_not_include_trace_status(self):
        with Repository() as repository, Collector(repository, clock=lambda: 100) as collector:
            collector.ingest(
                batch(
                    event("trace.ended", make_trace(ended=True, status="ok")),
                    event(
                        "span.ended",
                        make_span(ended=True, error=Error("Failure", "failed")),
                    ),
                    event(
                        "span.ended",
                        make_span(
                            span_id=CHILD_SPAN_ID,
                            ended=True,
                            status="ok",
                            error=None,
                        ),
                    ),
                )
            )
            self.assertEqual(count_error_spans(repository, TRACE_ID), 1)
            self.assertEqual(repository.get_trace(TRACE_ID).status, "ok")

    def test_replayed_error_completion_does_not_double_count_error_spans(self):
        error_span = make_span(ended=True, error=Error("Failure", "failed"))
        with Repository() as repository, Collector(repository) as collector:
            self.assertEqual(
                collector.ingest(batch(event("span.ended", error_span))),
                {"accepted": 1, "duplicates": 0, "stale": 0},
            )
            self.assertEqual(
                collector.ingest(batch(event("span.ended", error_span))),
                {"accepted": 1, "duplicates": 0, "stale": 0},
            )
            self.assertEqual(count_error_spans(repository, TRACE_ID), 1)

    def test_event_hash_changes_for_type_time_and_payload_and_is_stable_for_deep_unicode_numbers(self):
        collector = Collector()
        try:
            base = make_trace(
                metadata={
                    "unicode": "日本語",
                    "deep": {"items": [{"value": 1.25}, {"value": "leaf"}]},
                }
            )
            same_reordered = make_trace(
                metadata={
                    "deep": {"items": [{"value": 1.25}, {"value": "leaf"}]},
                    "unicode": "日本語",
                }
            )
            first = collector._validate_event(
                event("trace.started", base, emitted_at="2026-08-10T13:00:00Z"), 0
            )
            deterministic = collector._validate_event(
                event("trace.started", same_reordered, emitted_at="2026-08-10T13:00:00.000000Z"), 0
            )
            self.assertEqual(first.event_content_sha256, deterministic.event_content_sha256)

            changed_type = collector._validate_event(
                event("trace.ended", make_trace(ended=True, status="unset")), 0
            )
            changed_time = collector._validate_event(
                event("trace.started", base, emitted_at="2026-08-10T13:00:00.000001Z"), 0
            )
            changed_payload = collector._validate_event(
                event("trace.started", make_trace(metadata={"changed": True})), 0
            )
            self.assertNotEqual(first.event_content_sha256, changed_type.event_content_sha256)
            self.assertNotEqual(first.event_content_sha256, changed_time.event_content_sha256)
            self.assertNotEqual(first.event_content_sha256, changed_payload.event_content_sha256)

            exact_number_wire = base.to_json().replace(
                '"unicode":"日本語"', '"unicode":1.23000000000000000001'
            )
            exact_number = Trace.from_json(exact_number_wire)
            exact_first = collector._validate_event(event("trace.started", exact_number), 0)
            exact_second = collector._validate_event(event("trace.started", exact_number), 0)
            self.assertEqual(
                exact_first.event_content_sha256,
                exact_second.event_content_sha256,
            )
        finally:
            collector.close()

    def test_span_before_trace_and_child_before_parent_are_accepted(self):
        child = make_span(span_id=CHILD_SPAN_ID, parent_span_id=SPAN_ID)
        with Repository() as repository, Collector(repository, clock=lambda: 100) as collector:
            collector.ingest(batch(event("span.started", child)))
            self.assertIsNotNone(repository.get_span(TRACE_ID, CHILD_SPAN_ID))
            collector.ingest(batch(event("trace.started", make_trace())))
            self.assertIsNotNone(repository.get_trace(TRACE_ID))

    def test_malformed_event_and_json_have_distinct_validation_contracts(self):
        malformed_payload = make_trace().to_dict()
        malformed_payload["unexpected"] = True
        with Repository() as repository, Collector(repository) as collector:
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(event("trace.started", Trace.from_dict(make_trace().to_dict())) | {"payload": malformed_payload}))
            self.assertEqual(context.exception.response()[0], 422)
            self.assertEqual(context.exception.code, "validation_error")
            self.assertIsNone(repository.get_trace(TRACE_ID))

            with self.assertRaises(IngestError) as context:
                collector.ingest_json("{not valid json")
            self.assertEqual(context.exception.response()[0], 400)
            self.assertEqual(context.exception.code, "malformed_json")

            with self.assertRaises(IngestError) as context:
                collector.ingest({"protocol_version": 2, "events": []})
            self.assertEqual(context.exception.response()[0], 422)
            self.assertEqual(context.exception.field, "protocol_version")

    def test_oversized_individual_event_rejects_whole_request(self):
        with Repository() as repository, Collector(repository) as collector:
            base_event = event("trace.started", make_trace(metadata={"blob": ""}))
            base_size = collector._validate_event(base_event, 0).serialized_size
            exact_payload = make_trace(metadata={"blob": "x" * (MAX_EVENT_BYTES - base_size)})
            exact_event = event("trace.started", exact_payload)
            self.assertEqual(collector._validate_event(exact_event, 0).serialized_size, MAX_EVENT_BYTES)
            self.assertEqual(collector.ingest(batch(exact_event)), {"accepted": 1, "duplicates": 0, "stale": 0})

            over_payload = make_trace(
                OTHER_TRACE_ID,
                metadata={"blob": "x" * (MAX_EVENT_BYTES - base_size + 1)},
            )
            over_event = event("trace.started", over_payload)
            self.assertGreater(collector._validate_event(over_event, 0).serialized_size, MAX_EVENT_BYTES)
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(over_event))
            self.assertEqual(context.exception.status_code, 413)
            self.assertIsNone(repository.get_trace(OTHER_TRACE_ID))

    def test_exact_request_limit_is_accepted_and_one_byte_over_is_rejected(self):
        trace_ids = [TRACE_ID, OTHER_TRACE_ID, THIRD_TRACE_ID, FOURTH_TRACE_ID]
        event_ids = [
            "4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
            "5dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
            "6dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
            "7dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
        ]
        with Repository() as repository, Collector(repository) as collector:
            def make_events(lengths):
                return [
                    event(
                        "trace.started",
                        make_trace(trace_id, metadata={"blob": "x" * length}),
                        event_id=event_id,
                    )
                    for trace_id, event_id, length in zip(trace_ids, event_ids, lengths)
                ]

            base_events = make_events([0, 0, 0, 0])
            normalized_base = {
                "protocol_version": 1,
                "events": [collector._validate_event(value, index).normalized_event for index, value in enumerate(base_events)],
            }
            base_size = len(_canonical_json_dumps(normalized_base).encode("utf-8"))
            delta = MAX_REQUEST_BYTES - base_size
            quotient, remainder = divmod(delta, 4)
            exact_events = make_events([quotient + (index < remainder) for index in range(4)])
            exact_request = batch(*exact_events)
            exact_normalized = {
                "protocol_version": 1,
                "events": [collector._validate_event(value, index).normalized_event for index, value in enumerate(exact_events)],
            }
            self.assertEqual(
                len(_canonical_json_dumps(exact_normalized).encode("utf-8")),
                MAX_REQUEST_BYTES,
            )
            self.assertEqual(collector.ingest(exact_request), {"accepted": 4, "duplicates": 0, "stale": 0})

            over_events = make_events([quotient + (index < remainder) + (1 if index == 0 else 0) for index in range(4)])
            with self.assertRaises(IngestError) as context:
                collector.ingest(batch(*over_events))
            self.assertEqual(context.exception.status_code, 413)
            self.assertEqual(
                repository.connection.execute("SELECT COUNT(*) FROM traces").fetchone()[0],
                4,
            )

    def test_privacy_normalization_happens_before_persistence(self):
        span = make_span(
            ended=True,
            input_value={"password": "secret-value"},
            input_capture=CaptureInfo("captured", None, False),
        )
        with Repository() as repository, Collector(repository) as collector:
            collector.ingest(batch(event("span.ended", span)))
            stored = repository.get_span(TRACE_ID, SPAN_ID)
            self.assertEqual(stored.input, {"password": "[REDACTED]"})
            persisted = repository.connection.execute(
                "SELECT input_json FROM span_io WHERE trace_id = ? AND span_id = ?",
                (TRACE_ID, SPAN_ID),
            ).fetchone()[0]
            self.assertNotIn("secret-value", persisted)

    def test_deletion_removes_idempotency_history_and_allows_resurrection(self):
        trace_event = event(
            "trace.started",
            make_trace(),
            event_id="4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
        )
        with Repository() as repository, Collector(repository) as collector:
            collector.ingest(batch(trace_event))
            self.assertTrue(repository.delete_trace(TRACE_ID))
            self.assertEqual(collector.ingest(batch(trace_event)), {"accepted": 1, "duplicates": 0, "stale": 0})
            self.assertIsNotNone(repository.get_trace(TRACE_ID))


if __name__ == "__main__":
    unittest.main()
