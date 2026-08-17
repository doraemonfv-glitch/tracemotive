"""Frozen v0.1 Query API HTTP handlers.

The handlers depend only on the repository query boundary.  They deliberately
serialize responses with the canonical JSON encoder so exact numeric values
and reconstructed Canonical content retain their wire representation.
"""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Callable

from fastapi import Request
from starlette.responses import Response

from tracemotive.canonical.models import (
    _ExactNumber,
    _canonical_json_dumps,
    validate_span_id,
    validate_trace_id,
)
from tracemotive.comparison import ComparisonTooLargeError, compare_trace_inputs
from tracemotive.api_v3 import build_v3_comparison
from tracemotive.api_v4 import build_v4_comparison
from tracemotive.storage import Repository, TraceStats, TraceSummaryRecord
from tracemotive.storage.repository import timestamp_to_us


_QUERY_PARAMETERS = frozenset({"limit", "offset", "status", "name"})
_INTEGER_PARAMETER = re.compile(r"^[+-]?\d+$")
_STATUSES = frozenset({"unset", "ok", "error", "omitted"})


class QueryRequestError(ValueError):
    """Raised for a malformed Query API identifier or parameter."""


def _json_response(Response: type[Any], content: Any, *, status_code: int = 200) -> Any:
    return Response(
        content=_canonical_json_dumps(content),
        status_code=status_code,
        media_type="application/json",
    )


