import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { decodeSpanDetailResponse, spanDetailUrl, spanListUrl, traceDetailUrl } from "./api";
import { formatJsonValue, SpanInspector, type SpanInspectorState } from "./span-inspector";
import { TraceDetail } from "./trace-detail";
import type { CanonicalJsonValue } from "./types";

const traceA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const traceB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function response(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "Content-Type": "application/json" } });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function detailObject({
  traceId = traceA,
  spanId = "shared",
  type = "llm",
  input = null,
  output = null,
  inputCapture = { state: "captured", reason: null, redacted: false },
  outputCapture = { state: "not_captured", reason: "disabled", redacted: false },
  error = null,
  status,
  details = {
    kind: "llm",
    provider: "openai",
    request_model: "request-model",
    response_model: "response-model",
    response_id: "response-id",
    usage: {
      input_tokens: 0,
      output_tokens: 9007199254740993,
      reasoning_output_tokens: null,
      cache_read_input_tokens: null,
      cache_creation_input_tokens: null,
    },
    finish_reasons: ["stop", "tool_calls"],
    request_parameters: { temperature: 0, enabled: false },
    estimated_cost: { amount: 0.0042, currency: "USD", estimated: true },
  },
}: {
  traceId?: string;
  spanId?: string;
  type?: string;
  input?: unknown;
  output?: unknown;
  inputCapture?: Record<string, unknown>;
  outputCapture?: Record<string, unknown>;
  error?: Record<string, unknown> | null;
  status?: "unset" | "ok" | "error";
  details?: Record<string, unknown>;
} = {}) {
  return {
    schema_version: "0.1",
    trace_id: traceId,
    span_id: spanId,
    parent_span_id: null,
    type,
    operation: `${type}.operation`,
    name: `${type} detail`,
    started_at: "2026-08-10T13:00:00.000000Z",
    ended_at: "2026-08-10T13:00:01.250000Z",
    status: status ?? (error === null ? "ok" : "error"),
    error,
    input,
    output,
    capture: { input: inputCapture, output: outputCapture },
    source: {
      framework: "test-framework",
      framework_version: "1.0",
      integration: "test-integration",
      integration_version: "1.0",
      native_trace_id: null,
      native_span_id: null,
      native_parent_span_id: null,
    },
    metadata: { observed: true },
    attributes: { empty: false },
    details,
  };
}

function detailWire(options: Parameters<typeof detailObject>[0] = {}): string {
  return JSON.stringify({ span: detailObject(options), latency_ms: 1250 });
}

function estimatedCostWire(amount: string): string {
  return detailWire().replace('"amount":0.0042', `"amount":${amount}`);
}

function spanListWire(traceId: string): string {
  return JSON.stringify({
    items: [
      { span: { trace_id: traceId, span_id: "shared", parent_span_id: null, type: "agent", operation: "agent.run", name: "Shared A", started_at: "2026-08-10T13:00:00.000000Z", ended_at: "2026-08-10T13:00:01.000000Z", status: "ok" }, latency_ms: 1000 },
      { span: { trace_id: traceId, span_id: "second", parent_span_id: null, type: "tool", operation: "tool.call", name: "Second B", started_at: "2026-08-10T13:00:00.100000Z", ended_at: "2026-08-10T13:00:01.100000Z", status: "ok" }, latency_ms: 1000 },
    ],
  });
}

function traceWire(traceId: string, name = "Inspector trace"): string {
  return JSON.stringify({
    trace: { schema_version: "0.1", trace_id: traceId, name, started_at: "2026-08-10T13:00:00.000000Z", ended_at: "2026-08-10T13:00:01.250000Z", status: "ok" },
    stats: { latency_ms: 1250, span_count: 2, error_count: 0, llm_call_count: 1, input_tokens: 0, output_tokens: 9007199254740993 },
  });
}

