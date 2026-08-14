"""Canonical TraceMotive repository backed by SQLite.

This module is an internal persistence boundary.  It stores only canonical
models and JSON values, never framework objects.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import os
import re
import sqlite3
from threading import RLock
from typing import Any, Callable, Iterator, TypeVar
from uuid import UUID

from tracemotive.canonical.models import (
    Capture,
    CaptureInfo,
    Error,
    Span,
    Trace,
    ValidationError,
    _canonical_json_dumps,
    _parse_canonical_json,
    details_from_dict,
    validate_span_id,
    validate_timestamp,
    validate_trace_id,
)

from .migrations import run_migrations
from .paths import DatabasePathError, prepare_database_path


class EntityConflictError(RuntimeError):
    """Raised when an immutable or repeated lifecycle snapshot conflicts."""


@dataclass(frozen=True, slots=True)
class TraceStats:
    """Derived statistics returned by the Query API storage boundary."""

    span_count: int
    error_count: int
    llm_call_count: int
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class TraceSummaryRecord:
    """A trace-list record without loading trace content fields."""

    trace_id: str
    name: str
    started_at: str
    ended_at: str | None
    status: str
    stats: TraceStats


@dataclass(frozen=True, slots=True)
class TraceQueryRecord:
    """A reconstructed Trace and its derived Query API statistics."""

    trace: Trace
    stats: TraceStats


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_T = TypeVar("_T")


def timestamp_to_us(timestamp: str | None) -> int | None:
    if timestamp is None:
        return None
    validate_timestamp(timestamp)
    parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    delta = parsed - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def us_to_timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    parsed = _EPOCH + timedelta(microseconds=value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_now_us() -> int:
    delta = datetime.now(timezone.utc) - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _json(value: Any) -> str:
    if hasattr(value, "to_json"):
        return value.to_json()
    return _canonical_json_dumps(value)


def _parsed_json(value: str) -> Any:
    return _parse_canonical_json(value)


def _trace_immutable(trace: Trace) -> tuple[Any, ...]:
    return (
        trace.schema_version,
        trace.trace_id,
        trace.name,
        trace.source.to_json(),
    )


def _span_immutable(span: Span) -> tuple[Any, ...]:
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


def _row_span_immutable(row: sqlite3.Row) -> tuple[Any, ...]:
    details = _parsed_json(row["details_json"])
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
        row["schema_version"],
        row["trace_id"],
        row["span_id"],
        row["parent_span_id"],
        row["type"],
        row["operation"],
        row["name"],
        row["source_json"],
        kind,
        *(details[field] for field in identity_fields),
    )


def _trace_values(trace: Trace, lifecycle_stage: int) -> tuple[Any, ...]:
    return (
        trace.trace_id,
        trace.schema_version,
        trace.name,
        timestamp_to_us(trace.started_at),
        timestamp_to_us(trace.ended_at),
        lifecycle_stage,
        trace.status,
        trace.source.to_json(),
        _json(trace.metadata),
        _json(trace.attributes),
    )


def _span_values(span: Span, lifecycle_stage: int) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    structural = (
        span.trace_id,
        span.span_id,
        span.schema_version,
        span.parent_span_id,
        span.type,
        span.operation,
        span.name,
        timestamp_to_us(span.started_at),
        timestamp_to_us(span.ended_at),
        lifecycle_stage,
        span.status,
        None if span.error is None else span.error.to_json(),
        span.source.to_json(),
        _json(span.metadata),
        _json(span.attributes),
        span.details.to_json(),
    )
    io = (
        span.trace_id,
        span.span_id,
        _json(span.input) if span.capture.input.state == "captured" else None,
        _json(span.output) if span.capture.output.state == "captured" else None,
        span.capture.input.to_json(),
        span.capture.output.to_json(),
    )
    return structural, io


def _row_trace_values(row: sqlite3.Row) -> tuple[Any, ...]:
    return (
        row["trace_id"],
        row["schema_version"],
        row["name"],
        row["started_at_us"],
        row["ended_at_us"],
        row["lifecycle_stage"],
        row["status"],
        row["source_json"],
        row["metadata_json"],
        row["attributes_json"],
    )


def _row_span_values(
    row: sqlite3.Row,
    io_row: sqlite3.Row,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    structural = (
        row["trace_id"],
        row["span_id"],
        row["schema_version"],
        row["parent_span_id"],
        row["type"],
        row["operation"],
        row["name"],
        row["started_at_us"],
        row["ended_at_us"],
        row["lifecycle_stage"],
        row["status"],
        row["error_json"],
        row["source_json"],
        row["metadata_json"],
        row["attributes_json"],
        row["details_json"],
    )
    stored_io = (
        io_row["trace_id"],
        io_row["span_id"],
        io_row["input_json"],
        io_row["output_json"],
        io_row["input_capture_json"],
        io_row["output_capture_json"],
    )
    return structural, stored_io


class Repository:
    """Thread-safe internal repository for canonical TraceMotive state."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = prepare_database_path(path)
        self._lock = RLock()
        try:
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise DatabasePathError("could not open database") from exc
        self._connection.row_factory = sqlite3.Row
        try:
            run_migrations(self._connection)
        except Exception:
            self._connection.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the connection for internal transaction composition/tests."""

        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            owns_transaction = not self._connection.in_transaction
            if owns_transaction:
                self._connection.execute("BEGIN")
            try:
                yield self._connection
            except Exception:
                if owns_transaction and self._connection.in_transaction:
                    self._connection.rollback()
                raise
            else:
                if owns_transaction:
                    self._connection.commit()

    def _write(self, callback: Callable[[sqlite3.Connection], _T]) -> _T:
        with self.transaction() as connection:
            return callback(connection)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def upsert_trace(
        self,
        trace: Trace,
        *,
        lifecycle_stage: int | None = None,
        now_us: int | None = None,
    ) -> bool:
        """Insert or promote one canonical Trace.

        Returns true when persisted state changed.  Repeated equivalent
        snapshots and stale starts are no-ops and do not change timestamps.
        """

        if lifecycle_stage is None:
            lifecycle_stage = 2 if trace.ended_at is not None else 1
        if lifecycle_stage == 1:
            trace.validate_started()
        elif lifecycle_stage == 2:
            trace.validate_ended()
        else:
            raise ValueError("lifecycle_stage must be 1 or 2")
        changed_at = _utc_now_us() if now_us is None else now_us

        def write(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT * FROM traces WHERE trace_id = ?", (trace.trace_id,)
            ).fetchone()
            incoming = _trace_values(trace, lifecycle_stage)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO traces (
                        trace_id, schema_version, name, started_at_us, ended_at_us,
                        lifecycle_stage, status, source_json, metadata_json,
                        attributes_json, created_at_us, updated_at_us
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*incoming, changed_at, changed_at),
                )
                return True

            if _trace_immutable(trace) != (
                row["schema_version"],
                row["trace_id"],
                row["name"],
                row["source_json"],
            ):
                raise EntityConflictError("Trace immutable fields conflict")
            if lifecycle_stage < row["lifecycle_stage"]:
                return False
            if lifecycle_stage == row["lifecycle_stage"]:
                if incoming != _row_trace_values(row):
                    raise EntityConflictError("repeated Trace snapshot conflicts")
                return False

            connection.execute(
                """
                UPDATE traces SET
                    schema_version = ?, name = ?, started_at_us = ?, ended_at_us = ?,
                    lifecycle_stage = ?, status = ?, source_json = ?, metadata_json = ?,
                    attributes_json = ?, updated_at_us = ?
                WHERE trace_id = ?
                """,
                (*incoming[1:], changed_at, trace.trace_id),
            )
            return True

        return self._write(write)

    def upsert_span(
        self,
        span: Span,
        *,
        lifecycle_stage: int | None = None,
        now_us: int | None = None,
        replace_same_stage: bool = False,
    ) -> bool:
        """Insert or promote one canonical Span and its separated I/O row.

        ``replace_same_stage`` is an internal collector hook.  The collector
        performs the Frozen semantic merge first, then uses this option to
        persist an observation-preserving same-stage enrichment (for example,
        a captured input arriving after an end-before-start event).  The
        default storage behavior remains strict repeated-snapshot checking.
        """

        if lifecycle_stage is None:
            lifecycle_stage = 2 if span.ended_at is not None else 1
        if lifecycle_stage == 1:
            span.validate_started()
        elif lifecycle_stage == 2:
            span.validate_ended()
        else:
            raise ValueError("lifecycle_stage must be 1 or 2")
        changed_at = _utc_now_us() if now_us is None else now_us

        def write(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT * FROM spans WHERE trace_id = ? AND span_id = ?",
                (span.trace_id, span.span_id),
            ).fetchone()
            structural, io = _span_values(span, lifecycle_stage)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO spans (
                        trace_id, span_id, schema_version, parent_span_id, type,
                        operation, name, started_at_us, ended_at_us, lifecycle_stage,
                        status, error_json, source_json, metadata_json, attributes_json,
                        details_json, created_at_us, updated_at_us
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*structural, changed_at, changed_at),
                )
                connection.execute(
                    """
                    INSERT INTO span_io (
                        trace_id, span_id, input_json, output_json,
                        input_capture_json, output_capture_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    io,
                )
                return True

            if _span_immutable(span) != _row_span_immutable(row):
                raise EntityConflictError("Span immutable fields conflict")
            io_row = connection.execute(
                "SELECT * FROM span_io WHERE trace_id = ? AND span_id = ?",
                (span.trace_id, span.span_id),
            ).fetchone()
            if io_row is None:
                raise RuntimeError("span_io is missing for a persisted Span")
            if lifecycle_stage < row["lifecycle_stage"]:
                return False
            if lifecycle_stage == row["lifecycle_stage"]:
                if (structural, io) == _row_span_values(row, io_row):
                    return False
                if not replace_same_stage:
                    raise EntityConflictError("repeated Span snapshot conflicts")

            connection.execute(
                """
                UPDATE spans SET
                    schema_version = ?, parent_span_id = ?, type = ?, operation = ?,
                    name = ?, started_at_us = ?, ended_at_us = ?, lifecycle_stage = ?,
                    status = ?, error_json = ?, source_json = ?, metadata_json = ?,
                    attributes_json = ?, details_json = ?, updated_at_us = ?
                WHERE trace_id = ? AND span_id = ?
                """,
                (*structural[2:], changed_at, span.trace_id, span.span_id),
            )
            connection.execute(
                """
                UPDATE span_io SET
                    input_json = ?, output_json = ?, input_capture_json = ?,
                    output_capture_json = ?
                WHERE trace_id = ? AND span_id = ?
                """,
                (*io[2:], span.trace_id, span.span_id),
            )
            return True

        return self._write(write)

    def record_ingest_event(
        self,
        *,
        event_id: str,
        event_content_sha256: str,
        event_type: str,
        trace_id: str,
        span_id: str | None = None,
        received_at_us: int | None = None,
    ) -> None:
        """Persist the collector's event idempotency record."""

        try:
            UUID(event_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("event_id must be an RFC 4122 UUID string") from exc
        if _SHA256_RE.fullmatch(event_content_sha256) is None:
            raise ValueError("event_content_sha256 must be lowercase SHA-256")
        if not isinstance(event_type, str) or event_type == "":
            raise ValueError("event_type must be a non-empty string")
        validate_trace_id(trace_id)
        if span_id is not None:
            validate_span_id(span_id)
        received = _utc_now_us() if received_at_us is None else received_at_us

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO ingest_events (
                    event_id, event_content_sha256, event_type, trace_id, span_id,
                    received_at_us
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_content_sha256,
                    event_type,
                    trace_id,
                    span_id,
                    received,
                ),
            )

        self._write(write)

    def get_trace(self, trace_id: str) -> Trace | None:
        validate_trace_id(trace_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        return None if row is None else self._trace_from_row(row)

    def list_trace_summaries(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        name: str | None = None,
    ) -> tuple[list[TraceSummaryRecord], int]:
        """Return deterministic, paged trace summaries and the total count.

        Trace-list reads intentionally select only summary columns.  Canonical
        metadata, attributes, and other content fields are not loaded for
        this Query API response.
        """

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be non-negative")
        if status is not None and status not in {"unset", "ok", "error"}:
            raise ValueError("invalid trace status")
        if name is not None and not isinstance(name, str):
            raise ValueError("name must be a string")

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT trace_id, name, started_at_us, ended_at_us, status
                FROM traces
                ORDER BY started_at_us DESC, trace_id ASC
                """
            ).fetchall()

            normalized_name = None if name is None else name.casefold()
            matching_rows = [
                row
                for row in rows
                if (status is None or row["status"] == status)
                and (
                    normalized_name is None
                    or normalized_name in row["name"].casefold()
                )
            ]
            total = len(matching_rows)
            page_rows = matching_rows[offset : offset + limit]
            trace_ids = [row["trace_id"] for row in page_rows]
            stats_by_trace = self._stats_by_trace_ids(trace_ids)

        return (
            [
                TraceSummaryRecord(
                    trace_id=row["trace_id"],
                    name=row["name"],
                    started_at=us_to_timestamp(row["started_at_us"]),  # type: ignore[arg-type]
                    ended_at=us_to_timestamp(row["ended_at_us"]),
                    status=row["status"],
                    stats=stats_by_trace[row["trace_id"]],
                )
                for row in page_rows
            ],
            total,
        )

    def get_trace_query(self, trace_id: str) -> TraceQueryRecord | None:
        """Reconstruct one Trace and derive its Query API statistics."""

        validate_trace_id(trace_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if row is None:
                return None
            span_rows = self._connection.execute(
                "SELECT trace_id, status, type, details_json FROM spans WHERE trace_id = ?",
                (trace_id,),
            ).fetchall()
            trace = self._trace_from_row(row)
            stats = self._stats_for_rows(span_rows)
        return TraceQueryRecord(trace=trace, stats=stats)

    def get_spans_for_trace(self, trace_id: str) -> list[Span] | None:
        """Return all spans for a trace in the Frozen deterministic order.

        A span-before-trace ingest is queryable while the span exists; an ID
        with neither a Trace nor any Span is treated as an unknown trace.
        """

        validate_trace_id(trace_id)
        with self._lock:
            trace_exists = self._connection.execute(
                "SELECT 1 FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone() is not None
            span_rows = self._connection.execute(
                """
                SELECT * FROM spans
                WHERE trace_id = ?
                ORDER BY started_at_us ASC, span_id ASC
                """,
                (trace_id,),
            ).fetchall()
            if not trace_exists and not span_rows:
                return None
            io_rows = self._connection.execute(
                "SELECT * FROM span_io WHERE trace_id = ?", (trace_id,)
            ).fetchall()
            io_by_span = {row["span_id"]: row for row in io_rows}
            spans: list[Span] = []
            for row in span_rows:
                io_row = io_by_span.get(row["span_id"])
                if io_row is None:
                    raise RuntimeError("span_io is missing for a persisted Span")
                spans.append(self._span_from_rows(row, io_row))
        return spans

    def health_check(self) -> bool:
        """Verify that the configured SQLite connection is readable."""

        with self._lock:
            row = self._connection.execute("SELECT 1").fetchone()
        return row is not None and row[0] == 1

    def get_span(self, trace_id: str, span_id: str) -> Span | None:
        validate_trace_id(trace_id)
        validate_span_id(span_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM spans WHERE trace_id = ? AND span_id = ?",
                (trace_id, span_id),
            ).fetchone()
            if row is None:
                return None
            io_row = self._connection.execute(
                "SELECT * FROM span_io WHERE trace_id = ? AND span_id = ?",
                (trace_id, span_id),
            ).fetchone()
        if io_row is None:
            raise RuntimeError("span_io is missing for a persisted Span")
        return self._span_from_rows(row, io_row)

    def get_ingest_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM ingest_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def _stats_by_trace_ids(
        self, trace_ids: list[str]
    ) -> dict[str, TraceStats]:
        stats_by_trace = {
            trace_id: TraceStats(0, 0, 0, None, None) for trace_id in trace_ids
        }
        if not trace_ids:
            return stats_by_trace
        placeholders = ", ".join("?" for _ in trace_ids)
        rows = self._connection.execute(
            f"""
            SELECT trace_id, status, type, details_json
            FROM spans
            WHERE trace_id IN ({placeholders})
            """,
            trace_ids,
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {trace_id: [] for trace_id in trace_ids}
        for row in rows:
            grouped[row["trace_id"]].append(row)
        for trace_id, trace_rows in grouped.items():
            stats_by_trace[trace_id] = self._stats_for_rows(trace_rows)
        return stats_by_trace

    @staticmethod
    def _stats_for_rows(rows: list[sqlite3.Row]) -> TraceStats:
        span_count = 0
        error_count = 0
        llm_call_count = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
        for row in rows:
            span_count += 1
            if row["status"] == "error":
                error_count += 1
            if row["type"] != "llm":
                continue
            llm_call_count += 1
            details = details_from_dict(_parsed_json(row["details_json"]))
            if details.kind != "llm":
                raise ValidationError("stored LLM Span has non-LLM details")
            if details.usage.input_tokens is not None:
                input_tokens = (input_tokens or 0) + details.usage.input_tokens
            if details.usage.output_tokens is not None:
                output_tokens = (output_tokens or 0) + details.usage.output_tokens
        return TraceStats(
            span_count=span_count,
            error_count=error_count,
            llm_call_count=llm_call_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def has_ingest_event_type(
        self,
        *,
        trace_id: str,
        span_id: str,
        event_type: str,
    ) -> bool:
        """Return whether one lifecycle event type was recorded for a Span."""

        validate_trace_id(trace_id)
        validate_span_id(span_id)
        if not isinstance(event_type, str) or event_type == "":
            raise ValueError("event_type must be a non-empty string")
        with self._lock:
            return (
                self._connection.execute(
                    """
                    SELECT 1 FROM ingest_events
                    WHERE trace_id = ? AND span_id = ? AND event_type = ?
                    LIMIT 1
                    """,
                    (trace_id, span_id, event_type),
                ).fetchone()
                is not None
            )

    def delete_trace(self, trace_id: str) -> bool:
        """Delete a Trace, its Spans, content, and event records atomically."""

        validate_trace_id(trace_id)

        def write(connection: sqlite3.Connection) -> bool:
            existed = connection.execute(
                "SELECT 1 FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone() is not None
            connection.execute("DELETE FROM span_io WHERE trace_id = ?", (trace_id,))
            connection.execute("DELETE FROM spans WHERE trace_id = ?", (trace_id,))
            connection.execute("DELETE FROM ingest_events WHERE trace_id = ?", (trace_id,))
            connection.execute("DELETE FROM traces WHERE trace_id = ?", (trace_id,))
            return existed

        return self._write(write)

    @staticmethod
    def _trace_from_row(row: sqlite3.Row) -> Trace:
        return Trace(
            schema_version=row["schema_version"],
            trace_id=row["trace_id"],
            name=row["name"],
            started_at=us_to_timestamp(row["started_at_us"]),  # type: ignore[arg-type]
            ended_at=us_to_timestamp(row["ended_at_us"]),
            status=row["status"],
            source=_model_from_json(row["source_json"], "source"),
            metadata=_parsed_json(row["metadata_json"]),
            attributes=_parsed_json(row["attributes_json"]),
        )

    @staticmethod
    def _span_from_rows(row: sqlite3.Row, io_row: sqlite3.Row) -> Span:
        stored_input = (
            None if io_row["input_json"] is None else _parsed_json(io_row["input_json"])
        )
        stored_output = (
            None if io_row["output_json"] is None else _parsed_json(io_row["output_json"])
        )
        stored_capture = Capture(
            CaptureInfo.from_dict(_parsed_json(io_row["input_capture_json"])),
            CaptureInfo.from_dict(_parsed_json(io_row["output_capture_json"])),
        )
        span = Span(
            schema_version=row["schema_version"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            type=row["type"],
            operation=row["operation"],
            name=row["name"],
            started_at=us_to_timestamp(row["started_at_us"]),  # type: ignore[arg-type]
            ended_at=us_to_timestamp(row["ended_at_us"]),
            status=row["status"],
            error=(
                None
                if row["error_json"] is None
                else Error.from_dict(_parsed_json(row["error_json"]))
            ),
            input=stored_input,
            output=stored_output,
            capture=stored_capture,
            source=_model_from_json(row["source_json"], "source"),
            metadata=_parsed_json(row["metadata_json"]),
            attributes=_parsed_json(row["attributes_json"]),
            details=details_from_dict(_parsed_json(row["details_json"])),
        )
        for field_name, stored_value in (
            ("input", stored_input),
            ("output", stored_output),
        ):
            if _canonical_json_dumps(getattr(span, field_name)) != _canonical_json_dumps(
                stored_value
            ):
                raise ValidationError(
                    f"stored Span {field_name} is not privacy-normalized"
                )
        for field_name in ("input", "output"):
            validated_info = getattr(span.capture, field_name)
            stored_info = getattr(stored_capture, field_name)
            if (validated_info.state, validated_info.reason) != (
                stored_info.state,
                stored_info.reason,
            ):
                raise ValidationError(
                    f"stored Span {field_name} CaptureInfo is inconsistent"
                )

        # Span construction above performs the complete canonical validation
        # and privacy pass.  CaptureInfo.redacted is persisted historical
        # metadata, so restore that validated object without recomputing it.
        object.__setattr__(span, "capture", stored_capture)
        return span


def _model_from_json(value: str, field_name: str) -> Any:
    from tracemotive.canonical.models import SpanSource, TraceSource

    parsed = _parsed_json(value)
    if field_name == "source":
        # The storage column determines which source shape is valid.  Callers
        # pass the exact constructor below rather than relying on heuristics.
        if "native_span_id" in parsed:
            return SpanSource.from_dict(parsed)
        return TraceSource.from_dict(parsed)
    raise ValueError(f"unsupported stored model field: {field_name}")


SQLiteRepository = Repository


__all__ = [
    "EntityConflictError",
    "Repository",
    "SQLiteRepository",
    "TraceQueryRecord",
    "TraceStats",
    "TraceSummaryRecord",
    "timestamp_to_us",
    "us_to_timestamp",
]