def _error(Response: type[Any], status_code: int, code: str, message: str) -> Any:
    return _json_response(
        Response,
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


def _parse_int_parameter(raw: str | None, *, field: str, default: int) -> int:
    if raw is None:
        return default
    if _INTEGER_PARAMETER.fullmatch(raw) is None:
        raise QueryRequestError("invalid query parameter")
    try:
        value = int(raw, 10)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QueryRequestError("invalid query parameter") from exc
    if field == "limit" and not 1 <= value <= 100:
        raise QueryRequestError("invalid query parameter")
    if field == "offset" and value < 0:
        raise QueryRequestError("invalid query parameter")
    return value


def _parse_trace_filters(request: Any) -> tuple[int, int, str | None, str | None]:
    query_params = request.query_params
    if any(key not in _QUERY_PARAMETERS for key in query_params.keys()):
        raise QueryRequestError("invalid query parameter")
    limit = _parse_int_parameter(query_params.get("limit"), field="limit", default=50)
    offset = _parse_int_parameter(query_params.get("offset"), field="offset", default=0)
    status = query_params.get("status")
    if status is not None and status not in _STATUSES:
        raise QueryRequestError("invalid query parameter")
    name = query_params.get("name")
    return limit, offset, (None if status in (None, "omitted") else status), name


def _latency_ms(started_at: str, ended_at: str | None) -> _ExactNumber | None:
    if ended_at is None:
        return None
    started_us = timestamp_to_us(started_at)
    ended_us = timestamp_to_us(ended_at)
    if started_us is None or ended_us is None or ended_us < started_us:
        raise RuntimeError("stored timestamp ordering is invalid")
    return _ExactNumber(Decimal(ended_us - started_us) / Decimal(1000))


def _stats_dict(stats: TraceStats, latency_ms: _ExactNumber | None) -> dict[str, Any]:
    return {
        "latency_ms": latency_ms,
        "span_count": stats.span_count,
        "error_count": stats.error_count,
        "llm_call_count": stats.llm_call_count,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
    }


def _summary_dict(record: TraceSummaryRecord) -> dict[str, Any]:
    return {
        "trace_id": record.trace_id,
        "name": record.name,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "status": record.status,
        "latency_ms": _latency_ms(record.started_at, record.ended_at),
        "span_count": record.stats.span_count,
        "error_count": record.stats.error_count,
        "llm_call_count": record.stats.llm_call_count,
        "input_tokens": record.stats.input_tokens,
        "output_tokens": record.stats.output_tokens,
    }


def _validated_id(value: str, validator: Callable[[Any], str]) -> str:
    try:
        return validator(value)
    except Exception as exc:
        raise QueryRequestError("invalid identifier") from exc


def register_query_routes(app: Any, repository: Repository) -> None:
    """Register the Frozen v0.1 routes and additive v0.2 comparison route."""

    @app.get("/api/v1/traces")
    async def list_traces(request: Request) -> Response:
        try:
            limit, offset, status, name = _parse_trace_filters(request)
            items, total = repository.list_trace_summaries(
                limit=limit,
                offset=offset,
                status=status,
                name=name,
            )
            return _json_response(
                Response,
                {
                    "items": [_summary_dict(item) for item in items],
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                },
            )
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.get("/api/v1/traces/{trace_id}/spans/{span_id}")
    async def get_span(request: Request, trace_id: str, span_id: str) -> Response:
        del request
        try:
            trace_id = _validated_id(trace_id, validate_trace_id)
            span_id = _validated_id(span_id, validate_span_id)
            span = repository.get_span(trace_id, span_id)
            if span is None:
                return _error(Response, 404, "not_found", "not found")
            return _json_response(
                Response,
                {
                    "span": span.to_dict(),
                    "latency_ms": _latency_ms(span.started_at, span.ended_at),
                },
            )
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.get("/api/v1/traces/{trace_id}/spans")
    async def list_spans(request: Request, trace_id: str) -> Response:
        del request
        try:
            trace_id = _validated_id(trace_id, validate_trace_id)
            spans = repository.get_spans_for_trace(trace_id)
            if spans is None:
                return _error(Response, 404, "not_found", "not found")
            return _json_response(
                Response,
                {
                    "items": [
                        {
                            "span": span.to_dict(),
                            "latency_ms": _latency_ms(span.started_at, span.ended_at),
                        }
                        for span in spans
                    ]
                },
            )
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.get("/api/v1/traces/{trace_id}")
    async def get_trace(request: Request, trace_id: str) -> Response:
        del request
        try:
            trace_id = _validated_id(trace_id, validate_trace_id)
            record = repository.get_trace_query(trace_id)
            if record is None:
                return _error(Response, 404, "not_found", "not found")
            return _json_response(
                Response,
                {
                    "trace": record.trace.to_dict(),
                    "stats": _stats_dict(
                        record.stats,
                        _latency_ms(record.trace.started_at, record.trace.ended_at),
                    ),
                },
            )
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.delete("/api/v1/traces/{trace_id}")
    async def delete_trace(request: Request, trace_id: str) -> Response:
        del request
        try:
            trace_id = _validated_id(trace_id, validate_trace_id)
            repository.delete_trace(trace_id)
            return Response(status_code=204)
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.get("/api/v1/health")
    async def health() -> Response:
        try:
            if not repository.health_check():
                return _error(Response, 500, "internal_error", "internal error")
            return _json_response(Response, {"status": "ok"})
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.get("/api/v2/compare/{left_trace_id}/{right_trace_id}")
    async def compare_traces(left_trace_id: str, right_trace_id: str) -> Response:
        try:
            left_trace_id = _validated_id(left_trace_id, validate_trace_id)
            right_trace_id = _validated_id(right_trace_id, validate_trace_id)
            if left_trace_id == right_trace_id:
                raise QueryRequestError("comparison requires distinct traces")
            left_input, right_input = repository.get_trace_comparison_inputs(
                left_trace_id,
                right_trace_id,
            )
            if left_input is None or right_input is None:
                return _error(Response, 404, "not_found", "not found")
            return _json_response(
                Response,
                compare_trace_inputs(
                    left_input.record,
                    left_input.spans,
                    right_input.record,
                    right_input.spans,
                ),
            )
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except ComparisonTooLargeError:
            return _error(Response, 413, "comparison_too_large", "comparison too large")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.get("/api/v3/compare/{left_trace_id}/{right_trace_id}")
    async def compare_traces_v3(request: Request, left_trace_id: str, right_trace_id: str) -> Response:
        try:
            if request.query_params:
                raise QueryRequestError("invalid query parameter")
            left_trace_id = _validated_id(left_trace_id, validate_trace_id)
            right_trace_id = _validated_id(right_trace_id, validate_trace_id)
            if left_trace_id == right_trace_id:
                raise QueryRequestError("comparison requires distinct traces")
            left_input, right_input = repository.get_trace_comparison_inputs(
                left_trace_id,
                right_trace_id,
            )
            if left_input is None or right_input is None:
                return _error(Response, 404, "not_found", "not found")
            return _json_response(
                Response,
                build_v3_comparison(
                    left_input.record,
                    left_input.spans,
                    right_input.record,
                    right_input.spans,
                ),
            )
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except ComparisonTooLargeError:
            return _error(Response, 413, "comparison_too_large", "comparison too large")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")

    @app.get("/api/v4/compare/{left_trace_id}/{right_trace_id}")
    async def compare_traces_v4(request: Request, left_trace_id: str, right_trace_id: str) -> Response:
        try:
            if request.query_params:
                raise QueryRequestError("invalid query parameter")
            left_trace_id = _validated_id(left_trace_id, validate_trace_id)
            right_trace_id = _validated_id(right_trace_id, validate_trace_id)
            if left_trace_id == right_trace_id:
                raise QueryRequestError("comparison requires distinct traces")
            left_input, right_input = repository.get_trace_comparison_inputs(
                left_trace_id,
                right_trace_id,
            )
            if left_input is None or right_input is None:
                return _error(Response, 404, "not_found", "not found")
            return _json_response(
                Response,
                build_v4_comparison(
                    left_input.record,
                    left_input.spans,
                    right_input.record,
                    right_input.spans,
                ),
            )
        except QueryRequestError:
            return _error(Response, 400, "invalid_request", "invalid request")
        except ComparisonTooLargeError:
            return _error(Response, 413, "comparison_too_large", "comparison too large")
        except Exception:
            return _error(Response, 500, "internal_error", "internal error")


__all__ = ["register_query_routes"]
