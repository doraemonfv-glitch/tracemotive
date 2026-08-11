import json
import unittest
from datetime import datetime

from agentlens.canonical import (
    AGENTLENS_SCHEMA_VERSION,
    AgentDetails,
    Capture,
    CaptureInfo,
    CustomDetails,
    Error,
    HandoffDetails,
    LLMDetails,
    LLMUsage,
    Span,
    SpanSource,
    Trace,
    TraceSource,
    ToolDetails,
    ValidationError,
)
from agentlens.privacy import (
    MAX_CONTENT_BYTES,
    MANDATORY_SENSITIVE_KEYS,
    REDACTION_PLACEHOLDER,
    capture_content,
    sanitize_json_value,
    sanitize_text,
)


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
SPAN_SOURCE = SpanSource("framework", "1.0", "integration", "1.0", "native-trace", "native-span", None)


def make_span(*, input_value=None, output_value=None, capture=None, error=None, metadata=None, attributes=None, details=None, operation="operation", name="span", ended_at="2026-08-10T13:00:01.000000Z", status="ok"):
    return Span(
        AGENTLENS_SCHEMA_VERSION,
        TRACE_ID,
        SPAN_ID,
        None,
        "llm",
        operation,
        name,
        "2026-08-10T13:00:00.100000Z",
        ended_at,
        status,
        error,
        input_value,
        output_value,
        capture or Capture(CaptureInfo("captured", None, False), CaptureInfo("captured", None, False)),
        SPAN_SOURCE,
        metadata or {},
        attributes or {},
        details or LLMDetails("llm", "provider", "request", "response", "response-id", LLMUsage(0, None, None, None, None), [], None, None),
    )


