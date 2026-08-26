"""Loopback-only HTTP access for the local TraceMotive CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
import json
import unicodedata
import webbrowser
from typing import Any
from urllib.parse import quote, urlsplit

from tracemotive.transport import DEFAULT_ENDPOINT, validate_loopback_endpoint


_REQUEST_TIMEOUT_SECONDS = 5.0
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TRACE_LIST_LIMIT = 100
_KNOWN_UNCERTAINTIES = {
    "capture_unavailable": "Captured content was unavailable on at least one side.",
    "redacted_observation": "Redaction prevented a content-level comparison.",
}


class LocalClientError(RuntimeError):
    """Base class for expected local-client failures."""


class InvalidEndpointError(LocalClientError):
    """The requested endpoint is outside the local HTTP boundary."""


class TransportFailureError(LocalClientError):
    """The local server could not be reached or completed the HTTP exchange."""


class ApiContractError(LocalClientError):
    """The local server returned data that is not a supported v3 contract."""


class ApiResponseStatusError(LocalClientError):
    """The local API rejected or could not complete the request."""


class BrowserOpenError(LocalClientError):
    """The comparison succeeded, but Python could not open the local browser."""


LocalClientFailures = (
    InvalidEndpointError,
    TransportFailureError,
    ApiContractError,
    ApiResponseStatusError,
)


@dataclass(frozen=True, slots=True)
class LocalEndpoint:
    base: str
    hostname: str
    port: int


@dataclass(frozen=True, slots=True)
class JsonResponse:
    value: Any
    raw: bytes


@dataclass(frozen=True, slots=True)
class SelectedTrace:
    trace_id: str
    name: str
    started_at: str
    ended_at: str | None


@dataclass(frozen=True, slots=True)
class LastSelection:
    left: SelectedTrace
    right: SelectedTrace


def validate_endpoint(endpoint: str) -> LocalEndpoint:
    """Validate an endpoint with the SDK transport's loopback policy."""

    try:
        validate_loopback_endpoint(endpoint)
        parsed = urlsplit(endpoint)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain a path, query, or fragment")
        if parsed.hostname is None:
            raise ValueError("endpoint must have a host")
        port = 80 if parsed.port is None else parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise InvalidEndpointError(
            "endpoint must be an HTTP localhost or loopback IP URL without "
            "credentials, an unexpected path, query, or fragment"
        ) from exc
    return LocalEndpoint(f"http://{parsed.netloc}", hostname, port)


def _get_json(endpoint: LocalEndpoint, path: str) -> JsonResponse:
    connection: HTTPConnection | None = None
    try:
        connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=_REQUEST_TIMEOUT_SECONDS)
        connection.request(
            "GET",
            path,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        status = response.status
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError, HTTPException) as exc:
        raise TransportFailureError("local TraceMotive server request failed") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    if len(body) > _MAX_RESPONSE_BYTES:
        raise ApiContractError("local TraceMotive response exceeds the 4 MiB bound")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        if status == 200:
            raise ApiContractError("local TraceMotive response was not valid JSON") from exc
        payload = None
    if status != 200:
        raise ApiResponseStatusError(f"local TraceMotive API returned status {status}")
    return JsonResponse(payload, body)


def compare_traces(
    left_trace_id: str,
    right_trace_id: str,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
) -> JsonResponse:
    """Request and validate one existing production v3 comparison."""

    local_endpoint = validate_endpoint(endpoint)
    path = (
        "/api/v3/compare/"
        + quote(left_trace_id, safe="")
        + "/"
        + quote(right_trace_id, safe="")
    )
    result = _get_json(local_endpoint, path)
    _validate_comparison(result.value, left_trace_id, right_trace_id)
    return result


def select_last_pair(
    trace_name: str,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
) -> LastSelection:
    """Select the first two exact names in one paged API ordering sequence."""

    local_endpoint = validate_endpoint(endpoint)
    encoded_name = quote(trace_name, safe="")
    matches: list[SelectedTrace] = []
    offset = 0
    total: int | None = None

    while len(matches) < 2 and (total is None or offset < total):
        path = f"/api/v1/traces?name={encoded_name}&limit={_TRACE_LIST_LIMIT}&offset={offset}"
        result = _get_json(local_endpoint, path)
        page_limit, returned_offset, page_total, items = _validate_trace_list(
            result.value,
            expected_offset=offset,
        )
        total = page_total
        del page_limit
        if not items:
            break

        for item in items:
            selected = _selected_trace(item, trace_name)
            if selected is not None:
                matches.append(selected)
                if len(matches) == 2:
                    break
        offset += _TRACE_LIST_LIMIT

    if len(matches) < 2:
        raise ApiContractError(
            "fewer than two traces have exactly the requested name; refusing to pair"
        )

    newest, older = matches
    return LastSelection(left=older, right=newest)


