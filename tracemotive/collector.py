"""Framework-independent TraceMotive v0.1 collector ingest service.

This module is the boundary between a parsed ingest protocol request and the
Issue 03 SQLite repository.  It deliberately does not contain an HTTP server
or framework-specific code.  HTTP adapters can translate ``IngestError`` into
the status/body pair described by the Frozen Specification.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Callable, Mapping
from uuid import UUID
import re
import time

from tracemotive.canonical.models import (
    Capture,
    Span,
    Trace,
    ValidationError,
    _canonical_json_dumps,
    _parse_canonical_json,
    validate_timestamp,
)
from tracemotive.storage.repository import EntityConflictError, Repository, timestamp_to_us


PROTOCOL_VERSION = 1
EVENT_TYPES = frozenset(
    {"trace.started", "trace.ended", "span.started", "span.ended"}
)
MAX_EVENT_BYTES = 1_048_576
MAX_REQUEST_BYTES = 4_194_304
DEFAULT_BIND_HOST = "127.0.0.1"

_BATCH_FIELDS = frozenset({"protocol_version", "events"})
_EVENT_FIELDS = frozenset({"event_id", "event_type", "emitted_at", "payload"})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class IngestError(ValueError):
    """A protocol error that an adapter can render as an HTTP response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        event_index: int | None = None,
        field: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.event_index = event_index
        self.field = field
        super().__init__(message)

    def body(self) -> dict[str, Any]:
        if self.status_code == 413:
            return {}
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.event_index is not None:
            error["event_index"] = self.event_index
        if self.field is not None:
            error["field"] = self.field
        return {"error": error}

    def response(self) -> tuple[int, dict[str, Any]]:
        return self.status_code, self.body()


class PayloadTooLargeError(IngestError):
    def __init__(self, message: str = "payload too large") -> None:
        super().__init__(413, "payload_too_large", message)


@dataclass(frozen=True, slots=True)
class _ValidatedEvent:
    event_id: str
    event_type: str
    emitted_at: str
    emitted_at_us: int
    payload: Trace | Span
    normalized_event: dict[str, Any]
    event_content_sha256: str
    serialized_size: int


def _validation(
    message: str,
    *,
    event_index: int | None = None,
    field: str | None = None,
) -> IngestError:
    return IngestError(
        422,
        "validation_error",
        message,
        event_index=event_index,
        field=field,
    )


