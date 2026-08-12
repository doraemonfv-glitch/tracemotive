"""The framework-independent AgentLens v0.1 Python SDK core."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import secrets
import threading
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from . import privacy
from .canonical import (
    AGENTLENS_SCHEMA_VERSION,
    Capture,
    CaptureInfo,
    CustomDetails,
    Error,
    Span,
    SpanSource,
    Trace,
    TraceSource,
)
from .canonical.models import CanonicalModel, _canonical_json_dumps
from .transport import LocalTransport


DEFAULT_ENDPOINT = "http://127.0.0.1:8765"
SDK_INTEGRATION = "agentlens.manual"
SDK_INTEGRATION_VERSION = "0.1"
MAX_EVENT_BYTES = 1_048_576


class AgentLensConfigurationError(ValueError):
    """Raised when SDK configuration is invalid or already frozen."""


@dataclass(frozen=True, slots=True)
class _Configuration:
    enabled: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    capture_content: bool = False


@dataclass(frozen=True, slots=True)
class _CreatedEvent:
    configuration: _Configuration
    payload: CanonicalModel
    event: dict[str, Any]


_state_lock = threading.RLock()
_configuration = _Configuration()
_configuration_frozen = False
_event_sink: Any = None
_transport_sink: LocalTransport | None = None

_current_trace: ContextVar[Trace | None] = ContextVar(
    "agentlens_current_trace", default=None
)
_current_span: ContextVar["SpanHandle | None"] = ContextVar(
    "agentlens_current_span", default=None
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_trace_id() -> str:
    while True:
        value = secrets.token_hex(16)
        if value != "0" * 32:
            return value


def _new_span_id() -> str:
    while True:
        value = secrets.token_hex(8)
        if value != "0" * 16:
            return value


def _new_event_id() -> str:
    value = uuid4()
    if value.version != 4:
        raise RuntimeError("uuid4 returned a non-v4 UUID")
    return str(value)


def _with_capture_history(span: Span, capture: Capture) -> Span:
    """Retain trusted CaptureInfo for an already-normalized observation."""

    # Span construction intentionally re-sanitizes content.  The SDK stores
    # only the sanitized value after capture, so that pass cannot rediscover
    # whether an earlier pass removed a secret.  CaptureInfo is the trusted
    # historical result of that earlier observation and must remain paired
    # with its sanitized value across lifecycle events.
    object.__setattr__(span, "capture", capture)
    return span


def _validate_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, str):
        raise AgentLensConfigurationError("endpoint must be a string")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        parsed.port  # Validate malformed or out-of-range ports.
    except ValueError as exc:
        raise AgentLensConfigurationError("endpoint must be a loopback HTTP URL") from exc

    if parsed.scheme.casefold() != "http" or hostname is None:
        raise AgentLensConfigurationError("endpoint must be a loopback HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise AgentLensConfigurationError("endpoint must not contain credentials")
    if hostname.casefold() == "localhost":
        return endpoint
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise AgentLensConfigurationError(
            "endpoint host must be localhost or a loopback IP address"
        ) from exc
    if not address.is_loopback:
        raise AgentLensConfigurationError(
            "endpoint host must be localhost or a loopback IP address"
        )
    return endpoint


def configure(
    *,
    enabled: bool = False,
    endpoint: str = DEFAULT_ENDPOINT,
    capture_content: bool = False,
) -> None:
    """Configure the process-global SDK before its first event."""

    if type(enabled) is not bool:
        raise AgentLensConfigurationError("enabled must be a boolean")
    if type(capture_content) is not bool:
        raise AgentLensConfigurationError("capture_content must be a boolean")
    endpoint = _validate_endpoint(endpoint)
    requested = _Configuration(enabled, endpoint, capture_content)

    global _configuration
    with _state_lock:
        if _configuration_frozen:
            if requested != _configuration:
                raise AgentLensConfigurationError(
                    "AgentLens configuration is frozen after the first event"
                )
            return
        _configuration = requested


def current_trace() -> Trace | None:
    """Return the current context-local canonical Trace, for internal use."""

    return _current_trace.get()


def current_span() -> "SpanHandle | None":
    """Return the current context-local manual Span handle, for internal use."""

    return _current_span.get()


get_current_trace = current_trace
get_current_span = current_span


def _deliver(event: dict[str, Any]) -> None:
    global _transport_sink
    with _state_lock:
        sink = _event_sink
        if sink is None and _configuration.enabled:
            sink = _transport_sink
            if sink is None:
                try:
                    sink = LocalTransport(_configuration.endpoint)
                except BaseException:
                    sink = None
                else:
                    _transport_sink = sink
    if sink is None:
        return
    try:
        emitter = getattr(sink, "emit", None)
        if callable(emitter):
            emitter(event)
        elif callable(sink):
            sink(event)
    except Exception:
        # Do not log the event: it may contain user-derived content.
        return


def _event_envelope(event_type: str, payload: CanonicalModel) -> dict[str, Any]:
    event = {
        "event_id": _new_event_id(),
        "event_type": event_type,
        "emitted_at": _timestamp(),
        "payload": payload.to_dict(),
    }
    if len(_canonical_json_dumps(event).encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("event exceeds the local canonical event size limit")
    return event


def _create_first_event(
    payload_factory: Callable[[_Configuration], CanonicalModel],
    event_type: str,
) -> _CreatedEvent | None:
    """Create the first event and freeze its configuration atomically."""

    global _configuration_frozen
    with _state_lock:
        configuration = _configuration
        if not configuration.enabled:
            return None
        try:
            payload = payload_factory(configuration)
            event = _event_envelope(event_type, payload)
        except Exception:
            return None
        _configuration_frozen = True
    created = _CreatedEvent(configuration, payload, event)
    _deliver(event)
    return created


def _create_lifecycle_event(
    configuration: _Configuration,
    payload_factory: Callable[[_Configuration], CanonicalModel],
    event_type: str,
) -> None:
    with _state_lock:
        try:
            payload = payload_factory(configuration)
            event = _event_envelope(event_type, payload)
        except Exception:
            return
    _deliver(event)


def _manual_trace_source() -> TraceSource:
    return TraceSource(None, None, SDK_INTEGRATION, SDK_INTEGRATION_VERSION, None)


def _manual_span_source() -> SpanSource:
    return SpanSource(None, None, SDK_INTEGRATION, SDK_INTEGRATION_VERSION, None, None, None)


class _TraceContext:
    def __init__(self, name: Any, metadata: Any) -> None:
        self._name = name
        self._metadata = metadata
        self._trace: Trace | None = None
        self._configuration: _Configuration | None = None
        self._trace_token: Token[Trace | None] | None = None
        self._span_token: Token[SpanHandle | None] | None = None
        self._active = False

    def __enter__(self) -> Trace | None:
        def build(configuration: _Configuration) -> Trace:
            return Trace(
                AGENTLENS_SCHEMA_VERSION,
                _new_trace_id(),
                self._name,
                _timestamp(),
                None,
                "unset",
                _manual_trace_source(),
                {} if self._metadata is None else self._metadata,
                {},
            )

        created = _create_first_event(build, "trace.started")
        if created is None:
            self._name = None
            self._metadata = None
            return None
        self._trace = created.payload  # type: ignore[assignment]
        self._configuration = created.configuration
        self._trace_token = _current_trace.set(self._trace)
        self._span_token = _current_span.set(None)
        self._active = True
        self._name = None
        self._metadata = None
        return self._trace

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if not self._active or self._trace is None or self._configuration is None:
            return False
        trace = self._trace
        configuration = self._configuration
        status = "error" if exc_type is not None else "ok"
        try:
            _create_lifecycle_event(
                configuration,
                lambda _configuration: Trace(
                    AGENTLENS_SCHEMA_VERSION,
                    trace.trace_id,
                    trace.name,
                    trace.started_at,
                    _timestamp(),
                    status,
                    trace.source,
                    trace.metadata,
                    trace.attributes,
                ),
                "trace.ended",
            )
        except Exception:
            pass
        finally:
            self._restore_context()
            self._active = False
            self._trace = None
            self._configuration = None
            self._name = None
            self._metadata = None
        return False

    def _restore_context(self) -> None:
        if self._span_token is not None:
            try:
                _current_span.reset(self._span_token)
            except Exception:
                pass
            self._span_token = None
        if self._trace_token is not None:
            try:
                _current_trace.reset(self._trace_token)
            except Exception:
                pass
            self._trace_token = None


def trace(name: Any, metadata: Any = None) -> _TraceContext:
    """Create a manual Trace context manager."""

    return _TraceContext(name, metadata)


class SpanHandle:
    """The intentionally small public handle returned by ``span()``."""

    def __init__(
        self,
        name: Any,
        *,
        type: Any,
        operation: Any,
        details: Any,
        input: Any,
        metadata: Any,
    ) -> None:
        self._name = name
        self._type = type
        self._operation = operation
        self._details_source = details
        self._input_source = input
        self._metadata_source = metadata
        self._trace_id: str | None = None
        self._span_id: str | None = None
        self._parent_span_id: str | None = None
        self._configuration: _Configuration | None = None
        self._span_token: Token[SpanHandle | None] | None = None
        self._started: Span | None = None
        self._details: CanonicalModel | None = None
        self._input: Any = None
        self._input_capture: CaptureInfo | None = None
        self._output: Any = None
        self._output_capture: CaptureInfo | None = None
        self._output_set = False
        self._metadata: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {}
        self._active = False

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    @property
    def span_id(self) -> str | None:
        return self._span_id

    @property
    def parent_span_id(self) -> str | None:
        return self._parent_span_id

    @property
    def started(self) -> Span | None:
        return self._started

    def __enter__(self) -> "SpanHandle":
        trace_value = _current_trace.get()
        if trace_value is None:
            self._release_source_arguments()
            return self
        parent = _current_span.get()
        parent_span_id = None
        if parent is not None and parent._trace_id == trace_value.trace_id:
            parent_span_id = parent._span_id

        def build(configuration: _Configuration) -> Span:
            operation = self._operation
            if operation is None and self._type == "custom":
                operation = "custom"
            if operation is None:
                raise ValueError("operation is required for a non-custom Span")
            details = self._details_source
            if details is None and self._type == "custom":
                details = CustomDetails("custom", None)
            if not isinstance(details, CanonicalModel) or getattr(details, "kind", None) != self._type:
                raise ValueError("details.kind must equal span.type")
            input_value, input_capture = privacy.capture_content(
                self._input_source,
                capture_content=configuration.capture_content,
                source_available=True,
            )
            started = _with_capture_history(
                Span(
                    AGENTLENS_SCHEMA_VERSION,
                    trace_value.trace_id,
                    _new_span_id(),
                    parent_span_id,
                    self._type,
                    operation,
                    self._name,
                    _timestamp(),
                    None,
                    "unset",
                    None,
                    input_value,
                    None,
                    Capture(input_capture, CaptureInfo("not_captured", "not_yet_available", False)),
                    _manual_span_source(),
                    {} if self._metadata_source is None else self._metadata_source,
                    {},
                    details,
                ),
                Capture(input_capture, CaptureInfo("not_captured", "not_yet_available", False)),
            )
            return started

        created = _create_first_event(build, "span.started")
        if created is None:
            self._release_source_arguments()
            return self
        started = created.payload
        if not isinstance(started, Span):
            self._release_source_arguments()
            return self
        self._configuration = created.configuration
        self._started = started
        self._details = started.details
        self._trace_id = started.trace_id
        self._span_id = started.span_id
        self._parent_span_id = started.parent_span_id
        self._input = started.input
        self._input_capture = started.capture.input
        self._metadata = dict(started.metadata)
        self._attributes = dict(started.attributes)
        self._span_token = _current_span.set(self)
        self._active = True
        self._release_source_arguments()
        return self

    def set_output(self, value: Any) -> None:
        if not self._active or self._configuration is None:
            return
        try:
            output, capture = privacy.capture_content(
                value,
                capture_content=self._configuration.capture_content,
                source_available=True,
            )
        except Exception:
            output = None
            capture = CaptureInfo("not_captured", "serialization_error", False)
        self._output = output
        self._output_capture = capture
        self._output_set = True

    def set_attribute(self, key: Any, value: Any) -> None:
        if not self._active or not isinstance(key, str):
            return
        try:
            sanitized, _ = privacy.sanitize_json_value(value, field_name=f"attributes.{key}")
            self._attributes[key] = sanitized
        except Exception:
            return

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if not self._active or self._configuration is None or self._started is None:
            return False
        configuration = self._configuration
        started = self._started
        try:
            if self._output_set:
                output = self._output
                output_capture = self._output_capture
            else:
                output, output_capture = privacy.capture_content(
                    None,
                    capture_content=configuration.capture_content,
                    source_available=False,
                )
            if output_capture is None:
                output_capture = CaptureInfo("not_captured", "serialization_error", False)
            error = None
            status = "ok"
            if exc_type is not None:
                status = "error"
                try:
                    error_type = type.__getattribute__(exc_type, "__name__")
                    error = Error(error_type, None)
                except Exception:
                    error_type = None
                if error is not None and exc_value is not None:
                    try:
                        message = str(exc_value)
                    except Exception:
                        message = None
                    if message is not None:
                        try:
                            error = Error(error_type, message)
                        except Exception:
                            # Keep the type-only Error if message sanitization
                            # or construction fails.
                            pass
            _create_lifecycle_event(
                configuration,
                lambda _configuration: _with_capture_history(
                    Span(
                        AGENTLENS_SCHEMA_VERSION,
                        started.trace_id,
                        started.span_id,
                        started.parent_span_id,
                        started.type,
                        started.operation,
                        started.name,
                        started.started_at,
                        _timestamp(),
                        status,
                        error,
                        self._input,
                        output,
                        Capture(self._input_capture, output_capture),
                        started.source,
                        self._metadata,
                        self._attributes,
                        self._details,
                    ),
                    Capture(self._input_capture, output_capture),
                ),
                "span.ended",
            )
        except Exception:
            pass
        finally:
            self._restore_context()
            self._release_active_state()
        return False

    def _restore_context(self) -> None:
        if self._span_token is not None:
            try:
                _current_span.reset(self._span_token)
            except Exception:
                pass
            self._span_token = None

    def _release_source_arguments(self) -> None:
        self._name = None
        self._type = None
        self._operation = None
        self._input_source = None
        self._metadata_source = None
        self._details_source = None

    def _release_active_state(self) -> None:
        self._active = False
        self._configuration = None
        self._started = None
        self._details = None
        self._input = None
        self._input_capture = None
        self._output = None
        self._output_capture = None
        self._metadata = {}
        self._attributes = {}


def span(
    name: Any,
    *,
    type: Any = "custom",
    operation: Any = None,
    details: Any = None,
    input: Any = None,
    metadata: Any = None,
) -> SpanHandle:
    """Create a manual Span context manager."""

    return SpanHandle(name, type=type, operation=operation, details=details, input=input, metadata=metadata)


def flush(timeout_seconds: float = 2.0) -> bool:
    """Flush the Issue 06 event-emission boundary without doing transport."""

    with _state_lock:
        configuration = _configuration
        sink = _event_sink if _event_sink is not None else _transport_sink
    if not configuration.enabled or sink is None:
        return True
    try:
        flusher = getattr(sink, "flush", None)
        if not callable(flusher):
            return True
    except Exception:
        return False
    try:
        try:
            result = flusher(timeout_seconds)
        except TypeError:
            result = flusher()
        return True if result is None else bool(result)
    except Exception:
        return False


def _set_event_sink(sink: Any) -> None:
    """Install an internal sink for tests and the later transport issue."""

    global _event_sink, _transport_sink
    with _state_lock:
        old_transport = _transport_sink if sink is not None else None
        if sink is not None:
            _transport_sink = None
        _event_sink = sink
    if old_transport is not None:
        try:
            old_transport.shutdown()
        except BaseException:
            pass


def _reset_for_tests() -> None:
    """Reset process state for isolated SDK tests; not a public API."""

    global _configuration, _configuration_frozen, _event_sink, _transport_sink
    with _state_lock:
        old_transport = _transport_sink
        _configuration = _Configuration()
        _configuration_frozen = False
        _event_sink = None
        _transport_sink = None
    if old_transport is not None:
        try:
            old_transport.shutdown()
        except BaseException:
            pass
    _current_trace.set(None)
    _current_span.set(None)


__all__ = [
    "AgentLensConfigurationError",
    "configure",
    "flush",
    "span",
    "trace",
]