def last_comparison(
    trace_name: str,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
) -> tuple[LastSelection, JsonResponse]:
    selection = select_last_pair(trace_name, endpoint=endpoint)
    comparison = compare_traces(
        selection.left.trace_id,
        selection.right.trace_id,
        endpoint=endpoint,
    )
    return selection, comparison


def comparison_url(left_trace_id: str, right_trace_id: str, *, endpoint: str) -> str:
    local_endpoint = validate_endpoint(endpoint)
    return (
        local_endpoint.base
        + "/#/compare/"
        + quote(left_trace_id, safe="")
        + "/"
        + quote(right_trace_id, safe="")
    )


def open_comparison(
    left_trace_id: str,
    right_trace_id: str,
    *,
    endpoint: str,
) -> str:
    url = comparison_url(left_trace_id, right_trace_id, endpoint=endpoint)
    try:
        opened = webbrowser.open(url, new=0, autoraise=False)
    except Exception as exc:
        raise BrowserOpenError("could not open the local comparison URL") from exc
    if not opened:
        raise BrowserOpenError("no browser was available for the local comparison URL")
    return url


def _validate_comparison(payload: Any, left_trace_id: str, right_trace_id: str) -> None:
    if not isinstance(payload, dict) or payload.get("comparison_version") != "0.3":
        raise ApiContractError("local comparison is not a v3 response")
    left = _trace_identity(payload.get("left_trace"), "left")
    right = _trace_identity(payload.get("right_trace"), "right")
    if left["trace_id"] != left_trace_id or right["trace_id"] != right_trace_id:
        raise ApiContractError("local comparison identities do not match the request")

    investigation = payload.get("investigation")
    findings = payload.get("findings")
    uncertainties = payload.get("uncertainties")
    if not isinstance(investigation, dict) or not isinstance(findings, list) or not isinstance(uncertainties, list):
        raise ApiContractError("local comparison v3 response is incomplete")
    if any(not isinstance(item, dict) for item in findings + uncertainties):
        raise ApiContractError("local comparison v3 records are invalid")

    starting_point = investigation.get("starting_point")
    if starting_point is not None:
        if not isinstance(starting_point, dict):
            raise ApiContractError("local starting point is invalid")
        finding_id = starting_point.get("finding_id")
        label = starting_point.get("label")
        semantic_path = starting_point.get("semantic_path")
        if not isinstance(finding_id, str) or not isinstance(label, str) or not isinstance(semantic_path, list):
            raise ApiContractError("local starting point is incomplete")
        finding = next((item for item in findings if item.get("finding_id") == finding_id), None)
        if finding is None:
            raise ApiContractError("local starting point has no matching finding")