def _require_exact_object(
    value: Any,
    expected: frozenset[str],
    *,
    model_name: str,
    event_index: int | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise _validation(
            f"{model_name} must be an object",
            event_index=event_index,
            field=field,
        )
    actual = frozenset(value)
    missing = expected - actual
    extra = actual - expected
    if missing:
        missing_name = sorted(missing)[0]
        raise _validation(
            "missing required field",
            event_index=event_index,
            field=(field + "." if field else "") + missing_name,
        )
    if extra:
        raise _validation(
            f"invalid {('request' if model_name == 'ingest request' else 'event')} object",
            event_index=event_index,
            field=field or ("request" if model_name == "ingest request" else "event"),
        )
    return value


def _canonical_uuid(value: Any, *, event_index: int) -> str:
    if type(value) is not str or _UUID_RE.fullmatch(value) is None:
        raise _validation(
            "event_id must be an RFC 4122 UUID string",
            event_index=event_index,
            field="event_id",
        )
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _validation(
            "event_id must be an RFC 4122 UUID string",
            event_index=event_index,
            field="event_id",
        ) from exc
    if parsed.variant != "specified in RFC 4122":
        raise _validation(
            "event_id must be an RFC 4122 UUID string",
            event_index=event_index,
            field="event_id",
        )
    return value


def _validation_field(message: str) -> str:
    """Map canonical-model validation to the protocol's payload field."""

    candidates = (
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
        "capture",
        "source",
        "metadata",
        "attributes",
        "details",
        "input",
        "output",
    )
    for candidate in candidates:
        if candidate in message:
            return f"payload.{candidate}"
    return "payload"


def _safe_validation_message(field: str) -> str:
    """Return a fixed message for an allowlisted schema field only."""

    if field.startswith("payload."):
        leaf = field.removeprefix("payload.").split(".", 1)[0]
        if leaf in {
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
            "capture",
            "source",
            "metadata",
            "attributes",
            "details",
            "input",
            "output",
        }:
            return f"invalid {leaf}"
    if field in {"event_id", "event_type", "emitted_at", "protocol_version", "events"}:
        return f"invalid {field}"
    return "invalid canonical payload"


def _immutable_trace_values(trace: Trace) -> tuple[Any, ...]:
    return (
        trace.schema_version,
        trace.trace_id,
        trace.name,
        trace.source.to_json(),
    )


def _immutable_span_values(span: Span) -> tuple[Any, ...]:
    details = span.details.to_dict()
    kind = details["kind"]
    identity_fields = {
        "agent": ("agent_name", "agent_version"),
        "tool": ("tool_name", "tool_call_id"),
        "handoff": ("from_agent", "to_agent"),
        "custom": ("source_type",),
        "llm": (),
        "retrieval": (),
    }[kind]
    return (
        span.schema_version,
        span.trace_id,
        span.span_id,
        span.parent_span_id,
        span.type,
        span.operation,
        span.name,
        span.source.to_json(),
        kind,
        *(details[field] for field in identity_fields),
    )


def _first_trace_immutable_difference(previous: Trace, current: Trace) -> str:
    fields = (
        ("schema_version", previous.schema_version, current.schema_version),
        ("trace_id", previous.trace_id, current.trace_id),
        ("name", previous.name, current.name),
        ("source", previous.source.to_json(), current.source.to_json()),
    )
    for field, old, new in fields:
        if old != new:
            return f"payload.{field}"
    return "payload"


def _first_span_immutable_difference(previous: Span, current: Span) -> str:
    fields = (
        ("schema_version", previous.schema_version, current.schema_version),
        ("trace_id", previous.trace_id, current.trace_id),
        ("span_id", previous.span_id, current.span_id),
        ("parent_span_id", previous.parent_span_id, current.parent_span_id),
        ("type", previous.type, current.type),
        ("operation", previous.operation, current.operation),
        ("name", previous.name, current.name),
        ("source", previous.source.to_json(), current.source.to_json()),
        ("details.kind", previous.details.to_dict()["kind"], current.details.to_dict()["kind"]),
    )
    for field, old, new in fields:
        if old != new:
            return f"payload.{field}"
    previous_details = previous.details.to_dict()
    current_details = current.details.to_dict()
    for field in (
        "agent_name",
        "agent_version",
        "tool_name",
        "tool_call_id",
        "from_agent",
        "to_agent",
        "source_type",
    ):
        if previous_details.get(field) != current_details.get(field):
            return f"payload.details.{field}"
    return "payload"


def _io_pair(span: Span, field_name: str) -> tuple[Any, Any]:
    return getattr(span, field_name), getattr(span.capture, field_name)


def _replace_span_io(
    span: Span,
    *,
    input_value: Any,
    output_value: Any,
    input_capture: Any,
    output_capture: Any,
) -> Span:
    """Replace I/O while retaining validated CaptureInfo history metadata."""

    merged = replace(
        span,
        input=input_value,
        output=output_value,
        capture=Capture(input_capture, output_capture),
    )
    # Span construction re-sanitizes already-normalized values and can clear
    # the historical redacted bit.  The pair was validated at ingest, so
    # restore that wire-authoritative metadata after construction.
    object.__setattr__(merged, "capture", Capture(input_capture, output_capture))
    return merged


def _merge_span_io(existing: Span, incoming: Span) -> Span:
    """Apply only the §27.1 observation-preserving I/O rules."""

    existing_input, existing_input_capture = _io_pair(existing, "input")
    incoming_input, incoming_input_capture = _io_pair(incoming, "input")
    if existing_input_capture.state == "captured":
        merged_input, merged_input_capture = existing_input, existing_input_capture
    elif incoming_input_capture.state == "captured":
        merged_input, merged_input_capture = incoming_input, incoming_input_capture
    else:
        merged_input, merged_input_capture = incoming_input, incoming_input_capture

    existing_output, existing_output_capture = _io_pair(existing, "output")
    incoming_output, incoming_output_capture = _io_pair(incoming, "output")
    if (
        existing_output_capture.state == "captured"
        and incoming_output_capture.state != "captured"
    ):
        merged_output, merged_output_capture = existing_output, existing_output_capture
    elif incoming_output_capture.state == "captured":
        merged_output, merged_output_capture = incoming_output, incoming_output_capture
    else:
        merged_output, merged_output_capture = incoming_output, incoming_output_capture

    return _replace_span_io(
        incoming,
        input_value=merged_input,
        output_value=merged_output,
        input_capture=merged_input_capture,
        output_capture=merged_output_capture,
    )


def _merge_repeated_final_io(existing: Span, incoming: Span) -> Span:
    """Merge only non-destructive evidence for a repeated final snapshot.

    A completed Span with no accepted ``span.started`` event has no
    start-vs-end authority exception.  Two captured final inputs are therefore
    compared as final snapshots; a difference remains an entity conflict.
    Captured evidence is still not replaced by a later not-captured pair.
    """

    existing_input, existing_input_capture = _io_pair(existing, "input")
    incoming_input, incoming_input_capture = _io_pair(incoming, "input")
    if (
        existing_input_capture.state == "captured"
        and incoming_input_capture.state != "captured"
    ):
        merged_input, merged_input_capture = existing_input, existing_input_capture
    else:
        merged_input, merged_input_capture = incoming_input, incoming_input_capture

    existing_output, existing_output_capture = _io_pair(existing, "output")
    incoming_output, incoming_output_capture = _io_pair(incoming, "output")
    if (
        existing_output_capture.state == "captured"
        and incoming_output_capture.state != "captured"
    ):
        merged_output, merged_output_capture = existing_output, existing_output_capture
    else:
        merged_output, merged_output_capture = incoming_output, incoming_output_capture

    return _replace_span_io(
        incoming,
        input_value=merged_input,
        output_value=merged_output,
        input_capture=merged_input_capture,
        output_capture=merged_output_capture,
    )


def _span_stage(span: Span) -> int:
    return 2 if span.ended_at is not None else 1


def _trace_stage(trace: Trace) -> int:
    return 2 if trace.ended_at is not None else 1


class Collector:
    """Apply validated ingest batches atomically to an Issue 03 repository."""

    def __init__(
        self,
        repository: Repository | None = None,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.repository = repository if repository is not None else Repository()
        self._clock = clock

    def close(self) -> None:
        self.repository.close()

    def __enter__(self) -> "Collector":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def ingest_json(self, body: str | bytes) -> dict[str, int]:
        """Parse a JSON body, then apply it as one ingest batch."""

        if type(body) is bytes:
            raw_size = len(body)
            if raw_size > MAX_REQUEST_BYTES:
                raise PayloadTooLargeError("ingest request exceeds 4 MiB")
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IngestError(400, "malformed_json", "malformed JSON") from exc
        elif type(body) is str:
            try:
                raw_size = len(body.encode("utf-8"))
            except UnicodeError as exc:
                raise IngestError(400, "malformed_json", "malformed JSON") from exc
            if raw_size > MAX_REQUEST_BYTES:
                raise PayloadTooLargeError("ingest request exceeds 4 MiB")
        else:
            raise IngestError(400, "malformed_json", "malformed JSON")

        try:
            request = _parse_canonical_json(body)
        except (ValidationError, TypeError) as exc:
            raise IngestError(400, "malformed_json", "malformed JSON") from exc
        return self._ingest_parsed(request, raw_size=raw_size)

    def ingest(self, request: Mapping[str, Any] | str | bytes) -> dict[str, int]:
        """Validate and apply one parsed batch, returning the Frozen counts."""

        if type(request) in (str, bytes):
            return self.ingest_json(request)
        return self._ingest_parsed(request, raw_size=None)

    def _ingest_parsed(self, request: Any, *, raw_size: int | None) -> dict[str, int]:
        batch = _require_exact_object(request, _BATCH_FIELDS, model_name="ingest request")
        if type(batch["protocol_version"]) is not int or batch["protocol_version"] != PROTOCOL_VERSION:
            raise _validation("unsupported protocol_version", field="protocol_version")
        if type(batch["events"]) is not list:
            raise _validation("events must be an array", field="events")

        events = [self._validate_event(event, index) for index, event in enumerate(batch["events"])]
        normalized_request = {
            "protocol_version": PROTOCOL_VERSION,
            "events": [event.normalized_event for event in events],
        }
        normalized_size = len(_canonical_json_dumps(normalized_request).encode("utf-8"))
        if raw_size is not None and raw_size > MAX_REQUEST_BYTES:
            raise PayloadTooLargeError("ingest request exceeds 4 MiB")
        if normalized_size > MAX_REQUEST_BYTES:
            raise PayloadTooLargeError("ingest request exceeds 4 MiB")
        oversized = next(
            (event for event in events if event.serialized_size > MAX_EVENT_BYTES),
            None,
        )
        if oversized is not None:
            raise PayloadTooLargeError("individual event exceeds 1 MiB")

        received_at_us = self._now_us()
        accepted = duplicates = stale = 0
        with self.repository.transaction():
            self._check_event_id_conflicts(events)
            seen: dict[str, str] = {}
            for index, event in enumerate(events):
                known = self.repository.get_ingest_event(event.event_id)
                if known is not None or event.event_id in seen:
                    duplicates += 1
                    seen[event.event_id] = event.event_content_sha256
                    continue

                # Make accepted lifecycle history visible to later events in
                # this same transaction.  In particular, a span.started
                # followed by span.ended in one batch must use the §27.1
                # started-input authority.  Any later validation/conflict
                # failure still rolls the history and entity writes back
                # together with the transaction.
                self.repository.record_ingest_event(
                    event_id=event.event_id,
                    event_content_sha256=event.event_content_sha256,
                    event_type=event.event_type,
                    trace_id=event.payload.trace_id,
                    span_id=(event.payload.span_id if isinstance(event.payload, Span) else None),
                    received_at_us=received_at_us,
                )
                outcome = self._apply_event(event, index, received_at_us)
                if outcome == "stale":
                    stale += 1
                else:
                    accepted += 1
                seen[event.event_id] = event.event_content_sha256
        return {"accepted": accepted, "duplicates": duplicates, "stale": stale}

    def _check_event_id_conflicts(self, events: list[_ValidatedEvent]) -> None:
        batch_hashes: dict[str, str] = {}
        for index, event in enumerate(events):
            previous_batch_hash = batch_hashes.get(event.event_id)
            if previous_batch_hash is not None:
                if previous_batch_hash != event.event_content_sha256:
                    raise IngestError(
                        409,
                        "event_id_conflict",
                        "event_id was reused with different content",
                        event_index=index,
                        field="event_id",
                    )
                continue
            known = self.repository.get_ingest_event(event.event_id)
            if known is not None and known["event_content_sha256"] != event.event_content_sha256:
                raise IngestError(
                    409,
                    "event_id_conflict",
                    "event_id was reused with different content",
                    event_index=index,
                    field="event_id",
                )
            batch_hashes[event.event_id] = event.event_content_sha256

    def _validate_event(self, value: Any, index: int) -> _ValidatedEvent:
        event = _require_exact_object(
            value,
            _EVENT_FIELDS,
            model_name="event",
            event_index=index,
        )
        event_id = _canonical_uuid(event["event_id"], event_index=index)
        event_type = event["event_type"]
        if type(event_type) is not str or event_type not in EVENT_TYPES:
            raise _validation(
                "event_type must be one of the supported lifecycle events",
                event_index=index,
                field="event_type",
            )
        try:
            emitted_at = validate_timestamp(event["emitted_at"], field_name="emitted_at")
            emitted_at_us = timestamp_to_us(emitted_at)
        except (ValidationError, TypeError) as exc:
            raise _validation(
                "invalid emitted_at", event_index=index, field="emitted_at"
            ) from exc

        try:
            if event_type.startswith("trace."):
                payload = Trace.from_dict(event["payload"])
                if event_type == "trace.started":
                    payload.validate_started()
                else:
                    payload.validate_ended()
            else:
                payload = Span.from_dict(event["payload"])
                # ``Span.from_dict`` re-sanitizes already normalized wire
                # content and can therefore clear the historical
                # CaptureInfo.redacted bit.  The CaptureInfo pair is part of
                # the validated protocol payload; retain that wire metadata
                # just as the storage rehydration boundary does.
                object.__setattr__(
                    payload,
                    "capture",
                    Capture.from_dict(event["payload"]["capture"]),
                )
                if event_type == "span.started":
                    payload.validate_started()
                else:
                    payload.validate_ended()
        except (ValidationError, TypeError, KeyError) as exc:
            safe_field = _validation_field(str(exc))
            raise _validation(
                _safe_validation_message(safe_field),
                event_index=index,
                field=safe_field,
            ) from exc

        normalized_event = {
            "event_id": event_id,
            "event_type": event_type,
            "emitted_at": emitted_at,
            "payload": payload.to_dict(),
        }
        serialized_size = len(_canonical_json_dumps(normalized_event).encode("utf-8"))
        hash_material = {
            "event_type": event_type,
            "emitted_at_us": emitted_at_us,
            "payload": payload.to_dict(),
        }
        event_hash = sha256(
            _canonical_json_dumps(hash_material).encode("utf-8")
        ).hexdigest()
        return _ValidatedEvent(
            event_id,
            event_type,
            emitted_at,
            emitted_at_us,
            payload,
            normalized_event,
            event_hash,
            serialized_size,
        )

    def _apply_event(self, event: _ValidatedEvent, index: int, now_us: int) -> str:
        try:
            if isinstance(event.payload, Trace):
                return self._apply_trace(event.payload, event.event_type, index, now_us)
            return self._apply_span(event.payload, event.event_type, index, now_us)
        except EntityConflictError as exc:
            raise IngestError(
                409,
                "entity_conflict",
                "entity conflict",
                event_index=index,
                field="payload",
            ) from exc

    def _apply_trace(self, incoming: Trace, event_type: str, index: int, now_us: int) -> str:
        existing = self.repository.get_trace(incoming.trace_id)
        stage = 1 if event_type == "trace.started" else 2
        if existing is None:
            self.repository.upsert_trace(incoming, lifecycle_stage=stage, now_us=now_us)
            return "accepted"

        if _immutable_trace_values(existing) != _immutable_trace_values(incoming):
            field = _first_trace_immutable_difference(existing, incoming)
            raise IngestError(
                409,
                "entity_conflict",
                "immutable Trace fields differ",
                event_index=index,
                field=field,
            )
        existing_stage = _trace_stage(existing)
        if stage < existing_stage:
            return "stale"
        if stage == existing_stage:
            if existing.to_json() != incoming.to_json():
                raise IngestError(
                    409,
                    "entity_conflict",
                    "repeated Trace snapshot conflicts",
                    event_index=index,
                    field="payload",
                )
            return "accepted"
        self.repository.upsert_trace(incoming, lifecycle_stage=stage, now_us=now_us)
        return "accepted"

    def _apply_span(self, incoming: Span, event_type: str, index: int, now_us: int) -> str:
        existing = self.repository.get_span(incoming.trace_id, incoming.span_id)
        stage = 1 if event_type == "span.started" else 2
        if existing is None:
            self.repository.upsert_span(incoming, lifecycle_stage=stage, now_us=now_us)
            return "accepted"

        if _immutable_span_values(existing) != _immutable_span_values(incoming):
            field = _first_span_immutable_difference(existing, incoming)
            raise IngestError(
                409,
                "entity_conflict",
                "immutable Span fields differ",
                event_index=index,
                field=field,
            )
        existing_stage = _span_stage(existing)

        if stage < existing_stage:
            if incoming.capture.input.state == "captured":
                enriched = _replace_span_io(
                    existing,
                    input_value=incoming.input,
                    output_value=existing.output,
                    input_capture=incoming.capture.input,
                    output_capture=existing.capture.output,
                )
                self.repository.upsert_span(
                    enriched,
                    lifecycle_stage=2,
                    now_us=now_us,
                    replace_same_stage=True,
                )
            return "stale"

        if stage == existing_stage:
            if stage == 1:
                candidate = incoming
            else:
                has_started = self.repository.has_ingest_event_type(
                    trace_id=incoming.trace_id,
                    span_id=incoming.span_id,
                    event_type="span.started",
                )
                candidate = (
                    _merge_span_io(existing, incoming)
                    if has_started
                    else _merge_repeated_final_io(existing, incoming)
                )
            if candidate.to_json() != existing.to_json():
                raise IngestError(
                    409,
                    "entity_conflict",
                    "repeated Span snapshot conflicts",
                    event_index=index,
                    field="payload",
                )
            return "accepted"

        candidate = _merge_span_io(existing, incoming)
        self.repository.upsert_span(candidate, lifecycle_stage=2, now_us=now_us)
        return "accepted"

    def _now_us(self) -> int:
        if self._clock is not None:
            value = self._clock()
            if type(value) is not int:
                raise RuntimeError("collector clock must return integer microseconds")
            return value
        return time.time_ns() // 1_000


def create_app(
    repository: Repository | None = None,
    *,
    clock: Callable[[], int] | None = None,
    bind_host: str = DEFAULT_BIND_HOST,
) -> Any:
    """Create the local FastAPI application for ingest and Query API routes.

    Binding is deliberately constrained here because v0.1 has no remote
    collector security model.  The returned app contains only the ingest
    route; lifecycle and persistence behavior remain in :class:`Collector`.
    """

    if bind_host != DEFAULT_BIND_HOST:
        raise ValueError("TraceMotive v0.1 collector must bind to 127.0.0.1")
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - exercised by packaging
        raise RuntimeError(
            "FastAPI is required for the TraceMotive Issue 04 HTTP boundary"
        ) from exc

    collector = Collector(repository, clock=clock)
    app = FastAPI(
        title="TraceMotive Collector",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.tracemotive_collector = collector

    @app.post("/api/v1/ingest")
    async def ingest(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            result = collector.ingest_json(body)
        except IngestError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.body())
        return JSONResponse(status_code=200, content=result)

    from tracemotive.query import register_query_routes

    register_query_routes(app, collector.repository)
    return app


IngestService = Collector


__all__ = [
    "Collector",
    "DEFAULT_BIND_HOST",
    "EVENT_TYPES",
    "IngestError",
    "IngestService",
    "MAX_EVENT_BYTES",
    "MAX_REQUEST_BYTES",
    "PayloadTooLargeError",
    "PROTOCOL_VERSION",
    "create_app",
]