function loadedState(wire = detailWire()): SpanInspectorState {
  const decoded = decodeSpanDetailResponse(wire, traceA, "shared");
  return { kind: "loaded", identity: { trace_id: traceA, span_id: "shared" }, span: decoded.span };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Issue 13 span detail decoding", () => {
  it("enforces non-negative estimated costs from raw wire numbers without Number coercion", () => {
    for (const amount of ["0", "0.001", "9007199254740993", "1e400"]) {
      const decoded = decodeSpanDetailResponse(estimatedCostWire(amount), traceA, "shared");
      const estimatedCost = decoded.span.details.kind === "llm" ? decoded.span.details.estimated_cost : null;
      expect(estimatedCost).not.toBeNull();
      expect(formatJsonValue(estimatedCost!.amount)).toBe(amount);
    }

    for (const amount of ["-1", "-0.001", "-1e-400"]) {
      expect(() => decodeSpanDetailResponse(estimatedCostWire(amount), traceA, "shared")).toThrow("invalid");
    }
  });

  it("preserves contradictory observed status and Error evidence without normalization", () => {
    const hostile = "<img src=x onerror=alert(1)>observed error";
    const decoded = decodeSpanDetailResponse(detailWire({ status: "ok", error: { type: "ObservedError", message: hostile } }), traceA, "shared");
    expect(decoded.span.status).toBe("ok");
    expect(decoded.span.error).toEqual({ type: "ObservedError", message: hostile });
    render(<SpanInspector state={{ kind: "loaded", identity: { trace_id: traceA, span_id: "shared" }, span: decoded.span }} />);
    expect(screen.getByText("OK", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText("Observed status is OK while Error evidence is present.")).toBeTruthy();
    expect(screen.getByText(hostile)).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    cleanup();

    const statusErrorWithoutDetails = decodeSpanDetailResponse(detailWire({ status: "error", error: null }), traceA, "shared");
    render(<SpanInspector state={{ kind: "loaded", identity: { trace_id: traceA, span_id: "shared" }, span: statusErrorWithoutDetails.span }} />);
    expect(screen.getByText("Error", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText("No canonical error details were recorded.")).toBeTruthy();
    cleanup();

    expect(() => decodeSpanDetailResponse(detailWire({ status: "unset", error: { type: "ObservedError", message: "invalid lifecycle" } }), traceA, "shared")).toThrow("invalid");
  });

  it("uses the composite endpoint and preserves exact numbers through the production decoder", () => {
    const wire = detailWire({ input: { unsafe: 0, extreme: 0 }, output: null });
    const exactWire = wire.replace('"unsafe":0', '"unsafe":9007199254740993').replace('"extreme":0', '"extreme":1e400').replace('"temperature":0', '"temperature":1e-400').replace('"output_tokens":9007199254740992', '"output_tokens":9007199254740993');
    const decoded = decodeSpanDetailResponse(exactWire, traceA, "shared");

    expect(spanDetailUrl(traceA, "shared")).toBe(`${spanListUrl(traceA)}/shared`);
    expect(decoded.span.trace_id).toBe(traceA);
    expect(decoded.span.span_id).toBe("shared");
    expect(decoded.span.input).toMatchObject({ unsafe: expect.anything(), extreme: expect.anything() });
    expect(formatJsonValue(decoded.span.input)).toContain("9007199254740993");
    expect(formatJsonValue(decoded.span.input)).toContain("1e400");
    expect(formatJsonValue(decoded.span.details.kind === "llm" ? decoded.span.details.request_parameters! : null)).toContain("1e-400");
    expect(decoded.span.details.kind === "llm" ? decoded.span.details.usage.output_tokens : null).toBe(9007199254740993n);
    render(<SpanInspector state={{ kind: "loaded", identity: { trace_id: traceA, span_id: "shared" }, span: decoded.span }} />);
    expect(screen.getByText("9,007,199,254,740,993")).toBeTruthy();
    expect(screen.getByText(/9007199254740993/)).toBeTruthy();
    expect(() => decodeSpanDetailResponse(detailWire({ traceId: traceB }), traceA, "shared")).toThrow("invalid");
    expect(() => decodeSpanDetailResponse(detailWire({ spanId: "other" }), traceA, "shared")).toThrow("invalid");
  });

  it("keeps every CaptureInfo reason and captured JSON null distinct", () => {
    const reasons = ["disabled", "source_unavailable", "serialization_error", "size_limit", "not_yet_available"] as const;
    for (const reason of reasons) {
      const decoded = decodeSpanDetailResponse(detailWire({ input: null, inputCapture: { state: "not_captured", reason, redacted: false } }), traceA, "shared");
      render(<SpanInspector state={{ kind: "loaded", identity: { trace_id: traceA, span_id: "shared" }, span: decoded.span }} />);
      expect(screen.getAllByText(`Not captured: ${reason.replaceAll("_", " ")}`).length).toBeGreaterThan(0);
      cleanup();
    }

    render(
      <SpanInspector
        state={loadedState(detailWire({ input: null, inputCapture: { state: "captured", reason: null, redacted: false } }))}
      />,
    );
    expect(screen.getByText("Captured JSON null")).toBeTruthy();
    expect(screen.getAllByText("Not captured: disabled").length).toBeGreaterThan(0);
  });

  it("renders all Frozen detail variants without inventing fields", () => {
    const variants: Array<[string, Record<string, unknown>, string]> = [
      ["agent", { kind: "agent", agent_name: "Research", agent_version: "1.0" }, "Agent details"],
      ["tool", { kind: "tool", tool_name: "get_weather", tool_call_id: "call-1" }, "Tool details"],
      ["handoff", { kind: "handoff", from_agent: "triage", to_agent: "billing" }, "Handoff details"],
      ["retrieval", { kind: "retrieval" }, "Retrieval details"],
      ["custom", { kind: "custom", source_type: "guardrail" }, "Custom details"],
    ];
    for (const [type, details, heading] of variants) {
      const decoded = decodeSpanDetailResponse(detailWire({ type, details }), traceA, "shared");
      render(<SpanInspector state={{ kind: "loaded", identity: { trace_id: traceA, span_id: "shared" }, span: decoded.span }} />);
      expect(screen.getByRole("heading", { name: heading })).toBeTruthy();
      cleanup();
    }
  });

  it("renders hostile content as inert text and preserves error/status facts", () => {
    const hostile = `<script>alert(1)</script><img src=x onerror=alert(1)>\"><svg/onload=alert(1)>javascript:alert(1)`;
    const decoded = decodeSpanDetailResponse(detailWire({ input: hostile, output: { html: hostile }, inputCapture: { state: "captured", reason: null, redacted: true }, outputCapture: { state: "captured", reason: null, redacted: false }, error: { type: hostile, message: hostile } }), traceA, "shared");
    render(<SpanInspector state={{ kind: "loaded", identity: { trace_id: traceA, span_id: "shared" }, span: decoded.span }} />);

    expect(screen.getAllByText(/<script>alert\(1\)/).length).toBeGreaterThan(0);
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("svg")).toBeNull();
    expect(screen.getByText("Error", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText("Error type")).toBeTruthy();
  });

  it("formats deep and large JSON iteratively without changing data", () => {
    let deep: CanonicalJsonValue = "leaf";
    for (let index = 0; index < 3000; index += 1) {
      deep = { [`level-${index}`]: deep };
    }
    const large = "x".repeat(100_000);
    const deepText = formatJsonValue(deep);
    const largeText = formatJsonValue(large);
    expect(deepText).toContain('"level-2999"');
    expect(deepText).toContain('"leaf"');
    expect(largeText).toBe(JSON.stringify(large));
  });
});

describe("Issue 13 selection and request races", () => {
  it("shows no selection, then keeps the newest selected span when A resolves after B", async () => {
    const requestA = deferred<Response>();
    const requestB = deferred<Response>();
    const fetchMock = vi.fn((url: string) => {
      if (url === traceDetailUrl(traceA)) return Promise.resolve(response(traceWire(traceA)));
      if (url === spanListUrl(traceA)) return Promise.resolve(response(spanListWire(traceA)));
      if (url === spanDetailUrl(traceA, "shared")) return requestA.promise;
      if (url === spanDetailUrl(traceA, "second")) return requestB.promise;
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceDetail traceId={traceA} onBack={vi.fn()} />);
    expect((await screen.findAllByText("Select a span to inspect its canonical details.")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Select span Shared A" }));
    expect(await screen.findByText("Loading selected span...")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Select span Second B" }));
    requestB.resolve(response(detailWire({ spanId: "second", type: "tool", details: { kind: "tool", tool_name: "new-tool", tool_call_id: null } })));
    expect(await screen.findByText("new-tool")).toBeTruthy();
    requestA.resolve(response(detailWire({ spanId: "shared", type: "agent", details: { kind: "agent", agent_name: "stale-agent", agent_version: null } })));
    await waitFor(() => expect(screen.queryByText("stale-agent")).toBeNull());
    expect(screen.getByText("new-tool")).toBeTruthy();
    expect(fetchMock.mock.calls.map(([url]) => url)).toContain(spanDetailUrl(traceA, "shared"));
    expect(fetchMock.mock.calls.map(([url]) => url)).toContain(spanDetailUrl(traceA, "second"));
  });

  it("keeps old trace detail from populating a new trace with the same span ID", async () => {
    const aSpan = deferred<Response>();
    const bSpan = deferred<Response>();
    const fetchMock = vi.fn((url: string) => {
      if (url === traceDetailUrl(traceA)) return Promise.resolve(response(traceWire(traceA, "Trace A")));
      if (url === spanListUrl(traceA)) return Promise.resolve(response(spanListWire(traceA)));
      if (url === spanDetailUrl(traceA, "shared")) return aSpan.promise;
      if (url === traceDetailUrl(traceB)) return Promise.resolve(response(traceWire(traceB, "Trace B")));
      if (url === spanListUrl(traceB)) return Promise.resolve(response(spanListWire(traceB)));
      if (url === spanDetailUrl(traceB, "shared")) return bSpan.promise;
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<TraceDetail traceId={traceA} onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Select span Shared A" }));
    view.rerender(<TraceDetail traceId={traceB} onBack={vi.fn()} />);
    expect(await screen.findByText("Trace B")).toBeTruthy();
    expect(screen.queryByText("stale-agent")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Select span Shared A" }));
    bSpan.resolve(response(detailWire({ traceId: traceB, spanId: "shared", type: "tool", details: { kind: "tool", tool_name: "trace-b-tool", tool_call_id: null } })));
    expect(await screen.findByText("trace-b-tool")).toBeTruthy();
    aSpan.resolve(response(detailWire({ traceId: traceA, spanId: "shared", type: "agent", details: { kind: "agent", agent_name: "stale-agent", agent_version: null } })));
    await waitFor(() => expect(screen.queryByText("stale-agent")).toBeNull());
    expect(screen.getByText("trace-b-tool")).toBeTruthy();
  });

  it("handles not-found, malformed, and server-error responses without leaking bodies", async () => {
    const modes: Array<[number, string, string]> = [
      [404, "{\"error\":{\"message\":\"not found\"}}", "Span unavailable or not found."],
      [500, "C:\\secret\\database.db Authorization: Bearer secret-token", "Unable to load selected span."],
      [200, "{malformed", "Unable to load selected span."],
    ];
    for (const [status, body, expected] of modes) {
      const fetchMock = vi.fn((url: string) => {
        if (url === traceDetailUrl(traceA)) return Promise.resolve(response(traceWire(traceA)));
        if (url === spanListUrl(traceA)) return Promise.resolve(response(spanListWire(traceA)));
        return Promise.resolve(response(body, status));
      });
      vi.stubGlobal("fetch", fetchMock);
      render(<TraceDetail traceId={traceA} onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Select span Shared A" }));
      expect(await screen.findByText(expected)).toBeTruthy();
      expect(screen.queryByText(/database|Authorization|Bearer|secret-token/i)).toBeNull();
      cleanup();
      vi.unstubAllGlobals();
    }
  });
});
