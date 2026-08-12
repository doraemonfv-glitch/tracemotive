import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TraceMotiveApp, traceIdFromLocation, traceRoute } from "./app";
import { decodeSpanListResponse, decodeTraceDetailResponse, spanListUrl, traceDetailUrl } from "./api";
import { TraceDetail } from "./trace-detail";

const traceId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const otherTraceId = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function detailResponse(id: string, name = "Workflow") {
  return `{"trace":{"schema_version":"0.1","trace_id":"${id}","name":${JSON.stringify(name)},"started_at":"2026-08-10T13:00:00.000000Z","ended_at":"2026-08-10T13:00:01.250000Z","status":"ok"},"stats":{"latency_ms":1250,"span_count":2,"error_count":1,"llm_call_count":1,"input_tokens":9007199254740993,"output_tokens":0}}`;
}

function spansResponse(id: string, firstName = "Root span") {
  return `{"items":[{"span":{"schema_version":"0.1","trace_id":"${id}","span_id":"root","parent_span_id":null,"type":"agent","operation":"agent.run","name":${JSON.stringify(firstName)},"started_at":"2026-08-10T13:00:00.000000Z","ended_at":"2026-08-10T13:00:01.000000Z","status":"ok","error":null},"latency_ms":1000},{"span":{"schema_version":"0.1","trace_id":"${id}","span_id":"child","parent_span_id":"root","type":"llm","operation":"llm.generate","name":"Child span","started_at":"2026-08-10T13:00:00.100000Z","ended_at":null,"status":"error","error":{"type":"TimeoutError","message":"secret detail must not render"}},"latency_ms":null}]}`;
}

function response(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "Content-Type": "application/json" } });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "#/" );
});

describe("Trace Detail", () => {
  it("decodes the real detail/span shapes losslessly and renders trace summary plus hierarchy", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(url.endsWith("/spans") ? response(spansResponse(traceId)) : response(detailResponse(traceId))),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceDetail traceId={traceId} onBack={vi.fn()} />);

    expect(await screen.findByText("Workflow")).toBeTruthy();
    expect(screen.getAllByText("OK").length).toBe(2);
    expect(screen.getByText("9,007,199,254,740,993")).toBeTruthy();
    expect(screen.getByText("0", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText("Root span")).toBeTruthy();
    expect(screen.getByText("Child span")).toBeTruthy();
    expect(screen.getByText("In progress")).toBeTruthy();
    expect(screen.getByText("Error")).toBeTruthy();
    expect(screen.queryByText(/secret detail must not render/)).toBeNull();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      traceDetailUrl(traceId),
      spanListUrl(traceId),
    ]);
  });

  it("distinguishes loading, zero-span, not-found, and controlled error states", async () => {
    const detail = deferred<Response>();
    const spans = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/spans") ? spans.promise : detail.promise));
    render(<TraceDetail traceId={traceId} onBack={vi.fn()} />);
    expect(screen.getByText("Loading trace detail...")).toBeTruthy();
    expect(screen.getByText("Loading span tree...")).toBeTruthy();

    detail.resolve(response(detailResponse(traceId)));
    spans.resolve(response('{"items":[]}'));
    expect(await screen.findByText("This trace contains no spans.")).toBeTruthy();

    cleanup();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response("C:\\secret\\database.db", 500)));
    render(<TraceDetail traceId={traceId} onBack={vi.fn()} />);
    expect(await screen.findByText("Unable to load trace detail.")).toBeTruthy();
    expect(screen.queryByText(/database\.db/)).toBeNull();

    cleanup();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response('{"error":{"code":"not_found","message":"not found"}}', 404)));
    render(<TraceDetail traceId={traceId} onBack={vi.fn()} />);
    expect(await screen.findByText("Trace not found.")).toBeTruthy();
    expect(screen.queryByText(/not found.*database/i)).toBeNull();
  });

  it("keeps the current trace when an obsolete trace resolves late", async () => {
    const aDetail = deferred<Response>();
    const aSpans = deferred<Response>();
    const bDetail = deferred<Response>();
    const bSpans = deferred<Response>();
    const fetchMock = vi.fn((url: string) => {
      if (url.includes(traceId)) {
        return url.endsWith("/spans") ? aSpans.promise : aDetail.promise;
      }
      return url.endsWith("/spans") ? bSpans.promise : bDetail.promise;
    });
    vi.stubGlobal("fetch", fetchMock);

    const view = render(<TraceDetail traceId={traceId} onBack={vi.fn()} />);
    view.rerender(<TraceDetail traceId={otherTraceId} onBack={vi.fn()} />);
    bDetail.resolve(response(detailResponse(otherTraceId, "Current trace B")));
    bSpans.resolve(response(spansResponse(otherTraceId, "Current root B")));
    expect(await screen.findByText("Current trace B")).toBeTruthy();
    expect(screen.getByText("Current root B")).toBeTruthy();
    expect(screen.getByRole("img", { name: /Current root B/ })).toBeTruthy();

    aDetail.resolve(response(detailResponse(traceId, "Stale trace A")));
    aSpans.resolve(response(spansResponse(traceId, "Stale root A")));
    await waitFor(() => expect(screen.queryByText("Stale trace A")).toBeNull());
    expect(screen.queryByText("Stale root A")).toBeNull();
    expect(screen.queryByRole("img", { name: /Stale root A/ })).toBeNull();
    expect(screen.getByText("Current trace B")).toBeTruthy();
  });

  it("uses a safe exact-ID route and list-to-detail navigation", async () => {
    const unusualId = "trace/with?reserved&chars";
    expect(traceRoute(unusualId)).toBe("#/traces/trace%2Fwith%3Freserved%26chars");
    expect(traceIdFromLocation({ hash: traceRoute(unusualId) })).toBe(unusualId);

    window.history.replaceState({}, "", "#/" );
    const listPayload = `{"items":[{"trace_id":"${traceId}","name":"Open me","started_at":"2026-08-10T13:00:00Z","ended_at":null,"status":"unset","latency_ms":null,"span_count":0,"error_count":0,"llm_call_count":0,"input_tokens":null,"output_tokens":null}],"limit":50,"offset":0,"total":1}`;
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (url.startsWith("/api/v1/traces?")) {
        return Promise.resolve(response(listPayload));
      }
      return Promise.resolve(url.endsWith("/spans") ? response('{"items":[]}') : response(detailResponse(traceId, "Open me")));
    }));

    render(<TraceMotiveApp />);
    fireEvent.click(await screen.findByRole("button", { name: /Open me/ }));
    expect(window.location.hash).toBe(traceRoute(traceId));
    expect(await screen.findByText("Trace summary")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back to traces" }));
    expect(window.location.hash).toBe("#/");
    expect(await screen.findByRole("heading", { name: "Traces" })).toBeTruthy();
  });
});

describe("Issue 11 response decoding", () => {
  it("keeps zero distinct from null and rejects mixed-trace span payloads", () => {
    const detail = decodeTraceDetailResponse(detailResponse(traceId), traceId);
    expect(detail.stats.input_tokens).toBe(9007199254740993n);
    expect(detail.stats.output_tokens).toBe(0n);
    expect(detail.stats.error_count).toBe(1n);
    expect(() => decodeSpanListResponse(spansResponse(otherTraceId), traceId)).toThrow("invalid");
  });
});
