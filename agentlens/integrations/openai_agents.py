"""OpenAI Agents SDK tracing adapter.

This module deliberately stops at the AgentLens SDK event boundary.  It does
not install itself into the OpenAI tracing provider; installation semantics
belong to Issue 09.  The processor can be constructed directly by internal
tests or by the later installation layer.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
from typing import Any
import warnings

from .. import privacy, sdk
from ..canonical import (
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
    ToolDetails,
    Trace,
    TraceSource,
    normalize_json_value,
)
from ..canonical.models import CanonicalModel

try:  # The core package remains importable without the optional SDK.
    from agents.tracing import TracingProcessor as _TracingProcessor
except Exception:  # pragma: no cover - exercised when the optional SDK is absent.
    class _TracingProcessor:
        """Small fallback base used by deterministic adapter tests."""


try:
    import agents as _agents
except Exception:  # pragma: no cover - exercised when the optional SDK is absent.
    _agents = None


OPENAI_FRAMEWORK = "openai-agents"
OPENAI_INTEGRATION = "openai_agents"
INTEGRATION_VERSION = "0.1"
_MISSING = object()
_OPTIONAL = object()
_RESPONSE_SKIP = object()
_MAX_RETAINED_TRACES = 4096
_MAX_RETAINED_SPANS = 16384

_REQUEST_PARAMETER_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "tool_choice",
        "parallel_tool_calls",
        "truncation",
        "max_tokens",
        "reasoning",
        "verbosity",
        "store",
        "prompt_cache_retention",
        "include_usage",
        "response_include",
        "top_logprobs",
        "retry",
        "context_management",
        "prompt_cache_options",
        "preserve_raw_usage",
    }
)


def _framework_version() -> str | None:
    value = getattr(_agents, "__version__", None) if _agents is not None else None
    return value if isinstance(value, str) else None


def _safe_attr(value: Any, name: str, default: Any = _MISSING) -> Any:
    """Read one explicitly allowlisted SDK field without introspection."""

    try:
        result = getattr(value, name)
    except AttributeError:
        if default is _MISSING:
            raise
        return default
    return result


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null")
    return value


def _canonical_timestamp(value: Any, field_name: str) -> str:
    """Normalize an SDK ISO timestamp to the Canonical UTC wire form."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _callback_timestamp() -> str:
    return sdk._timestamp()


def _warn_missing_timestamp(field_name: str) -> None:
    # Keep the warning deterministic and free of framework values/content.
    warnings.warn(
        f"AgentLens dropped OpenAI span callback: missing or invalid {field_name}",
        RuntimeWarning,
        stacklevel=3,
    )


