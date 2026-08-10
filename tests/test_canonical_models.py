import json
import math
import inspect
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agentlens.canonical import (
    AGENTLENS_SCHEMA_VERSION,
    AgentDetails,
    Capture,
    CaptureInfo,
    CustomDetails,
    Error,
    EstimatedCost,
    HandoffDetails,
    LLMDetails,
    LLMUsage,
    RetrievalDetails,
    Span,
    SpanSource,
    ToolDetails,
    Trace,
    TraceSource,
    ValidationError,
    normalize_json_value,
    validate_span_id,
    validate_timestamp,
    validate_trace_id,
    validate_trace_transition,
)


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
CHILD_SPAN_ID = "00f067aa0ba902b8"
SOURCE = TraceSource("framework", "1.0", "integration", "1.0", "native-trace")
SPAN_SOURCE = SpanSource("framework", "1.0", "integration", "1.0", "native-trace", "native-span", None)
CAPTURED = CaptureInfo("captured", None, False)
NOT_CAPTURED = CaptureInfo("not_captured", "disabled", False)
OUTPUT_NOT_YET = CaptureInfo("not_captured", "not_yet_available", False)

TRACE_FIELDS = {
    "schema_version",
    "trace_id",
    "name",
    "started_at",
    "ended_at",
    "status",
    "source",
    "metadata",
    "attributes",
}
SPAN_FIELDS = {
    "schema_version",
    "trace_id",
    "span_id",
    "parent_span_id",
    "type",
    "operation",
    "name",
    "started_at",
    "ended_at",
    "status",
    "error",
    "input",
    "output",
    "capture",
    "source",
    "metadata",
    "attributes",
    "details",
}
TRACE_SOURCE_FIELDS = {
    "framework",
    "framework_version",
    "integration",
    "integration_version",
    "native_trace_id",
}
SPAN_SOURCE_FIELDS = TRACE_SOURCE_FIELDS | {
    "native_span_id",
    "native_parent_span_id",
}
DETAIL_FIELDS = {
    "agent": {"kind", "agent_name", "agent_version"},
    "llm": {
        "kind",
        "provider",
        "request_model",
        "response_model",
        "response_id",
        "usage",
        "finish_reasons",
        "request_parameters",
        "estimated_cost",
    },
    "tool": {"kind", "tool_name", "tool_call_id"},
    "handoff": {"kind", "from_agent", "to_agent"},
    "retrieval": {"kind"},
    "custom": {"kind", "source_type"},
}


def make_details(span_type: str):
    if span_type == "agent":
        return AgentDetails("agent", "Research Agent", None)
    if span_type == "llm":
        return LLMDetails(
            "llm",
            "openai",
            "gpt-request",
            "gpt-response",
            "resp_1",
            LLMUsage(0, None, None, None, None),
            [],
            None,
            None,
        )
    if span_type == "tool":
        return ToolDetails("tool", "get_weather", None)
    if span_type == "handoff":
        return HandoffDetails("handoff", "triage", "billing")
    if span_type == "retrieval":
        return RetrievalDetails("retrieval")
    return CustomDetails("custom", "guardrail")


def make_trace(*, status="unset", ended_at=None, name="Agent workflow", started_at="2026-08-10T13:00:00.000000Z"):
    return Trace(
        AGENTLENS_SCHEMA_VERSION,
        TRACE_ID,
        name,
        started_at,
        ended_at,
        status,
        SOURCE,
        {},
        {},
    )


def make_span(
    *,
    span_type="llm",
    span_id=SPAN_ID,
    parent_span_id=None,
    status="unset",
    ended_at=None,
    error=None,
    input=None,
    output=None,
    capture=None,
    operation="operation",
    name="span",
    started_at="2026-08-10T13:00:00.100000Z",
    details=None,
):
    return Span(
        AGENTLENS_SCHEMA_VERSION,
        TRACE_ID,
        span_id,
        parent_span_id,
        span_type,
        operation,
        name,
        started_at,
        ended_at,
        status,
        error,
        input,
        output,
        capture or Capture(NOT_CAPTURED, OUTPUT_NOT_YET),
        SPAN_SOURCE,
        {},
        {},
        details or make_details(span_type),
    )