def _trace_identity(value: Any, side: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ApiContractError(f"local {side} trace identity is missing")
    identity = {}
    for field in ("trace_id", "name", "status"):
        item = value.get(field)
        if not isinstance(item, str):
            raise ApiContractError(f"local {side} trace {field} is invalid")
        identity[field] = item
    return identity


def _validate_trace_list(
    payload: Any,
    *,
    expected_offset: int,
) -> tuple[int, int, int, list[Any]]:
    if not isinstance(payload, dict):
        raise ApiContractError("local trace-list response is invalid")
    limit = payload.get("limit")
    offset = payload.get("offset")
    total = payload.get("total")
    items = payload.get("items")
    if (
        type(limit) is not int
        or limit != _TRACE_LIST_LIMIT
        or type(offset) is not int
        or offset != expected_offset
        or type(total) is not int
        or total < 0
        or not isinstance(items, list)
        or len(items) > limit
    ):
        raise ApiContractError("local trace-list response does not satisfy pagination")
    return limit, offset, total, items


def _selected_trace(item: Any, exact_name: str) -> SelectedTrace | None:
    if not isinstance(item, dict) or item.get("name") != exact_name:
        return None
    trace_id = item.get("trace_id")
    name = item.get("name")
    started_at = item.get("started_at")
    ended_at = item.get("ended_at")
    if (
        not isinstance(trace_id, str)
        or not trace_id
        or not isinstance(name, str)
        or not name
        or not isinstance(started_at, str)
        or not started_at
        or ended_at is not None and not isinstance(ended_at, str)
    ):
        raise ApiContractError("exact-name trace summary is invalid")
    return SelectedTrace(trace_id, name, started_at, ended_at)


def _display(value: Any) -> str:
    text = "<not returned>" if value is None else str(value)
    replacement = chr(0xFFFD)
    return "".join(
        replacement if unicodedata.category(character).startswith("C") else character
        for character in text
    )


def _coordinate_text(coordinate: Any) -> str:
    if not isinstance(coordinate, dict):
        return "<not returned>"
    parts: list[str] = [f"kind={_display(coordinate.get('kind'))}"]
    semantic_path = coordinate.get("semantic_path")
    if isinstance(semantic_path, list):
        rendered = []
        for segment in semantic_path:
            if not isinstance(segment, dict):
                continue
            rendered.append(
                f"{_display(segment.get('type'))}:{_display(segment.get('name'))}"
                f"#{_display(segment.get('ordinal'))}"
            )
        if rendered:
            parts.append("path=" + " > ".join(rendered))
    signature = coordinate.get("group_signature")
    if isinstance(signature, dict):
        parts.append(
            "group="
            f"{_display(signature.get('type'))}:{_display(signature.get('name'))}"
        )
    return " ".join(parts)


def format_comparison_result(
    comparison: JsonResponse,
    *,
    endpoint: str,
    selection: LastSelection | None = None,
) -> str:
    """Project supported v3 fields into compact terminal output."""

    payload = comparison.value
    if not isinstance(payload, dict):
        raise ApiContractError("cannot format an invalid comparison")
    left = _trace_identity(payload.get("left_trace"), "left")
    right = _trace_identity(payload.get("right_trace"), "right")
    investigation = payload.get("investigation", {})
    findings = payload.get("findings", [])
    uncertainties = payload.get("uncertainties", [])

    lines = ["Selected runs:"]
    if selection is None:
        lines.extend(
            (
                f"  left:  {_display(left['trace_id'])} ({_display(left['name'])}, status={_display(left['status'])})",
                f"  right: {_display(right['trace_id'])} ({_display(right['name'])}, status={_display(right['status'])})",
            )
        )
    else:
        lines.extend(
            (
                f"  left (older):  {_display(selection.left.trace_id)} ({_display(selection.left.name)}), started_at={_display(selection.left.started_at)}",
                f"  right (newer): {_display(selection.right.trace_id)} ({_display(selection.right.name)}), started_at={_display(selection.right.started_at)}",
            )
        )

    starting_point = investigation.get("starting_point")
    if isinstance(starting_point, dict):
        label = starting_point.get("label")
        finding_id = starting_point.get("finding_id")
        coordinate = starting_point
        finding = next(
            (item for item in findings if isinstance(item, dict) and item.get("finding_id") == finding_id),
            None,
        )
        lines.append("")
        lines.append(f"Look here: {_display(label)}")
        lines.append(f"Location: {_coordinate_text(coordinate)}")
        lines.append("What changed:")
        if finding is None:
            lines.append("  The v3 response did not include the referenced finding.")
        else:
            lines.append(
                f"  observation={_display(finding.get('observation_state'))}"
                f" type={_display(finding.get('type'))}"
                f" reason_code={_display(finding.get('reason_code'))}"
            )
            field_path = finding.get("field_path")
            if field_path is not None:
                lines.append(f"  field_path={_display(field_path)}")
            left_ref = finding.get("left")
            right_ref = finding.get("right")
            if isinstance(left_ref, dict) or isinstance(right_ref, dict):
                left_span = left_ref.get("span_id") if isinstance(left_ref, dict) else None
                right_span = right_ref.get("span_id") if isinstance(right_ref, dict) else None
                lines.append(
                    f"  spans left={_display(left_span)} right={_display(right_span)}"
                )
    else:
        lines.extend(
            (
                "",
                "Look here: No starting point was returned by /api/v3.",
                "What changed: No behavioral finding was designated as the starting point.",
            )
        )

    lines.extend(("", "Evidence limitation / Unknowns:"))
    if not uncertainties:
        lines.append("  No uncertainty records were returned.")
    else:
        for uncertainty in uncertainties:
            if not isinstance(uncertainty, dict):
                continue
            reason_code = uncertainty.get("reason_code")
            explanation = _KNOWN_UNCERTAINTIES.get(reason_code)
            suffix = f": {explanation}" if explanation is not None else ""
            lines.append(
                f"  reason_code={_display(reason_code)} side={_display(uncertainty.get('side'))}"
                f" blocks_earlier_claim={_display(uncertainty.get('blocks_earlier_claim'))}{suffix}"
            )

    lines.extend(
        (
            "",
            f"Comparison URL: {comparison_url(left['trace_id'], right['trace_id'], endpoint=endpoint)}",
            "This is observed evidence for investigation; it does not establish cause.",
        )
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_ENDPOINT",
    "LocalClientFailures",
    "ApiResponseStatusError",
    "ApiContractError",
    "BrowserOpenError",
    "InvalidEndpointError",
    "JsonResponse",
    "LastSelection",
    "LocalClientError",
    "LocalEndpoint",
    "SelectedTrace",
    "TransportFailureError",
    "compare_traces",
    "comparison_url",
    "format_comparison_result",
    "last_comparison",
    "open_comparison",
    "select_last_pair",
    "validate_endpoint",
]