def _mapping_value(value: Any, key: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
        if default is _MISSING:
            raise KeyError(key)
        return default
    result = _safe_attr(value, key, default)
    return result


def _json_input(value: Any) -> Any:
    """Apply FunctionSpanData's only special input rule."""

    if not isinstance(value, str):
        return value
    try:
        return json.loads(value, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _capture(
    source: Any,
    *,
    available: bool,
    capture_content: bool,
    input_value: bool = False,
) -> tuple[Any, CaptureInfo]:
    if not capture_content:
        return None, CaptureInfo("not_captured", "disabled", False)
    return privacy.capture_content(
        source,
        capture_content=True,
        source_available=available,
    )


def _unavailable_capture() -> tuple[None, CaptureInfo]:
    return None, CaptureInfo("not_captured", "source_unavailable", False)


def _not_yet_available_capture() -> CaptureInfo:
    return CaptureInfo("not_captured", "not_yet_available", False)


def _restore_capture_observation(span: Span, capture: Capture) -> Span:
    """Keep the Issue 02 redaction evidence from the adapter pass.

    Canonical ``Span`` construction sanitizes again.  A second pass over an
    already-redacted value cannot know that the first pass replaced a secret,
    so restore only the already-sanitized CaptureInfo pair captured by the
    adapter.  No raw source value crosses this boundary.
    """

    object.__setattr__(span, "capture", capture)
    return span


def _normalize_parameter_value(value: Any, key: str) -> Any:
    normalized = normalize_json_value(value, field_name=f"request_parameters.{key}")
    sanitized, _ = privacy.sanitize_json_value(
        normalized,
        field_name=f"request_parameters.{key}",
    )
    return sanitized


def _request_parameters(model_config: Any) -> dict[str, Any] | None:
    if model_config is None:
        return None
    if not isinstance(model_config, Mapping):
        return {}

    result: dict[str, Any] = {}
    try:
        items = model_config.items()
        for key, value in items:
            if key not in _REQUEST_PARAMETER_KEYS:
                continue
            try:
                result[key] = _normalize_parameter_value(value, key)
            except Exception:
                # One unsupported provider value must not hide other approved
                # request controls or escape into Agent execution.
                continue
    except Exception:
        return result
    return result


def _numeric_usage_value(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage_field(usage: Any, key: str) -> int | None:
    try:
        value = _mapping_value(usage, key, None)
    except Exception:
        return None
    return _numeric_usage_value(value)


def _usage(usage: Any) -> LLMUsage:
    if usage is None:
        return LLMUsage(None, None, None, None, None)

    input_tokens = _usage_field(usage, "input_tokens")
    output_tokens = _usage_field(usage, "output_tokens")

    reasoning = _usage_field(usage, "reasoning_tokens")
    if reasoning is None:
        try:
            reasoning = _usage_field(
                _mapping_value(usage, "output_tokens_details", None),
                "reasoning_tokens",
            )
        except Exception:
            reasoning = None

    cached = _usage_field(usage, "cached_tokens")
    if cached is None:
        try:
            cached = _usage_field(
                _mapping_value(usage, "input_tokens_details", None),
                "cached_tokens",
            )
        except Exception:
            cached = None

    cache_creation = _usage_field(usage, "cache_creation_input_tokens")
    return LLMUsage(input_tokens, output_tokens, reasoning, cached, cache_creation)


def _response_field(value: Any, name: str, default: Any = _MISSING) -> Any:
    """Read one approved field from an OpenAI Responses output value."""

    if isinstance(value, Mapping):
        if name in value:
            return value[name]
        return default
    return _safe_attr(value, name, default)


def _response_content_block(value: Any) -> Any:
    """Normalize only the textual content forms in Response.output."""

    try:
        kind = _response_field(value, "type", _MISSING)
        if kind == "output_text":
            text = _response_field(value, "text", _MISSING)
            if isinstance(text, str):
                return {"type": "output_text", "text": text}
            return _RESPONSE_SKIP
        if kind == "refusal":
            refusal = _response_field(value, "refusal", _MISSING)
            if isinstance(refusal, str):
                return {"type": "refusal", "refusal": refusal}
    except BaseException:
        return _RESPONSE_SKIP
    return _RESPONSE_SKIP


def _response_output_item(value: Any) -> Any:
    """Normalize one allowlisted Responses output item without model dumping."""

    try:
        kind = _response_field(value, "type", _MISSING)
        if kind == "message":
            content = _response_field(value, "content", _MISSING)
            if not isinstance(content, (list, tuple)):
                return _RESPONSE_SKIP
            normalized_content = []
            for block in content:
                normalized = _response_content_block(block)
                if normalized is not _RESPONSE_SKIP:
                    normalized_content.append(normalized)
            return {"type": "message", "content": normalized_content}
        return _response_content_block(value)
    except BaseException:
        return _RESPONSE_SKIP


def _normalize_response_output(value: Any) -> Any:
    """Return a bounded, content-only JSON shape for Response.output."""

    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        normalized = []
        for item in value:
            mapped = _response_output_item(item)
            if mapped is not _RESPONSE_SKIP:
                normalized.append(mapped)
        return normalized
    mapped = _response_output_item(value)
    return [] if mapped is _RESPONSE_SKIP else [mapped]


@dataclass(frozen=True, slots=True)
class _TraceState:
    snapshot: Trace
    terminal: bool = False
    synthetic: bool = False


@dataclass(frozen=True, slots=True)
class _SpanState:
    snapshot: Span
    active: bool


@dataclass(frozen=True, slots=True)
class _RetainedTrace:
    """Small sanitized trace identity retained after active state is released."""

    trace_id: str
    name: str
    started_at: str
    source: TraceSource


class _MissingTimestamp(ValueError):
    pass


class AgentLensOpenAIProcessor(_TracingProcessor):
    """Internal Issue 08 OpenAI Agents SDK ``TracingProcessor`` adapter."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._trace_ids: OrderedDict[str, str] = OrderedDict()
        self._traces: dict[str, _TraceState] = {}
        self._retained_traces: OrderedDict[str, _RetainedTrace] = OrderedDict()
        self._span_ids: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._retained_span_ids: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._spans: dict[tuple[str, str], _SpanState] = {}

    @property
    def trace_mapping(self) -> dict[str, str]:
        """Return a copy for deterministic internal diagnostics/tests."""

        with self._lock:
            return dict(self._trace_ids)

    @property
    def span_mapping(self) -> dict[tuple[str, str], str]:
        """Return a copy for deterministic internal diagnostics/tests."""

        with self._lock:
            return dict(self._span_ids)

    def on_trace_start(self, trace: Any) -> None:
        try:
            self._emit_trace(trace, ended=False)
        except BaseException:
            return

    def on_trace_end(self, trace: Any) -> None:
        try:
            self._emit_trace(trace, ended=True)
        except BaseException:
            return

    def on_span_start(self, span: Any) -> None:
        try:
            self._emit_span(span, ended=False)
        except BaseException:
            return

    def on_span_end(self, span: Any) -> None:
        try:
            self._emit_span(span, ended=True)
        except BaseException:
            return

    def force_flush(self) -> None:
        try:
            sdk.flush()
        except BaseException:
            return

    def shutdown(self) -> None:
        try:
            self.force_flush()
        finally:
            with self._lock:
                self._trace_ids.clear()
                self._traces.clear()
                self._retained_traces.clear()
                self._span_ids.clear()
                self._retained_span_ids.clear()
                self._spans.clear()

    def _emit_trace(self, native_trace: Any, *, ended: bool) -> None:
        def build(configuration: Any) -> Trace:
            del configuration
            with self._lock:
                native_id = _required_string(
                    _safe_attr(native_trace, "trace_id"),
                    "trace.trace_id",
                )
                state = self._traces.get(native_id)
                if state is None:
                    canonical_id = self._trace_ids.get(native_id)
                    if canonical_id is None:
                        canonical_id = sdk._new_trace_id()
                        self._trace_ids[native_id] = canonical_id
                    retained = self._retained_traces.get(native_id)
                    if retained is not None:
                        self._retained_traces.move_to_end(native_id)
                        start_snapshot = Trace(
                            AGENTLENS_SCHEMA_VERSION,
                            retained.trace_id,
                            retained.name,
                            retained.started_at,
                            None,
                            "unset",
                            retained.source,
                            {},
                            {},
                        )
                    else:
                        name = _required_string(
                            _safe_attr(native_trace, "name"),
                            "trace.name",
                        )
                        started = _callback_timestamp()
                        source = TraceSource(
                            OPENAI_FRAMEWORK,
                            _framework_version(),
                            OPENAI_INTEGRATION,
                            INTEGRATION_VERSION,
                            native_id,
                        )
                        start_snapshot = Trace(
                            AGENTLENS_SCHEMA_VERSION,
                            canonical_id,
                            name,
                            started,
                            None,
                            "unset",
                            source,
                            {},
                            {},
                        )
                    state = _TraceState(start_snapshot)
                    self._traces[native_id] = state
                elif state.synthetic and not ended:
                    name = _required_string(
                        _safe_attr(native_trace, "name"),
                        "trace.name",
                    )
                    start_snapshot = Trace(
                        AGENTLENS_SCHEMA_VERSION,
                        state.snapshot.trace_id,
                        name,
                        _callback_timestamp(),
                        None,
                        "unset",
                        state.snapshot.source,
                        {},
                        {},
                    )
                    state = _TraceState(start_snapshot)
                    self._traces[native_id] = state
                if not ended:
                    return state.snapshot

                finished = Trace(
                    AGENTLENS_SCHEMA_VERSION,
                    state.snapshot.trace_id,
                    state.snapshot.name,
                    state.snapshot.started_at,
                    _callback_timestamp(),
                    self._trace_outcome(native_trace),
                    state.snapshot.source,
                    state.snapshot.metadata,
                    state.snapshot.attributes,
                )
                self._traces[native_id] = _TraceState(finished, terminal=True)
                self._cleanup_trace_if_quiescent(native_id)
                return finished

        sdk._create_first_event(build, "trace.ended" if ended else "trace.started")

    @staticmethod
    def _trace_outcome(native_trace: Any) -> str:
        # The supported TracingProcessor Trace contract has no top-level
        # outcome field.  on_trace_end is therefore deliberately not success.
        del native_trace
        return "unset"

    def _emit_span(self, native_span: Any, *, ended: bool) -> None:
        def build(configuration: Any) -> Span:
            with self._lock:
                native_trace_id = _required_string(
                    _safe_attr(native_span, "trace_id"),
                    "span.trace_id",
                )
                native_span_id = _required_string(
                    _safe_attr(native_span, "span_id"),
                    "span.span_id",
                )
                native_parent_id = _safe_attr(native_span, "parent_id", None)
                native_parent_id = _optional_string(native_parent_id, "span.parent_id")
                trace_state = self._ensure_trace_state(native_trace_id)
                key = (native_trace_id, native_span_id)
                canonical_span_id = self._span_id_for(key)
                canonical_parent_id = (
                    None
                    if native_parent_id is None
                    else self._span_id_for((native_trace_id, native_parent_id))
                )
                callback_started_at = self._validated_callback_started_at(native_span)
                previous = self._spans.get(key)

                if ended:
                    snapshot = self._build_ended_span(
                        configuration,
                        native_span,
                        native_trace_id,
                        native_span_id,
                        native_parent_id,
                        trace_state,
                        canonical_span_id,
                        canonical_parent_id,
                        previous,
                        callback_started_at,
                    )
                    # Terminal snapshots are no longer needed for carry-forward.
                    # The native identity maps above remain stable for late callbacks.
                    self._spans.pop(key, None)
                    self._cleanup_trace_if_quiescent(native_trace_id)
                    return snapshot

                snapshot = self._build_started_span(
                    configuration,
                    native_span,
                    native_trace_id,
                    native_span_id,
                    native_parent_id,
                    canonical_span_id,
                    canonical_parent_id,
                    previous,
                    callback_started_at,
                )
                self._spans[key] = _SpanState(
                    snapshot,
                    active=previous is None or previous.active,
                )
                return snapshot

        sdk._create_first_event(build, "span.ended" if ended else "span.started")

    def _ensure_trace_state(self, native_trace_id: str) -> _TraceState:
        state = self._traces.get(native_trace_id)
        if state is not None:
            return state
        retained = self._retained_traces.get(native_trace_id)
        if retained is not None:
            self._retained_traces.move_to_end(native_trace_id)
            snapshot = Trace(
                AGENTLENS_SCHEMA_VERSION,
                retained.trace_id,
                retained.name,
                retained.started_at,
                None,
                "unset",
                retained.source,
                {},
                {},
            )
            state = _TraceState(snapshot, terminal=True)
            self._traces[native_trace_id] = state
            return state
        canonical_id = self._trace_ids.get(native_trace_id)
        if canonical_id is None:
            canonical_id = sdk._new_trace_id()
            self._trace_ids[native_trace_id] = canonical_id
        now = _callback_timestamp()
        source = TraceSource(
            OPENAI_FRAMEWORK,
            _framework_version(),
            OPENAI_INTEGRATION,
            INTEGRATION_VERSION,
            native_trace_id,
        )
        snapshot = Trace(
            AGENTLENS_SCHEMA_VERSION,
            canonical_id,
            "OpenAI workflow",
            now,
            None,
            "unset",
            source,
            {},
            {},
        )
        state = _TraceState(snapshot, synthetic=True)
        self._traces[native_trace_id] = state
        return state

    @staticmethod
    def _validated_callback_started_at(native_span: Any) -> str:
        value = _safe_attr(native_span, "started_at", None)
        try:
            return _canonical_timestamp(value, "span.started_at")
        except (TypeError, ValueError):
            _warn_missing_timestamp("span.started_at")
            raise _MissingTimestamp("span.started_at is required")

    def _span_id_for(self, key: tuple[str, str]) -> str:
        result = self._span_ids.get(key)
        if result is None:
            result = sdk._new_span_id()
            self._span_ids[key] = result
        else:
            self._span_ids.move_to_end(key)
            if key in self._retained_span_ids:
                self._retained_span_ids.move_to_end(key)
        return result

    def _build_started_span(
        self,
        configuration: Any,
        native_span: Any,
        native_trace_id: str,
        native_span_id: str,
        native_parent_id: str | None,
        canonical_span_id: str,
        canonical_parent_id: str | None,
        previous: _SpanState | None,
        started_at: str,
    ) -> Span:
        data = _safe_attr(native_span, "span_data")
        kind, operation, name, details, input_source, input_available = self._map_data(
            data,
            capture_content=configuration.capture_content,
        )
        input_value, input_capture = _capture(
            input_source,
            available=input_available,
            capture_content=configuration.capture_content,
        )
        source = SpanSource(
            OPENAI_FRAMEWORK,
            _framework_version(),
            OPENAI_INTEGRATION,
            INTEGRATION_VERSION,
            native_trace_id,
            native_span_id,
            native_parent_id,
        )
        if previous is not None and previous.snapshot.ended_at is not None:
            # A late start is stale, but it still carries the exact native
            # identity and current start observation for the collector's
            # end-before-start merge rules.
            pass
        started = Span(
            AGENTLENS_SCHEMA_VERSION,
            self._traces[native_trace_id].snapshot.trace_id,
            canonical_span_id,
            canonical_parent_id,
            kind,
            operation,
            name,
            started_at,
            None,
            "unset",
            None,
            input_value,
            None,
            Capture(input_capture, _not_yet_available_capture()),
            source,
            {},
            {},
            details,
        )
        return _restore_capture_observation(
            started,
            Capture(input_capture, _not_yet_available_capture()),
        )

    def _build_ended_span(
        self,
        configuration: Any,
        native_span: Any,
        native_trace_id: str,
        native_span_id: str,
        native_parent_id: str | None,
        trace_state: _TraceState,
        canonical_span_id: str,
        canonical_parent_id: str | None,
        previous: _SpanState | None,
        callback_started_at: str,
    ) -> Span:
        previous_snapshot = previous.snapshot if previous is not None else None
        if previous_snapshot is None:
            data = _safe_attr(native_span, "span_data")
            kind, operation, name, details, input_source, input_available = self._map_data(
                data,
                capture_content=configuration.capture_content,
            )
            started_at = callback_started_at
            input_value, input_capture = _capture(
                input_source,
                available=input_available,
                capture_content=configuration.capture_content,
            )
            source = SpanSource(
                OPENAI_FRAMEWORK,
                _framework_version(),
                OPENAI_INTEGRATION,
                INTEGRATION_VERSION,
                native_trace_id,
                native_span_id,
                native_parent_id,
            )
            metadata: dict[str, Any] = {}
            attributes: dict[str, Any] = {}
        else:
            kind = previous_snapshot.type
            operation = previous_snapshot.operation
            name = previous_snapshot.name
            details = self._merge_details(
                previous_snapshot.details,
                self._map_data(
                    _safe_attr(native_span, "span_data"),
                    capture_content=configuration.capture_content,
                )[3],
            )
            started_at = previous_snapshot.started_at
            input_value, input_capture = self._end_input(
                configuration,
                native_span,
                previous_snapshot,
            )
            source = previous_snapshot.source
            metadata = dict(previous_snapshot.metadata)
            attributes = dict(previous_snapshot.attributes)

        output_value, output_capture = self._end_output(
            configuration,
            native_span,
            previous_snapshot,
            kind,
        )
        raw_error = _safe_attr(native_span, "error", None)
        error = self._map_error(raw_error)
        has_framework_error = raw_error is not None
        status = "error" if has_framework_error else "ok"
        ended_at_value = _safe_attr(native_span, "ended_at", None)
        ended_at = (
            _canonical_timestamp(ended_at_value, "span.ended_at")
            if ended_at_value is not None
            else _callback_timestamp()
        )
        ended = Span(
            AGENTLENS_SCHEMA_VERSION,
            trace_state.snapshot.trace_id,
            canonical_span_id,
            canonical_parent_id if previous_snapshot is None else previous_snapshot.parent_span_id,
            kind,
            operation,
            name,
            started_at,
            ended_at,
            status,
            error,
            input_value,
            output_value,
            Capture(input_capture, output_capture),
            source,
            metadata,
            attributes,
            details,
        )
        return _restore_capture_observation(
            ended,
            Capture(input_capture, output_capture),
        )

    def _end_input(
        self,
        configuration: Any,
        native_span: Any,
        previous: Span,
    ) -> tuple[Any, CaptureInfo]:
        """Enrich an uncaptured start input from a later captured end input."""

        if previous.capture.input.state == "captured":
            return previous.input, previous.capture.input
        if not configuration.capture_content:
            return None, CaptureInfo("not_captured", "disabled", False)

        try:
            data = _safe_attr(native_span, "span_data")
            _, _, _, _, input_source, input_available = self._map_data(
                data,
                capture_content=True,
            )
        except BaseException:
            return previous.input, previous.capture.input
        value, capture = _capture(
            input_source,
            available=input_available,
            capture_content=True,
        )
        if capture.state == "captured":
            return value, capture
        return previous.input, previous.capture.input

    def _end_output(
        self,
        configuration: Any,
        native_span: Any,
        previous: Span | None,
        kind: str,
    ) -> tuple[Any, CaptureInfo]:
        if not configuration.capture_content:
            return None, CaptureInfo("not_captured", "disabled", False)
        source_available, source = self._output_source(native_span, kind)
        value, capture = _capture(
            source,
            available=source_available,
            capture_content=True,
        )
        if (
            previous is not None
            and previous.capture.output.state == "captured"
            and capture.state != "captured"
        ):
            return previous.output, previous.capture.output
        return value, capture

    @staticmethod
    def _output_source(native_span: Any, kind: str) -> tuple[bool, Any]:
        data = _safe_attr(native_span, "span_data")
        if kind == "llm" and _safe_attr(data, "type", None) == "response":
            response = _safe_attr(data, "response", None)
            if response is None:
                return False, None
            value = _safe_attr(response, "output", _OPTIONAL)
            if value is _OPTIONAL:
                return False, None
            return True, _normalize_response_output(value)
        if kind in {"llm", "tool"}:
            value = _safe_attr(data, "output", _OPTIONAL)
            return value is not _OPTIONAL, value
        return False, None

    def _map_data(
        self,
        data: Any,
        *,
        capture_content: bool,
    ) -> tuple[str, str, str, CanonicalModel, Any, bool]:
        source_type = _required_string(
            _safe_attr(data, "type", type(data).__name__),
            "span_data.type",
        )
        if source_type == "agent":
            name = _required_string(_safe_attr(data, "name"), "AgentSpanData.name")
            return (
                "agent",
                "agent.run",
                name,
                AgentDetails("agent", name, None),
                None,
                False,
            )
        if source_type == "generation":
            model = _optional_string(_safe_attr(data, "model", None), "GenerationSpanData.model")
            params = _request_parameters(_safe_attr(data, "model_config", None))
            details = LLMDetails(
                "llm",
                None,
                model,
                None,
                None,
                _usage(_safe_attr(data, "usage", None)),
                [],
                params,
                None,
            )
            input_value = _safe_attr(data, "input", _OPTIONAL) if capture_content else _OPTIONAL
            input_available = input_value is not _OPTIONAL
            if not input_available:
                input_value = None
            return "llm", "llm.generate", "generation", details, input_value, input_available
        if source_type == "response":
            response = _safe_attr(data, "response", None)
            provider = None
            response_id = None
            response_model = None
            if response is not None:
                provider = "openai"
                response_id = _optional_string(_safe_attr(response, "id", None), "Response.id")
                response_model = _optional_string(
                    _safe_attr(response, "model", None),
                    "Response.model",
                )
            details = LLMDetails(
                "llm",
                provider,
                None,
                response_model,
                response_id,
                _usage(_safe_attr(data, "usage", None)),
                [],
                None,
                None,
            )
            input_value = _safe_attr(data, "input", _OPTIONAL) if capture_content else _OPTIONAL
            input_available = input_value is not _OPTIONAL
            if not input_available:
                input_value = None
            # Response output is handled by _output_source so it cannot be
            # accidentally replaced by a serialization of the response.
            del capture_content
            return "llm", "llm.response", "response", details, input_value, input_available
        if source_type == "function":
            name = _required_string(_safe_attr(data, "name"), "FunctionSpanData.name")
            input_value = _safe_attr(data, "input", _OPTIONAL) if capture_content else _OPTIONAL
            input_available = input_value is not _OPTIONAL
            if not input_available:
                input_value = None
            elif capture_content:
                input_value = _json_input(input_value)
            return (
                "tool",
                "tool.execute",
                name,
                ToolDetails("tool", name, None),
                input_value,
                input_available,
            )
        if source_type == "handoff":
            from_agent = _optional_string(_safe_attr(data, "from_agent", None), "Handoff.from_agent")
            to_agent = _optional_string(_safe_attr(data, "to_agent", None), "Handoff.to_agent")
            return (
                "handoff",
                "handoff",
                "handoff",
                HandoffDetails("handoff", from_agent, to_agent),
                None,
                False,
            )

        source_name = _safe_attr(data, "name", None)
        if not isinstance(source_name, str) or source_name == "":
            source_name = source_type
        return (
            "custom",
            f"openai.{source_type}",
            source_name,
            CustomDetails("custom", source_type),
            None,
            False,
        )

    @staticmethod
    def _merge_details(previous: CanonicalModel, current: CanonicalModel) -> CanonicalModel:
        if not isinstance(previous, LLMDetails) or not isinstance(current, LLMDetails):
            return previous
        previous_usage = previous.usage
        current_usage = current.usage
        usage = LLMUsage(
            current_usage.input_tokens
            if current_usage.input_tokens is not None
            else previous_usage.input_tokens,
            current_usage.output_tokens
            if current_usage.output_tokens is not None
            else previous_usage.output_tokens,
            current_usage.reasoning_output_tokens
            if current_usage.reasoning_output_tokens is not None
            else previous_usage.reasoning_output_tokens,
            current_usage.cache_read_input_tokens
            if current_usage.cache_read_input_tokens is not None
            else previous_usage.cache_read_input_tokens,
            current_usage.cache_creation_input_tokens
            if current_usage.cache_creation_input_tokens is not None
            else previous_usage.cache_creation_input_tokens,
        )
        if previous.request_parameters is None:
            request_parameters = current.request_parameters
        elif current.request_parameters is None:
            request_parameters = previous.request_parameters
        else:
            request_parameters = dict(previous.request_parameters)
            request_parameters.update(current.request_parameters)
        return LLMDetails(
            "llm",
            current.provider if current.provider is not None else previous.provider,
            current.request_model if current.request_model is not None else previous.request_model,
            current.response_model if current.response_model is not None else previous.response_model,
            current.response_id if current.response_id is not None else previous.response_id,
            usage,
            [],
            request_parameters,
            current.estimated_cost if current.estimated_cost is not None else previous.estimated_cost,
        )

    def _map_error(self, value: Any) -> Error | None:
        if not isinstance(value, Mapping):
            return None
        try:
            message = value.get("message")
        except BaseException:
            return None
        if not isinstance(message, str):
            return None
        sanitized, _ = privacy.sanitize_text(message)
        return Error(None, sanitized)

    def _cleanup_trace_if_quiescent(self, native_trace_id: str) -> None:
        state = self._traces.get(native_trace_id)
        if state is None or not state.terminal:
            return
        if any(
            key[0] == native_trace_id and span_state.active
            for key, span_state in self._spans.items()
        ):
            return
        retained = _RetainedTrace(
            state.snapshot.trace_id,
            state.snapshot.name,
            state.snapshot.started_at,
            state.snapshot.source,
        )
        self._retained_traces[native_trace_id] = retained
        self._retained_traces.move_to_end(native_trace_id)
        self._traces.pop(native_trace_id, None)
        for key in [key for key in self._span_ids if key[0] == native_trace_id]:
            self._retained_span_ids[key] = None
            self._retained_span_ids.move_to_end(key)
            self._spans.pop(key, None)
        self._evict_retained_identities()

    def _evict_retained_identities(self) -> None:
        while len(self._retained_traces) > _MAX_RETAINED_TRACES:
            native_trace_id, _ = self._retained_traces.popitem(last=False)
            self._trace_ids.pop(native_trace_id, None)
            for key in [key for key in self._retained_span_ids if key[0] == native_trace_id]:
                self._retained_span_ids.pop(key, None)
                self._span_ids.pop(key, None)
        while len(self._retained_span_ids) > _MAX_RETAINED_SPANS:
            key, _ = self._retained_span_ids.popitem(last=False)
            self._span_ids.pop(key, None)


OpenAITracingProcessor = AgentLensOpenAIProcessor

__all__ = ["AgentLensOpenAIProcessor", "OpenAITracingProcessor"]