class CanonicalModelTests(unittest.TestCase):
    def test_valid_trace_and_spans(self):
        trace = make_trace()
        root = make_span()
        child = make_span(span_id=CHILD_SPAN_ID, parent_span_id=SPAN_ID)
        self.assertEqual(trace.status, "unset")
        self.assertIsNone(root.parent_span_id)
        self.assertEqual(child.parent_span_id, SPAN_ID)

    def test_every_span_type_is_valid(self):
        for span_type in ("agent", "llm", "tool", "handoff", "retrieval", "custom"):
            with self.subTest(span_type=span_type):
                span = make_span(span_type=span_type)
                self.assertEqual(span.details.kind, span_type)

    def test_ids_and_timestamp_validation(self):
        self.assertEqual(validate_trace_id(TRACE_ID), TRACE_ID)
        self.assertEqual(validate_span_id(SPAN_ID), SPAN_ID)
        self.assertEqual(validate_timestamp("2026-08-10T13:00:00.123456Z"), "2026-08-10T13:00:00.123456Z")
        for invalid in ("not-an-id", "A" * 32, "0" * 32, "0" * 16):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    (validate_trace_id if len(invalid) == 32 else validate_span_id)(invalid)
        for invalid_timestamp in (
            "2026-08-10T13:00:00+00:00",
            "2026-08-10T13:00:00.1234567Z",
            "2026-13-10T13:00:00Z",
        ):
            with self.assertRaises(ValidationError):
                validate_timestamp(invalid_timestamp)

    def test_exact_schema_version_and_enums(self):
        with self.assertRaises(ValidationError):
            Trace("0.2", TRACE_ID, "x", "2026-08-10T13:00:00Z", None, "unset", SOURCE, {}, {})
        with self.assertRaises(ValidationError):
            make_trace(status="unknown")
        with self.assertRaises(ValidationError):
            make_span(span_type="unknown")

    def test_details_type_mismatch_is_invalid(self):
        with self.assertRaises(ValidationError):
            Span(
                AGENTLENS_SCHEMA_VERSION,
                TRACE_ID,
                SPAN_ID,
                None,
                "tool",
                "operation",
                "span",
                "2026-08-10T13:00:00Z",
                None,
                "unset",
                None,
                None,
                None,
                Capture(NOT_CAPTURED, OUTPUT_NOT_YET),
                SPAN_SOURCE,
                {},
                {},
                make_details("llm"),
            )

    def test_lifecycle_and_timestamp_invariants(self):
        with self.assertRaises(ValidationError):
            make_trace(status="ok")
        with self.assertRaises(ValidationError):
            make_span(status="ok")
        with self.assertRaises(ValidationError):
            make_span(ended_at="2026-08-10T13:00:00Z", status="ok")
        with self.assertRaises(ValidationError):
            make_span(ended_at="2026-08-10T13:00:01Z", status="ok", error=Error("E", "failed"))
        ended = make_span(ended_at="2026-08-10T13:00:00.100001Z", status="ok")
        ended.validate_ended()
        with self.assertRaises(ValidationError):
            make_span(ended_at="2026-08-10T13:00:00.099999Z", status="ok")
        with self.assertRaises(ValidationError):
            make_span(ended_at="2026-08-10T13:00:01Z", status="unset")

    def test_terminal_trace_status_does_not_transition(self):
        terminal = make_trace(status="ok", ended_at="2026-08-10T13:00:01Z")
        validate_trace_transition(terminal, make_trace(status="ok", ended_at="2026-08-10T13:00:02Z"))
        with self.assertRaises(ValidationError):
            validate_trace_transition(terminal, make_trace(status="error", ended_at="2026-08-10T13:00:02Z"))
        with self.assertRaises(ValidationError):
            validate_trace_transition(terminal, make_trace())

    def test_capture_info_and_captured_json_null(self):
        self.assertEqual(CAPTURED.reason, None)
        span = make_span(input=None, capture=Capture(CAPTURED, OUTPUT_NOT_YET))
        self.assertIsNone(span.input)
        self.assertEqual(span.capture.input.state, "captured")
        with self.assertRaises(ValidationError):
            CaptureInfo("captured", "disabled", False)
        with self.assertRaises(ValidationError):
            CaptureInfo("not_captured", None, False)
        with self.assertRaises(ValidationError):
            CaptureInfo("not_captured", "disabled", True)

    def test_json_value_normalization_and_finite_numbers(self):
        source = {"nested": [True, 1, 1.5, None, "text"]}
        normalized = normalize_json_value(source)
        self.assertEqual(normalized, source)
        self.assertIsNot(normalized, source)
        for invalid in (math.nan, math.inf, -math.inf, b"bytes", datetime.now(), {"set"}, object()):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(ValidationError):
                    normalize_json_value(invalid)

    def test_usage_zero_is_distinct_from_unknown_null(self):
        usage = LLMUsage(0, None, None, None, None)
        self.assertEqual(usage.input_tokens, 0)
        self.assertIsNone(usage.output_tokens)
        with self.assertRaises(ValidationError):
            LLMUsage(-1, None, None, None, None)
        with self.assertRaises(ValidationError):
            LLMUsage(True, None, None, None, None)
        with self.assertRaises(ValidationError):
            EstimatedCost(1, "123", True)

    def test_serialization_round_trip(self):
        original = make_span(
            status="error",
            ended_at="2026-08-10T13:00:01Z",
            error=Error("TimeoutError", "request timed out"),
            input=None,
            output={"answer": None},
            capture=Capture(CAPTURED, CAPTURED),
        )
        wire = original.to_json()
        self.assertEqual(json.loads(wire), original.to_dict())
        restored = Span.from_json(wire)
        self.assertEqual(restored, original)
        self.assertEqual(restored.to_json(), wire)

    def test_deserialization_rejects_invalid_values(self):
        trace_data = make_trace().to_dict()
        trace_data["metadata"] = None
        with self.assertRaises(ValidationError):
            Trace.from_dict(trace_data)
        span_data = make_span().to_dict()
        span_data["input"] = float("nan")
        with self.assertRaises(ValidationError):
            Span.from_dict(span_data)
        span_data = make_span().to_dict()
        span_data["details"]["finish_reasons"] = "not-an-array"
        with self.assertRaises(ValidationError):
            Span.from_dict(span_data)
        with self.assertRaises(ValidationError):
            Span.from_json('{"input": NaN}')

    def test_exact_canonical_wire_shapes_and_extra_fields(self):
        trace = make_trace()
        span = make_span()
        self.assertEqual(set(trace.to_dict()), TRACE_FIELDS)
        self.assertEqual(set(span.to_dict()), SPAN_FIELDS)
        self.assertEqual(set(trace.source.to_dict()), TRACE_SOURCE_FIELDS)
        self.assertEqual(set(span.source.to_dict()), SPAN_SOURCE_FIELDS)
        self.assertEqual(set(Error("TimeoutError", "timed out").to_dict()), {"type", "message"})
        with self.assertRaises(ValidationError):
            Error(None, None)

        for span_type in DETAIL_FIELDS:
            with self.subTest(span_type=span_type):
                self.assertEqual(set(make_details(span_type).to_dict()), DETAIL_FIELDS[span_type])

        invalid_documents = [
            (Trace, trace.to_dict()),
            (Span, span.to_dict()),
            (TraceSource, trace.source.to_dict()),
            (SpanSource, span.source.to_dict()),
            (Error, Error("E", "message").to_dict()),
            (CaptureInfo, CAPTURED.to_dict()),
            (AgentDetails, AgentDetails("agent", None, None).to_dict()),
            (LLMDetails, make_details("llm").to_dict()),
            (ToolDetails, make_details("tool").to_dict()),
            (HandoffDetails, make_details("handoff").to_dict()),
            (RetrievalDetails, make_details("retrieval").to_dict()),
            (CustomDetails, make_details("custom").to_dict()),
            (LLMUsage, LLMUsage(None, None, None, None, None).to_dict()),
            (EstimatedCost, EstimatedCost(0, "USD", True).to_dict()),
        ]
        for model_type, document in invalid_documents:
            with self.subTest(model=model_type.__name__):
                document["unexpected"] = True
                with self.assertRaises(ValidationError):
                    model_type.from_dict(document)

    def test_string_boundaries_and_required_non_empty_values(self):
        for length in (1, 256):
            with self.subTest(trace_name_length=length):
                self.assertEqual(len(make_trace(name="x" * length).name), length)
        with self.assertRaises(ValidationError):
            make_trace(name="")
        with self.assertRaises(ValidationError):
            make_trace(name="x" * 257)

        for field_name, kwargs, maximum in (
            ("span_name", {"name": "x" * 256}, 256),
            ("operation", {"operation": "x" * 128}, 128),
        ):
            with self.subTest(field_name=field_name):
                valid = make_span(**kwargs)
                self.assertEqual(len(getattr(valid, "name" if field_name == "span_name" else "operation")), maximum)
        for kwargs in ({"name": ""}, {"name": "x" * 257}, {"operation": ""}, {"operation": "x" * 129}):
            with self.assertRaises(ValidationError):
                make_span(**kwargs)
        with self.assertRaises(ValidationError):
            ToolDetails("tool", "", None)

    def test_identifier_table_and_parent_validation(self):
        self.assertEqual(validate_trace_id("a" * 32), "a" * 32)
        self.assertEqual(validate_span_id("a" * 16), "a" * 16)
        for validator, valid_length in ((validate_trace_id, 32), (validate_span_id, 16)):
            invalid_values = [
                "a" * (valid_length - 1),
                "a" * (valid_length + 1),
                "A" * valid_length,
                "g" * valid_length,
                "0" * valid_length,
            ]
            for value in invalid_values:
                with self.subTest(validator=validator.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        validator(value)
        for parent_id in ("a" * 15, "A" * 16, "g" * 16, "0" * 16):
            with self.subTest(parent_id=parent_id):
                with self.assertRaises(ValidationError):
                    make_span(parent_span_id=parent_id)

    def test_timestamp_table_and_round_trip(self):
        for fraction in ("", ".1", ".12", ".123", ".1234", ".12345", ".123456"):
            with self.subTest(fraction=fraction):
                timestamp = f"2026-08-10T13:00:00{fraction}Z"
                self.assertEqual(validate_timestamp(timestamp), timestamp)
        for invalid in (
            "2026-08-10T13:00:00z",
            "2026-08-10T13:00:00+00:00",
            "2026-08-10T13:00:00+09:00",
            "2026-08-10T13:00:00.1234567Z",
            "2026-02-30T13:00:00Z",
            "2026-08-10T24:00:00Z",
            "2026-08-10 13:00:00Z",
        ):
            with self.subTest(timestamp=invalid):
                with self.assertRaises(ValidationError):
                    validate_timestamp(invalid)

        same_time = "2026-08-10T13:00:00.100000Z"
        trace = make_trace(ended_at="2026-08-10T13:00:00.000000Z")
        span = make_span(status="ok", ended_at=same_time)
        self.assertEqual(Trace.from_json(trace.to_json()), trace)
        self.assertEqual(Span.from_json(span.to_json()), span)
        with self.assertRaises(ValidationError):
            make_trace(ended_at="2026-08-10T12:59:59Z")

    def test_jsonvalue_valid_values_and_invalid_values(self):
        for value in (None, True, False, 0, 1.5, "text"):
            with self.subTest(value=value):
                self.assertEqual(normalize_json_value(value), value)
        nested = {"array": [None, {"number": 2.5}, ["value"]]}
        normalized = normalize_json_value(nested)
        self.assertEqual(normalized, nested)
        self.assertIsNot(normalized, nested)
        self.assertIsNot(normalized["array"], nested["array"])

        invalid_values = (
            math.nan,
            math.inf,
            -math.inf,
            b"bytes",
            bytearray(b"bytes"),
            datetime.now(),
            Decimal("1.2"),
            {"set"},
            ("tuple",),
            (item for item in (1, 2)),
            object(),
            {1: "non-string key"},
            {"nested": [{"bad": object()}]},
        )
        for value in invalid_values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValidationError):
                    normalize_json_value(value)

    def test_jsonvalue_cycles_are_rejected_without_recursion_error(self):
        direct = []
        direct.append(direct)
        indirect_list = []
        indirect_dict = {"child": indirect_list}
        indirect_list.append(indirect_dict)
        for value in (direct, indirect_dict):
            with self.subTest(value=value.__class__.__name__):
                with self.assertRaises(ValidationError):
                    normalize_json_value(value)

        deep_cycle = []
        cursor = deep_cycle
        for _ in range(1200):
            child = []
            cursor.append(child)
            cursor = child
        cursor.append(deep_cycle)
        with self.assertRaises(ValidationError):
            normalize_json_value(deep_cycle)

    def test_exact_non_integer_json_numbers_round_trip_without_float_loss(self):
        template = make_span(
            status="ok",
            ended_at="2026-08-10T13:00:01Z",
            input=0,
            output=None,
            capture=Capture(CAPTURED, CAPTURED),
        )
        tokens = (
            "1e400",
            "1e-400",
            "0.123456789012345678901234567890",
            "-1e400",
            "123456789012345678901234567890.12345678901234567890",
            "-123456789012345678901234567890.12345678901234567890",
            "0.1",
            "0",
            "-0",
            "-0.0",
        )
        for token in tokens:
            with self.subTest(token=token):
                wire = template.to_json().replace('"input":0', f'"input":{token}', 1)
                restored = Span.from_json(wire)
                serialized = restored.to_json()
                self.assertNotIn('"input":"', serialized)
                self.assertEqual(Span.from_json(serialized).to_json(), serialized)
        self.assertIn('"input":1E+400', Span.from_json(
            template.to_json().replace('"input":0', '"input":1e400', 1)
        ).to_json())
        self.assertIn('"input":1E-400', Span.from_json(
            template.to_json().replace('"input":0', '"input":1e-400', 1)
        ).to_json())

        huge_integer = 10**5000
        cost = EstimatedCost(huge_integer, "USD", True)
        self.assertEqual(EstimatedCost.from_json(cost.to_json()), cost)
        self.assertEqual(cost.to_json(), EstimatedCost.from_json(cost.to_json()).to_json())

        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(invalid_number=token):
                invalid_wire = template.to_json().replace('"input":0', f'"input":{token}', 1)
                with self.assertRaises(ValidationError):
                    Span.from_json(invalid_wire)

    def test_canonical_json_valid_and_malformed_matrix(self):
        valid_values = (
            None,
            True,
            False,
            [],
            {},
            [None, {"nested": [True, False, "text"]}],
            "",
            'escaped "quote"',
            "escaped \\\\backslash",
            "line\nreturn\ttab\r",
            "日本語😀",
        )
        for value in valid_values:
            with self.subTest(value=repr(value)):
                span = make_span(
                    status="ok",
                    ended_at="2026-08-10T13:00:01Z",
                    input=value,
                    output=None,
                    capture=Capture(CAPTURED, CAPTURED),
                )
                self.assertEqual(Span.from_json(span.to_json()), span)

        template = CaptureInfo("captured", None, False).to_json()
        malformed = (
            template + "garbage",
            template[:-1] + ",}",
            '{"state":"captured","reason":null "redacted":false}',
            '{"state","captured"}',
            template[:-1],
            '{"state":"captured","reason":"unclosed,"redacted":false}',
            '{"state":"captured","reason":"\\x","redacted":false}',
            '{"state":"captured","reason":"bad\nvalue","redacted":false}',
            '{"state":"captured","reason":null,"redacted":1.}',
            '{"state":"captured","reason":NaN,"redacted":false}',
            '{"state":"captured","reason":Infinity,"redacted":false}',
            '{"state":"captured","reason":nul,"redacted":false}',
        )
        for value in malformed:
            with self.subTest(malformed=value):
                with self.assertRaises(ValidationError):
                    CaptureInfo.from_json(value)

    def test_utf8_safe_string_serialization_and_unicode_round_trip(self):
        values = (
            "\ud800",
            "\udfff",
            "before\ud800after",
            "日本語",
            "😀",
            'quote " and backslash \\\\',
        )
        for value in values:
            with self.subTest(value=repr(value)):
                span = make_span(
                    status="ok",
                    ended_at="2026-08-10T13:00:01Z",
                    input=value,
                    output=None,
                    capture=Capture(CAPTURED, CAPTURED),
                )
                wire = span.to_json()
                wire.encode("utf-8")
                self.assertFalse(any(0xD800 <= ord(char) <= 0xDFFF for char in wire))
                self.assertEqual(Span.from_json(wire).input, value)

        escaped_wire = make_span(
            status="ok",
            ended_at="2026-08-10T13:00:01Z",
            input=0,
            output=None,
            capture=Capture(CAPTURED, CAPTURED),
        ).to_json().replace('"input":0', '"input":"\\u65e5\\u672c"', 1)
        self.assertEqual(Span.from_json(escaped_wire).input, "日本")

    def test_deserialized_mutable_containers_remain_protected(self):
        details = LLMDetails(
            "llm",
            None,
            None,
            None,
            None,
            LLMUsage(None, None, None, None, None),
            ["stop"],
            {"nested": [1]},
            None,
        )
        source = make_span(
            status="ok",
            ended_at="2026-08-10T13:00:01Z",
            input={"input": [1]},
            output={"output": [2]},
            capture=Capture(CAPTURED, CAPTURED),
            details=details,
        )
        restored = Span.from_json(source.to_json())
        restored.details.finish_reasons.append(7)
        restored.details.request_parameters["invalid"] = {"set"}
        restored.input["invalid"] = object()
        restored.output["invalid"] = bytearray(b"x")
        restored.metadata["invalid"] = set()
        restored.attributes["invalid"] = object()
        wire = restored.to_json()
        wire.encode("utf-8")
        self.assertEqual(Span.from_json(wire).details.finish_reasons, ["stop"])

    def test_deserialized_captureinfo_reason_types_use_validation_error(self):
        for reason in ([], 1, {}, object()):
            with self.subTest(reason_type=type(reason).__name__):
                document = CAPTURED.to_dict()
                document["state"] = "not_captured"
                document["reason"] = reason
                document["redacted"] = False
                with self.assertRaises(ValidationError):
                    CaptureInfo.from_dict(document)
        for reason in ("[]", "1", "{}"):
            with self.subTest(reason_json=reason):
                value = '{"state":"not_captured","reason":' + reason + ',"redacted":false}'
                with self.assertRaises(ValidationError):
                    CaptureInfo.from_json(value)

    def test_deep_object_model_round_trip(self):
        depth = 1200
        value = {}
        cursor = value
        for _ in range(depth):
            child = {}
            cursor["child"] = child
            cursor = child
        cursor["value"] = "end"
        span = make_span(
            status="ok",
            ended_at="2026-08-10T13:00:01Z",
            input=value,
            output=None,
            capture=Capture(CAPTURED, CAPTURED),
        )
        wire = span.to_json()
        wire.encode("utf-8")
        restored = Span.from_json(wire)
        cursor = restored.input
        for _ in range(depth):
            cursor = cursor["child"]
        self.assertEqual(cursor["value"], "end")

    def test_deep_acyclic_jsonvalue_is_supported(self):
        depth = 1200
        value = None
        for _ in range(depth):
            value = [value]
        normalized = normalize_json_value(value)
        for _ in range(depth):
            normalized = normalized[0]
        self.assertIsNone(normalized)

        round_trip_depth = 1200
        round_trip_value = None
        for _ in range(round_trip_depth):
            round_trip_value = [round_trip_value]
        span = make_span(
            status="ok",
            ended_at="2026-08-10T13:00:01Z",
            input=round_trip_value,
            output=None,
            capture=Capture(CAPTURED, CAPTURED),
        )
        wire_dict = span.to_dict()
        cursor = wire_dict["input"]
        for _ in range(round_trip_depth):
            cursor = cursor[0]
        self.assertIsNone(cursor)
        self.assertEqual(Span.from_json(span.to_json()), span)

    def test_captureinfo_valid_families_and_invalid_combinations(self):
        valid = [
            CaptureInfo("captured", None, False),
            CaptureInfo("captured", None, True),
        ] + [
            CaptureInfo("not_captured", reason, False)
            for reason in (
                "disabled",
                "source_unavailable",
                "not_yet_available",
                "size_limit",
                "serialization_error",
            )
        ]
        for info in valid:
            with self.subTest(info=info):
                self.assertEqual(CaptureInfo.from_json(info.to_json()), info)
        invalid = (
            ("captured", "disabled", False),
            ("captured", "source_unavailable", True),
            ("not_captured", None, False),
            ("not_captured", "unknown", False),
            ("not_captured", "disabled", True),
            ("unknown", "disabled", False),
        )
        for state, reason, redacted in invalid:
            with self.subTest(state=state, reason=reason, redacted=redacted):
                with self.assertRaises(ValidationError):
                    CaptureInfo(state, reason, redacted)
        for reason in ([], 1, {}, object()):
            with self.subTest(reason_type=type(reason).__name__):
                with self.assertRaises(ValidationError):
                    CaptureInfo("not_captured", reason, False)

        captured_null = make_span(
            input=None,
            capture=Capture(CaptureInfo("captured", None, False), OUTPUT_NOT_YET),
        )
        not_captured_null = make_span(input=None, capture=Capture(NOT_CAPTURED, OUTPUT_NOT_YET))
        self.assertEqual(captured_null.input, not_captured_null.input)
        self.assertEqual(captured_null.capture.input.state, "captured")
        self.assertEqual(not_captured_null.capture.input.state, "not_captured")
        with self.assertRaises(ValidationError):
            make_span(input={"secret": "value"}, capture=Capture(NOT_CAPTURED, OUTPUT_NOT_YET))

    def test_details_discriminated_union_table(self):
        span_types = tuple(DETAIL_FIELDS)
        for span_type in span_types:
            with self.subTest(span_type=span_type):
                detail = make_details(span_type)
                span = make_span(span_type=span_type, details=detail)
                self.assertEqual(span.details.kind, span.type)
                self.assertEqual(type(detail).from_json(detail.to_json()), detail)

        for span_type in span_types:
            for detail_type in span_types:
                if span_type == detail_type:
                    continue
                with self.subTest(span_type=span_type, detail_type=detail_type):
                    with self.assertRaises(ValidationError):
                        make_span(span_type=span_type, details=make_details(detail_type))

        unknown = make_span().to_dict()
        unknown["details"]["kind"] = "unknown"
        with self.assertRaises(ValidationError):
            Span.from_dict(unknown)
        with self.assertRaises(ValidationError):
            RetrievalDetails.from_dict({"kind": "retrieval", "extra": True})

    def test_llm_usage_counter_table(self):
        counter_names = (
            "input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        for counter_name in counter_names:
            for value in (None, 0, 7):
                with self.subTest(counter=counter_name, value=value):
                    counters = {name: None for name in counter_names}
                    counters[counter_name] = value
                    self.assertEqual(getattr(LLMUsage(**counters), counter_name), value)
            for value in (-1, True):
                with self.subTest(counter=counter_name, invalid=value):
                    counters = {name: None for name in counter_names}
                    counters[counter_name] = value
                    with self.assertRaises(ValidationError):
                        LLMUsage(**counters)

    def test_estimated_cost_table(self):
        huge_amount = 10**5000
        huge_cost = EstimatedCost(huge_amount, "USD", True)
        self.assertEqual(huge_cost.amount, huge_amount)
        self.assertEqual(EstimatedCost.from_json(huge_cost.to_json()), huge_cost)
        for amount in (0, 1, 0.0042):
            with self.subTest(amount=amount):
                self.assertEqual(EstimatedCost(amount, "USD", True).amount, amount)
        for amount in (-1, math.nan, math.inf, -math.inf, True):
            with self.subTest(amount=amount):
                with self.assertRaises(ValidationError):
                    EstimatedCost(amount, "USD", True)
        for currency in ("usd", "", "123"):
            with self.subTest(currency=currency):
                with self.assertRaises(ValidationError):
                    EstimatedCost(1, currency, True)
        for estimated in (False, 1, "true"):
            with self.subTest(estimated=estimated):
                with self.assertRaises(ValidationError):
                    EstimatedCost(1, "USD", estimated)

    def test_lifecycle_state_table_preserves_frozen_status_rules(self):
        for status in ("unset", "ok", "error"):
            with self.subTest(trace_status=status):
                trace = make_trace(status=status, ended_at="2026-08-10T13:00:01Z")
                trace.validate_ended()
        make_trace().validate_started()

        make_span().validate_started()
        make_span(status="ok", ended_at="2026-08-10T13:00:01Z").validate_ended()
        make_span(status="error", ended_at="2026-08-10T13:00:01Z").validate_ended()
        make_span(
            status="error",
            ended_at="2026-08-10T13:00:01Z",
            error=None,
        ).validate_ended()

        invalid_states = (
            {"status": "ok"},
            {"status": "error"},
            {"status": "unset", "ended_at": "2026-08-10T13:00:01Z"},
            {"status": "ok", "ended_at": "2026-08-10T13:00:01Z", "error": Error("E", "failure")},
        )
        for state in invalid_states:
            with self.subTest(state=state):
                with self.assertRaises(ValidationError):
                    make_span(**state)

    def test_deterministic_serialization_for_all_details_and_models(self):
        for span_type in DETAIL_FIELDS:
            with self.subTest(span_type=span_type):
                span = make_span(span_type=span_type)
                self.assertEqual(span.to_json(), span.to_json())
                self.assertEqual(
                    span.to_json(),
                    json.dumps(
                        span.to_dict(),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                self.assertEqual(Span.from_json(span.to_json()), span)
                self.assertEqual(type(span.details).from_json(span.details.to_json()), span.details)

        first = make_trace()
        second = Trace(
            AGENTLENS_SCHEMA_VERSION,
            TRACE_ID,
            "Agent workflow",
            "2026-08-10T13:00:00.000000Z",
            None,
            "unset",
            SOURCE,
            {"b": 2, "a": 1},
            {"d": 4, "c": 3},
        )
        reordered = Trace(
            AGENTLENS_SCHEMA_VERSION,
            TRACE_ID,
            "Agent workflow",
            "2026-08-10T13:00:00.000000Z",
            None,
            "unset",
            SOURCE,
            {"a": 1, "b": 2},
            {"c": 3, "d": 4},
        )
        self.assertNotEqual(first.to_json(), second.to_json())
        self.assertEqual(second.to_json(), reordered.to_json())

    def test_post_construction_mutation_cannot_corrupt_serialized_canonical_data(self):
        reasons = ["stop"]
        details = LLMDetails(
            "llm",
            None,
            None,
            None,
            None,
            LLMUsage(None, None, None, None, None),
            reasons,
            {"nested": [1]},
            None,
        )
        reasons.append(7)
        details.finish_reasons.append(7)
        details.request_parameters["bad"] = {"set"}
        self.assertEqual(details.to_dict()["finish_reasons"], ["stop"])
        self.assertEqual(details.to_dict()["request_parameters"], {"nested": [1]})
        self.assertEqual(json.loads(details.to_json())["finish_reasons"], ["stop"])

        metadata = {"nested": [1]}
        attributes = {"attribute": "value"}
        trace = Trace(
            AGENTLENS_SCHEMA_VERSION,
            TRACE_ID,
            "trace",
            "2026-08-10T13:00:00Z",
            None,
            "unset",
            SOURCE,
            metadata,
            attributes,
        )
        metadata["nested"].append({"invalid": {"set"}})
        trace.metadata["invalid"] = object()
        trace.attributes["invalid"] = bytearray(b"x")
        self.assertEqual(trace.to_dict()["metadata"], {"nested": [1]})
        self.assertEqual(trace.to_dict()["attributes"], attributes)
        self.assertEqual(json.loads(trace.to_json())["metadata"], {"nested": [1]})

        input_value = {"nested": [1]}
        output_value = {"result": True}
        span = make_span(
            status="ok",
            ended_at="2026-08-10T13:00:01Z",
            input=input_value,
            output=output_value,
            capture=Capture(CAPTURED, CAPTURED),
        )
        input_value["nested"].append({"invalid": {"set"}})
        output_value["invalid"] = object()
        span.input["invalid"] = set()
        span.output["invalid"] = datetime.now()
        self.assertEqual(span.to_dict()["input"], {"nested": [1]})
        self.assertEqual(span.to_dict()["output"], {"result": True})
        self.assertEqual(json.loads(span.to_json())["output"], {"result": True})

    def test_tool_name_has_only_the_frozen_non_empty_constraint(self):
        tool_name = "tool-" + ("x" * 100_000)
        self.assertEqual(ToolDetails("tool", tool_name, None).tool_name, tool_name)
        tool_source = inspect.getsource(ToolDetails)
        self.assertNotIn("2**31", tool_source)
        self.assertNotIn("_require_non_empty_string", tool_source)

    def test_canonical_package_framework_boundary(self):
        package_root = Path(__file__).resolve().parents[1] / "agentlens" / "canonical"
        source = "\n".join(path.read_text(encoding="utf-8") for path in package_root.glob("*.py"))
        for forbidden in (
            "import openai",
            "from openai",
            "import fastapi",
            "from fastapi",
            "import sqlite3",
            "from sqlite",
            "import requests",
            "import httpx",
            "from agentlens.backend",
            "from agentlens.transport",
            "from agentlens.frontend",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main()