class PrivacyTests(unittest.TestCase):
    def test_exact_mandatory_keys_are_recursive_case_insensitive_whole_value_redactions(self):
        source = {key.upper(): {"secret": "nested"} for key in MANDATORY_SENSITIVE_KEYS}
        source["nested"] = {"API_KEY": "abc", "safe": "keep"}
        source["array"] = [{"authorization": ["do", "not", "inspect"]}, {"safe": 0}]
        original = json.loads(json.dumps(source))

        sanitized, redacted = sanitize_json_value(source)

        self.assertTrue(redacted)
        for key in MANDATORY_SENSITIVE_KEYS:
            self.assertEqual(sanitized[key.upper()], REDACTION_PLACEHOLDER)
        self.assertEqual(sanitized["nested"], {"API_KEY": REDACTION_PLACEHOLDER, "safe": "keep"})
        self.assertEqual(sanitized["array"], [{"authorization": REDACTION_PLACEHOLDER}, {"safe": 0}])
        self.assertEqual(source, original)

    def test_false_positive_keys_and_json_scalars_are_preserved(self):
        for value in (None, True, False, 0, "", "日本語", [], {"api_key_name": "keep", "secretary": "keep"}):
            sanitized, redacted = sanitize_json_value(value)
            with self.subTest(value=value):
                self.assertEqual(sanitized, value)
                self.assertFalse(redacted)

        sanitized, redacted = sanitize_json_value({"password": REDACTION_PLACEHOLDER})
        self.assertEqual(sanitized, {"password": REDACTION_PLACEHOLDER})
        self.assertFalse(redacted)

    def test_free_text_patterns_support_case_quotes_delimiters_and_multiple_matches(self):
        cases = {
            "Bearer abc123": "Bearer [REDACTED]",
            "bearer abc123": "Bearer [REDACTED]",
            "Basic dXNlcjpwYXNz": "Basic [REDACTED]",
            'api_key="abc123"': 'api_key="[REDACTED]"',
            "password='hunter2'": "password='[REDACTED]'",
            r'api_key="sec\"ret"': 'api_key="[REDACTED]"',
            r"api_key='sec\'ret'": "api_key='[REDACTED]'",
            r'password="abc\\def"': 'password="[REDACTED]"',
            r"client_secret='abc\\def'": "client_secret='[REDACTED]'",
            r'prefix api_key="one\"two" middle password="three\\four" suffix': 'prefix api_key="[REDACTED]" middle password="[REDACTED]" suffix',
            "api-key: abc123": "api-key: [REDACTED]",
            "x-api-key = abc123": "x-api-key = [REDACTED]",
            "access_token = abc123 client_secret: xyz": "access_token = [REDACTED] client_secret: [REDACTED]",
            "prefix Bearer abc123 suffix": "prefix Bearer [REDACTED] suffix",
            "ordinary text with api_key_name=keep": "ordinary text with api_key_name=keep",
            "Bearer [REDACTED]": "Bearer [REDACTED]",
            'quoted api_key="unterminated secret': 'quoted api_key="[REDACTED]',
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                sanitized, redacted = sanitize_text(source)
                self.assertEqual(sanitized, expected)
                self.assertEqual(redacted, source != expected)

        long_quoted = 'api_key="' + ("x" * 100_000) + '"'
        sanitized, redacted = sanitize_text(long_quoted)
        self.assertEqual(sanitized, 'api_key="[REDACTED]"')
        self.assertTrue(redacted)

        trailing_escape = 'api_key="unterminated' + "\\"
        sanitized, redacted = sanitize_text(trailing_escape)
        self.assertEqual(sanitized, 'api_key="[REDACTED]')
        self.assertTrue(redacted)

        span = make_span(
            input_value=[r'api_key="alpha\"omega"', r"client_secret='beta\\gamma'"],
        )
        serialized = span.to_json()
        for secret in ("alpha", "omega", "beta", "gamma"):
            self.assertNotIn(secret, serialized)
        self.assertIn(REDACTION_PLACEHOLDER, serialized)

    def test_redacted_flag_requires_an_actual_serialized_replacement(self):
        for value in (
            "Bearer [REDACTED]",
            'api_key="[REDACTED]"',
            "password='[REDACTED]'",
        ):
            with self.subTest(value=value):
                sanitized, redacted = sanitize_text(value)
                self.assertEqual(sanitized, value)
                self.assertFalse(redacted)

        sanitized, redacted = sanitize_json_value({"password": "secret"})
        self.assertEqual(sanitized, {"password": REDACTION_PLACEHOLDER})
        self.assertTrue(redacted)

        already_sanitized = make_span(input_value={"password": REDACTION_PLACEHOLDER})
        newly_sanitized = make_span(input_value={"password": "secret"})
        incorrectly_marked = make_span(
            input_value={"password": REDACTION_PLACEHOLDER},
            capture=Capture(
                CaptureInfo("captured", None, True),
                CaptureInfo("captured", None, False),
            ),
        )
        self.assertFalse(already_sanitized.capture.input.redacted)
        self.assertTrue(newly_sanitized.capture.input.redacted)
        self.assertFalse(incorrectly_marked.capture.input.redacted)

    def test_capture_info_precedence_and_null_semantics(self):
        invalid_source = object()
        value, info = capture_content(invalid_source, capture_content=True, source_available=True, not_yet_available=True)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "not_yet_available", False))

        value, info = capture_content(invalid_source, capture_content=False, source_available=False, not_yet_available=True)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "not_yet_available", False))

        value, info = capture_content(invalid_source, capture_content=False, source_available=False)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "disabled", False))

        value, info = capture_content(invalid_source, capture_content=True, source_available=False)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "source_unavailable", False))

        value, info = capture_content(invalid_source, capture_content=True)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "serialization_error", False))

        value, info = capture_content(None, capture_content=True)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("captured", None, False))

    def test_capture_normalization_failures_are_serialization_errors(self):
        cyclic = []
        cyclic.append(cyclic)
        values = (b"bytes", bytearray(b"bytearray"), datetime.now(), {"set"}, ("tuple",), object(), cyclic)
        for source in values:
            with self.subTest(source_type=type(source).__name__):
                value, info = capture_content(source, capture_content=True)
                self.assertIsNone(value)
                self.assertEqual(info, CaptureInfo("not_captured", "serialization_error", False))

    def test_content_limit_is_post_sanitization_utf8_canonical_json_and_not_truncated(self):
        exact = "x" * (MAX_CONTENT_BYTES - 2)
        value, info = capture_content(exact, capture_content=True)
        self.assertEqual(value, exact)
        self.assertEqual(info, CaptureInfo("captured", None, False))

        over = "x" * (MAX_CONTENT_BYTES - 1)
        value, info = capture_content(over, capture_content=True)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "size_limit", False))

        near_boundary = "api_key=" + ("x" * (MAX_CONTENT_BYTES - 2))
        value, info = capture_content(near_boundary, capture_content=True)
        self.assertEqual(value, "api_key=[REDACTED]")
        self.assertEqual(info, CaptureInfo("captured", None, True))

        multibyte = "日" * ((MAX_CONTENT_BYTES - 2) // 3) + "x" * 2
        value, info = capture_content(multibyte, capture_content=True)
        self.assertEqual(value, multibyte)
        self.assertEqual(info, CaptureInfo("captured", None, False))

        escaped = '"' * (MAX_CONTENT_BYTES - 2)
        value, info = capture_content(escaped, capture_content=True)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "size_limit", False))

        nested_exact = {"data": "x" * (MAX_CONTENT_BYTES - 11)}
        value, info = capture_content(nested_exact, capture_content=True)
        self.assertEqual(value, nested_exact)
        self.assertEqual(info, CaptureInfo("captured", None, False))

        nested_over = {"data": "x" * (MAX_CONTENT_BYTES - 10)}
        value, info = capture_content(nested_over, capture_content=True)
        self.assertIsNone(value)
        self.assertEqual(info, CaptureInfo("not_captured", "size_limit", False))

    def test_final_sanitized_name_and_operation_lengths_are_validated(self):
        trace_name = "x" * (256 - len(" Bearer x")) + " Bearer x"
        with self.assertRaises(ValidationError):
            Trace(
                AGENTLENS_SCHEMA_VERSION,
                TRACE_ID,
                trace_name,
                "2026-08-10T13:00:00.000000Z",
                None,
                "unset",
                TraceSource("framework", "1.0", "integration", "1.0", "native-trace"),
                {},
                {},
            )

        span_name = "x" * (256 - len(" Bearer x")) + " Bearer x"
        with self.assertRaises(ValidationError):
            make_span(name=span_name)

        operation = "x" * (128 - len(" Bearer x")) + " Bearer x"
        with self.assertRaises(ValidationError):
            make_span(operation=operation)

    def test_detail_field_authority_does_not_expand_free_text_sanitization(self):
        llm = LLMDetails(
            "llm",
            "Bearer provider-secret",
            "api_key=request-secret",
            "Bearer response-secret",
            "api_key=response-id-secret",
            LLMUsage(0, None, None, None, None),
            ["Bearer finish-secret"],
            None,
            None,
        )
        self.assertEqual(llm.provider, "Bearer provider-secret")
        self.assertEqual(llm.request_model, "api_key=request-secret")
        self.assertEqual(llm.response_model, "Bearer response-secret")
        self.assertEqual(llm.response_id, "api_key=response-id-secret")
        self.assertEqual(llm.finish_reasons, ["Bearer finish-secret"])

        agent = AgentDetails("agent", "Bearer agent-secret", None)
        handoff = HandoffDetails("handoff", "Bearer from-secret", "api_key=to-secret")
        tool = ToolDetails("tool", "Basic tool-secret", None)
        custom = CustomDetails("custom", "api_key=source-type")
        self.assertEqual(agent.agent_name, "Bearer [REDACTED]")
        self.assertEqual(handoff.from_agent, "Bearer [REDACTED]")
        self.assertEqual(handoff.to_agent, "api_key=[REDACTED]")
        self.assertEqual(tool.tool_name, "Basic [REDACTED]")
        self.assertEqual(custom.source_type, "api_key=source-type")

    def test_canonical_models_sanitize_all_intentionally_persisted_privacy_fields(self):
        details = LLMDetails(
            "llm",
            "provider",
            "request",
            "response",
            "response-id",
            LLMUsage(0, None, None, None, None),
            [],
            {"headers": {"Authorization": "Bearer framework-secret"}, "request": "api_key=parameter-secret"},
            None,
        )
        span = make_span(
            input_value={"password": "input-secret", "text": "Bearer input-bearer"},
            output_value=[{"client_secret": "output-secret"}],
            error=Error("Error", "failed with access_token=error-secret"),
            metadata={"cookie": "metadata-secret", "text": "Basic metadata-basic"},
            attributes={"nested": {"x-api-key": "attribute-secret"}},
            details=details,
            status="error",
        )

        serialized = span.to_json()
        for secret in (
            "input-secret",
            "input-bearer",
            "output-secret",
            "error-secret",
            "metadata-secret",
            "metadata-basic",
            "attribute-secret",
            "framework-secret",
            "parameter-secret",
        ):
            self.assertNotIn(secret, serialized)
        self.assertTrue(span.capture.input.redacted)
        self.assertTrue(span.capture.output.redacted)
        self.assertEqual(span.error.message, "failed with access_token=[REDACTED]")
        self.assertEqual(span.details.request_parameters["headers"]["Authorization"], REDACTION_PLACEHOLDER)

    def test_started_output_is_not_yet_available_even_if_source_like_value_is_supplied(self):
        with self.assertRaises(ValidationError):
            make_span(
                input_value=None,
                output_value=object(),
                capture=Capture(
                    CaptureInfo("captured", None, False),
                    CaptureInfo("not_captured", "not_yet_available", False),
                ),
                ended_at=None,
                status="unset",
            )

        span = make_span(
            input_value=None,
            output_value=None,
            capture=Capture(
                CaptureInfo("captured", None, False),
                CaptureInfo("not_captured", "not_yet_available", False),
            ),
            ended_at=None,
            status="unset",
        )
        self.assertIsNone(span.output)
        self.assertEqual(span.capture.output, CaptureInfo("not_captured", "not_yet_available", False))

    def test_canonical_span_boundary_also_applies_the_content_limit(self):
        span = make_span(
            input_value="x" * (MAX_CONTENT_BYTES - 1),
            output_value=None,
        )
        self.assertIsNone(span.input)
        self.assertEqual(span.capture.input, CaptureInfo("not_captured", "size_limit", False))


if __name__ == "__main__":
    unittest.main()
