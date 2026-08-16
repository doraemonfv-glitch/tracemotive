"""Deterministic local v0.3 first-value demo seeding."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Any
from urllib.parse import quote, urlsplit

import tracemotive
from tracemotive.canonical import AgentDetails, LLMDetails, LLMUsage, ToolDetails
from tracemotive.transport import validate_loopback_endpoint


DEFAULT_DEMO_ENDPOINT = "http://127.0.0.1:8765"
DEMO_PRIMARY_COORDINATE = (
    {"type": "agent", "operation": "agent.run", "name": "Order status agent", "ordinal": 0},
    {"type": "tool", "operation": "tool.call", "name": "Lookup policy", "ordinal": 0},
)


class DemoError(RuntimeError):
    """A safe, actionable deterministic demo failure."""


@dataclass(frozen=True, slots=True)
class DemoResult:
    endpoint: str
    reference_trace_id: str
    changed_trace_id: str

    @property
    def reference_trace_url(self) -> str:
        return f"{self.endpoint}/#/traces/{quote(self.reference_trace_id, safe='')}"

    @property
    def changed_trace_url(self) -> str:
        return f"{self.endpoint}/#/traces/{quote(self.changed_trace_id, safe='')}"

    @property
    def comparison_url(self) -> str:
        left = quote(self.reference_trace_id, safe="")
        right = quote(self.changed_trace_id, safe="")
        return f"{self.endpoint}/#/compare/{left}/{right}"


class _ResponseSynthesisError(Exception):
    """Internal control flow for the changed run's observed synthesis error."""

    def __init__(self, message: str, trace_id: str | None = None) -> None:
        super().__init__(message)
        self.trace_id = trace_id


def _validated_endpoint(endpoint: str) -> tuple[str, str, int]:
    try:
        validate_loopback_endpoint(endpoint)
        parsed = urlsplit(endpoint)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain a path, query, or fragment")
        if parsed.hostname is None:
            raise ValueError("endpoint must have a host")
        parsed_port = parsed.port
        if parsed_port is not None and not 1 <= parsed_port <= 65535:
            raise ValueError("endpoint port must be from 1 through 65535")
        port = 80 if parsed_port is None else parsed_port
    except (TypeError, ValueError) as exc:
        raise DemoError(
            "--endpoint must be an HTTP loopback URL without a path, query, or fragment"
        ) from exc
    base = f"http://{parsed.netloc}".rstrip("/")
    return base, parsed.hostname, port


def _server_unavailable(endpoint: str) -> DemoError:
    return DemoError(
        f"TraceMotive is not running on {endpoint}. "
        "Start it with: tracemotive serve"
    )


