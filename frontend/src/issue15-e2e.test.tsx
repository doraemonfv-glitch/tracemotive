import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentLensApp } from "./app";

const traceId = "8bf92f3577b34da6a3ce929d0e0e4736";
const secret = "TEST-SECRET-VALUE";

function response(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "Content-Type": "application/json" } });
}

function traceListWire(): string {
  return `{"items":[{"trace_id":"${traceId}","name":"Issue 15 deterministic trace","started_at":"2026-08-10T13:00:00.000000Z","ended_at":"2026-08-10T13:00:01.000000Z","status":"ok","latency_ms":1000,"span_count":2,"error_count":1,"llm_call_count":1,"input_tokens":0,"output_tokens":0}],"limit":50,"offset":0,"total":1}`;
}

function traceDetailWire(): string {
  return `{"trace":{"schema_version":"0.1","trace_id":"${traceId}","name":"Issue 15 deterministic trace","started_at":"2026-08-10T13:00:00.000000Z","ended_at":"2026-08-10T13:00:01.000000Z","status":"ok"},"stats":{"latency_ms":1000,"span_count":2,"error_count":1,"llm_call_count":1,"input_tokens":0,"output_tokens":0}}`;
}

function spanListWire(): string {
  return `{"items":[{"span":{"trace_id":"${traceId}","span_id":"agent","parent_span_id":null,"type":"agent","operation":"agent.run","name":"Agent root","started_at":"2026-08-10T13:00:00.000000Z","ended_at":"2026-08-10T13:00:01.000000Z","status":"ok"},"latency_ms":1000},{"span":{"trace_id":"${traceId}","span_id":"child","parent_span_id":"agent","type":"llm","operation":"llm.generate","name":"Child LLM","started_at":"2026-08-10T13:00:00.100000Z","ended_at":"2026-08-10T13:00:00.900000Z","status":"error"},"latency_ms":800}]}`;
}

function spanDetailWire(): string {
  return `{"span":{"schema_version":"0.1","trace_id":"${traceId}","span_id":"child","parent_span_id":"agent","type":"llm","operation":"llm.generate","name":"Child LLM","started_at":"2026-08-10T13:00:00.100000Z","ended_at":"2026-08-10T13:00:00.900000Z","status":"error","error":{"type":"FixtureError","message":"offline fixture"},"input":"Bearer [REDACTED]","output":null,"capture":{"input":{"state":"captured","reason":null,"redacted":true},"output":{"state":"not_captured","reason":"source_unavailable","redacted":false}},"source":{"framework":null,"framework_version":null,"integration":"agentlens.manual","integration_version":"0.1","native_trace_id":null,"native_span_id":null,"native_parent_span_id":null},"metadata":{},"attributes":{},"details":{"kind":"llm","provider":"fixture-provider","request_model":"offline-model","response_model":null,"response_id":null,"usage":{"input_tokens":0,"output_tokens":0,"reasoning_output_tokens":null,"cache_read_input_tokens":null,"cache_creation_input_tokens":null},"finish_reasons":[],"request_parameters":null,"estimated_cost":null}},"latency_ms":800}`;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "#/" );
});

describe("Issue 15 UI path", () => {
  it("walks a Query API-shaped deterministic trace from list to detail and inspector", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.startsWith("/api/v1/traces?")) {
        return Promise.resolve(response(traceListWire()));
      }
      if (url.endsWith(`/traces/${traceId}/spans/child`)) {
        return Promise.resolve(response(spanDetailWire()));
      }
      if (url.endsWith(`/traces/${traceId}/spans`)) {
        return Promise.resolve(response(spanListWire()));
      }
      return Promise.resolve(response(traceDetailWire()));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentLensApp />);

    fireEvent.click(await screen.findByRole("button", { name: /Issue 15 deterministic trace/ }));
    expect(await screen.findByRole("heading", { name: "Issue 15 deterministic trace" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Select span Child LLM" }));

    expect(await screen.findByRole("heading", { name: "Error" })).toBeTruthy();
    expect(screen.getByText("Captured (redacted)")).toBeTruthy();
    expect(screen.getByText("Not captured: source unavailable")).toBeTruthy();
    expect(screen.queryByText(secret)).toBeNull();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/traces?limit=50&offset=0",
      `/api/v1/traces/${traceId}`,
      `/api/v1/traces/${traceId}/spans`,
      `/api/v1/traces/${traceId}/spans/child`,
    ]);
  });
});