def _check_server(endpoint: str, hostname: str, port: int) -> None:
    connection = http.client.HTTPConnection(hostname, port, timeout=1.0)
    try:
        connection.request("GET", "/api/v1/health", headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise _server_unavailable(endpoint)
        try:
            health = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise _server_unavailable(endpoint) from exc
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise _server_unavailable(endpoint)
    except DemoError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        raise _server_unavailable(endpoint) from exc
    finally:
        connection.close()


def _llm_details() -> LLMDetails:
    return LLMDetails(
        "llm",
        "local-demo",
        "demo-model-v1",
        "demo-model-v1",
        None,
        LLMUsage(None, None, None, None, None),
        ["stop"],
        {"temperature": 0},
        None,
    )


def _tool_details(tool_name: str, tool_call_id: str) -> ToolDetails:
    return ToolDetails("tool", tool_name, tool_call_id)


def _seed_trace_once(*, changed: bool) -> str:
    name = "Order status reference run" if not changed else "Order status changed run"
    trace_id: str | None = None
    with tracemotive.trace(
        name,
        metadata={"demo": "v0.3-order-status", "scenario": "reference" if not changed else "changed"},
    ) as trace_value:
        if trace_value is None:
            raise DemoError("TraceMotive did not create the demo Trace")
        trace_id = trace_value.trace_id
        with tracemotive.span(
            "Order status agent",
            type="agent",
            operation="agent.run",
            details=AgentDetails("agent", "Order status agent", "demo-1"),
            input={"request": "Where is order ORD-1001?"},
        ):
            with tracemotive.span(
                "Classify request",
                type="llm",
                operation="llm.generate",
                details=_llm_details(),
                input={"request": "Where is order ORD-1001?"},
            ) as classify:
                classify.set_output({"intent": "order_status", "order_id": "ORD-1001"})

            with tracemotive.span(
                "Search customer",
                type="tool",
                operation="tool.call",
                details=_tool_details("search_customer", "search-customer-1"),
                input={"order_id": "ORD-1001"},
            ) as order_lookup:
                order_lookup.set_output({"order_id": "ORD-1001", "status": "in_transit"})

            with tracemotive.span(
                "Lookup policy",
                type="tool",
                operation="tool.call",
                details=_tool_details("lookup_policy", "lookup-policy-1"),
                input={"policy": "order_status"},
            ) as policy_lookup:
                policy_lookup.set_output(
                    {
                        "policy_version": "2026-01",
                        "allowed_statuses": ["in_transit", "delivered"],
                    }
                    if not changed
                    else {
                        "policy_version": "2026-02",
                        "allowed_statuses": ["delivered"],
                    }
                )

            try:
                with tracemotive.span(
                    "Synthesize response",
                    type="tool",
                    operation="tool.call",
                    details=_tool_details("synthesize_response", "synthesize-response-1"),
                    input={"request": "Where is order ORD-1001?"},
                ) as synthesis:
                    if changed:
                        raise _ResponseSynthesisError("response synthesis could not complete")
                    synthesis.set_output(
                        {"message": "Order ORD-1001 is in transit."}
                    )
            except _ResponseSynthesisError:
                pass

            if changed:
                with tracemotive.span(
                    "Record escalation",
                    type="tool",
                    operation="tool.call",
                    details=_tool_details("record_escalation", "record-escalation-1"),
                    input={"order_id": "ORD-1001"},
                ) as escalation:
                    escalation.set_output({"recorded": True, "queue": "support-review"})

        if changed:
            raise _ResponseSynthesisError("response synthesis could not complete", trace_id)

    if trace_id is None:
        raise DemoError("TraceMotive did not return a demo Trace ID")
    return trace_id


def _seed_trace(*, changed: bool) -> str:
    try:
        return _seed_trace_once(changed=changed)
    except _ResponseSynthesisError as exc:
        if not changed or exc.trace_id is None:
            raise DemoError("TraceMotive could not complete the deterministic demo") from exc
        return exc.trace_id


def _comparison_json(
    hostname: str,
    port: int,
    reference_trace_id: str,
    changed_trace_id: str,
) -> dict[str, Any]:
    path = (
        "/api/v3/compare/"
        + quote(reference_trace_id, safe="")
        + "/"
        + quote(changed_trace_id, safe="")
    )
    connection = http.client.HTTPConnection(hostname, port, timeout=3.0)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise DemoError(
            "TraceMotive seeded the demo traces, but the local v0.3 comparison was unavailable"
        ) from exc
    finally:
        connection.close()
    if response.status != 200:
        raise DemoError(
            "TraceMotive seeded the demo traces, but the local v0.3 comparison was unavailable"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise DemoError(
            "TraceMotive seeded the demo traces, but the local v0.3 comparison was invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise DemoError(
            "TraceMotive seeded the demo traces, but the local v0.3 comparison was invalid"
        )
    return payload


def _validate_demo_comparison(
    hostname: str,
    port: int,
    reference_trace_id: str,
    changed_trace_id: str,
) -> None:
    payload = _comparison_json(
        hostname,
        port,
        reference_trace_id,
        changed_trace_id,
    )
    investigation = payload.get("investigation")
    starting_point = (
        investigation.get("starting_point")
        if isinstance(investigation, dict)
        else None
    )
    findings = payload.get("findings")
    finding_by_id = {
        item.get("finding_id"): item
        for item in findings
        if isinstance(item, dict) and item.get("finding_id") is not None
    } if isinstance(findings, list) else {}
    primary = (
        finding_by_id.get(starting_point.get("finding_id"))
        if isinstance(starting_point, dict)
        else None
    )
    left_trace = payload.get("left_trace")
    right_trace = payload.get("right_trace")
    coordinate = primary.get("coordinate") if isinstance(primary, dict) else None
    valid = (
        payload.get("comparison_version") == "0.3"
        and isinstance(left_trace, dict)
        and left_trace.get("trace_id") == reference_trace_id
        and isinstance(right_trace, dict)
        and right_trace.get("trace_id") == changed_trace_id
        and isinstance(investigation, dict)
        and investigation.get("state") == "identified"
        and isinstance(starting_point, dict)
        and isinstance(primary, dict)
        and primary.get("type") == "tool_output_changed"
        and primary.get("scope") == "behavioral"
        and isinstance(coordinate, dict)
        and coordinate.get("semantic_path") == list(DEMO_PRIMARY_COORDINATE)
    )
    if not valid:
        raise DemoError(
            "TraceMotive seeded the demo traces, but the local v0.3 comparison "
            "did not produce the expected supported investigation point"
        )


def seed_demo(endpoint: str = DEFAULT_DEMO_ENDPOINT) -> DemoResult:
    """Check the existing server and seed one deterministic comparison pair."""

    base, hostname, port = _validated_endpoint(endpoint)
    _check_server(base, hostname, port)
    try:
        tracemotive.configure(enabled=True, endpoint=base, capture_content=True)
        reference_trace_id = _seed_trace(changed=False)
        changed_trace_id = _seed_trace(changed=True)
        if not tracemotive.flush(timeout_seconds=5.0):
            raise DemoError("TraceMotive could not flush the demo events to the local server")
        _validate_demo_comparison(
            hostname,
            port,
            reference_trace_id,
            changed_trace_id,
        )
    except DemoError:
        raise
    except Exception as exc:
        raise DemoError("TraceMotive could not seed the deterministic demo") from exc
    return DemoResult(base, reference_trace_id, changed_trace_id)


def format_demo_result(result: DemoResult) -> str:
    """Return the compact user-facing next steps for a seeded pair."""

    return "\n".join(
        (
            "Demo traces created:",
            f"  reference: {result.reference_trace_id}",
            f"  changed:   {result.changed_trace_id}",
            "",
            f"Reference trace: {result.reference_trace_url}",
            f"Changed trace:   {result.changed_trace_url}",
            f"Open comparison: {result.comparison_url}",
            "",
            "The comparison should show the first supported policy-output observation,",
            "later observed evidence, and the uncertainty/context boundaries without a causal claim.",
            "Each demo invocation creates a fresh pair and leaves existing traces untouched.",
        )
    )


__all__ = [
    "DEFAULT_DEMO_ENDPOINT",
    "DEMO_PRIMARY_COORDINATE",
    "DemoError",
    "DemoResult",
    "format_demo_result",
    "seed_demo",
]
