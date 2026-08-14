import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { comparisonUrl, decodeComparisonResponse } from "./api";
import { comparisonRoute, TraceMotiveApp } from "./app";
import { TraceComparison } from "./trace-comparison";
import { TraceList } from "./trace-list";
import type { TraceListResponse, TraceSummary } from "./types";

const leftId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const rightId = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function ref(trace_id: string, span_id: string) {
  return { trace_id, span_id };
}

function path(name: string, ordinal = 0) {
  return [{ type: "agent", operation: "agent.run", name: "Root", ordinal: 0 }, { type: "tool", operation: "tool.call", name, ordinal }];
}

function traceView(trace_id: string, name: string, status: "ok" | "error" = "ok") {
  return {
    trace: { trace_id, name, started_at: "2026-08-10T13:00:00.000000Z", ended_at: "2026-08-10T13:00:01.250000Z", status },
    stats: { latency_ms: 1250, span_count: 8, error_count: status === "error" ? 1 : 0, llm_call_count: 2, input_tokens: 12, output_tokens: 34 },
  };
}

function field(pathName: string, state: "different" | "unknown" | "left_only" | "right_only", left: unknown, right: unknown, reason: string | null = null) {
  return { path: pathName, state, left, right, reason };
}

function comparisonPayload() {
  return {
    comparison_version: "0.2",
    left_trace: traceView(leftId, "Baseline run"),
    right_trace: traceView(rightId, "Candidate run", "error"),
    summary: {
      trace_fields: [
        field("status", "different", "ok", "error"),
        field("ended_at", "different", "2026-08-10T13:00:01.250000Z", "2026-08-10T13:00:02.500000Z"),
        field("latency_ms", "different", 1250, 2500),
        field("span_count", "different", 8, 9),
        field("error_count", "different", 0, 1),
        field("llm_call_count", "different", 2, 2),
        field("input_tokens", "unknown", null, 12, "unsupported_observation"),
        field("output_tokens", "different", 34, 35),
      ],
      alignment: { matched_spans: 2, left_only_spans: 1, right_only_spans: 1, ambiguous_groups: 1, unavailable_spans: 1 },
      difference_count: 7,
      uncertainty_count: 2,
    },
    spans: [
      {
        alignment: "exact_match",
        semantic_path: path("Stable span"),
        left: ref(leftId, "stable-left"),
        right: ref(rightId, "stable-right"),
        differences: [],
        uncertainties: [],
      },
      {
        alignment: "exact_match",
        semantic_path: path("Changed tool"),
        left: ref(leftId, "changed-left"),
        right: ref(rightId, "changed-right"),
        differences: [
          field("status", "different", "ok", "error"),
          field("latency_ms", "different", 10, 20),
          field("details.request_model", "different", "model-a", "model-b"),
          field("details.request_parameters", "different", { temperature: 0 }, { temperature: 0.7 }),
          field("input", "different", "<script>alert(1)</script>", "safe input"),
          field("output", "different", { html: "<img src=x onerror=alert(1)>" }, { result: "safe" }),
          field("capture.output", "different", { state: "captured", reason: null, redacted: false }, { state: "captured", reason: null, redacted: true }, "redacted_observation"),
        ],
        uncertainties: [field("output", "unknown", null, null, "capture_unavailable")],
      },
      {
        alignment: "left_only",
        semantic_path: path("Only left"),
        left: ref(leftId, "left-only"),
        right: null,
        differences: [field("", "left_only", { name: "left-only" }, null, "missing_side")],
        uncertainties: [],
      },
      {
        alignment: "right_only",
        semantic_path: path("Only right"),
        left: null,
        right: ref(rightId, "right-only"),
        differences: [field("", "right_only", null, { name: "right-only" }, "missing_side")],
        uncertainties: [],
      },
    ],
    ambiguous_groups: [
      {
        alignment: "ambiguous_group",
        parent_path: [{ type: "agent", operation: "agent.run", name: "Root", ordinal: 0 }],
        group_signature: { type: "tool", operation: "tool.call", name: "Repeated lookup" },
        left_count: 2,
        right_count: 3,
        resolved_members: [],
        ambiguous_members: { left: [ref(leftId, "repeat-left-1"), ref(leftId, "repeat-left-2")], right: [ref(rightId, "repeat-right-1"), ref(rightId, "repeat-right-2"), ref(rightId, "repeat-right-3")] },
        left_only_count: null,
        right_only_count: null,
        reason: "repeated_sibling_ambiguity",
      },
    ],
    unavailable_spans: [
      { alignment: "unavailable", side: "right", span: ref(rightId, "ambiguous-child"), reason: "ambiguous_parent" },
    ],
  };
}

function comparisonResponse(status = 200): Response {
  return new Response(JSON.stringify(comparisonPayload()), { status, headers: { "Content-Type": "application/json" } });
}

function emptyComparisonResponse(): Response {
  const payload = comparisonPayload() as any;
  payload.summary.trace_fields = payload.summary.trace_fields.map((entry: any) => ({ ...entry, state: "same", right: entry.left, reason: null }));
  payload.summary.alignment = { matched_spans: 0, left_only_spans: 0, right_only_spans: 0, ambiguous_groups: 0, unavailable_spans: 0 };
  payload.summary.difference_count = 0;
  payload.summary.uncertainty_count = 0;
  payload.spans = [];
  payload.ambiguous_groups = [];
  payload.unavailable_spans = [];
  return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
}

const leftTrace: TraceSummary = {
  trace_id: leftId,
  name: "Baseline run",
  started_at: "2026-08-10T13:00:00.000000Z",
  ended_at: "2026-08-10T13:00:01.250000Z",
  status: "ok",
  latency_ms: 1250,
  span_count: 8n,
  error_count: 0n,
  llm_call_count: 2n,
  input_tokens: 12n,
  output_tokens: 34n,
};

const rightTrace: TraceSummary = { ...leftTrace, trace_id: rightId, name: "Candidate run", status: "error", error_count: 1n };

function traceListResponse(): Response {
  const payload: TraceListResponse = { items: [leftTrace, rightTrace], limit: 50, offset: 0n, total: 2n };
  return new Response(JSON.stringify(payload, (_key, value) => typeof value === "bigint" ? Number(value) : value), { status: 200, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "#/");
});

describe("TraceComparison", () => {
  it("decodes and requests the V02-20 endpoint without frontend alignment", async () => {
    const fetchMock = vi.fn().mockResolvedValue(comparisonResponse());
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);

    expect(screen.getByText("Loading comparison...")).toBeTruthy();
    expect(await screen.findByText("Trace-level differences")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(comparisonUrl(leftId, rightId), expect.objectContaining({ method: "GET" }));
    expect(decodeComparisonResponse(JSON.stringify(comparisonPayload()), leftId, rightId).comparison_version).toBe("0.2");
  });

  it("shows summary, all classifications, counts, changed fields, and inert hostile content", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(comparisonResponse()));
    const { container } = render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);

    expect(await screen.findByText("Baseline run")).toBeTruthy();
    expect(screen.getByText("Candidate run")).toBeTruthy();
    expect(screen.getAllByText("exact_match").length).toBe(2);
    expect(screen.getByText("left_only")).toBeTruthy();
    expect(screen.getByText("right_only")).toBeTruthy();
    expect(screen.getByText("ambiguous_group")).toBeTruthy();
    expect(screen.getByText("unavailable")).toBeTruthy();
    expect(screen.getByText("tool / Repeated lookup")).toBeTruthy();
    expect(screen.getByText("Left count")).toBeTruthy();
    expect(screen.getByText("Right count")).toBeTruthy();
    expect(screen.getAllByText("2").length).toBeGreaterThan(1);
    expect(screen.getAllByText("3").length).toBeGreaterThan(0);
    expect(screen.getByText("ambiguous_parent")).toBeTruthy();
    expect(screen.getByText("Repeated members were not paired because ordinal position alone cannot establish their identity.")).toBeTruthy();
    expect(screen.getByText("Model")).toBeTruthy();
    expect(screen.getByText("Request parameters")).toBeTruthy();
    expect(screen.getByText("Tool / span input")).toBeTruthy();
    expect(screen.getByText("capture output".replace("capture output", "Output capture state"))).toBeTruthy();
    expect(screen.getByText("redacted observation")).toBeTruthy();
    expect(screen.getByText("capture unavailable")).toBeTruthy();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    const modelField = screen.getByText("Model").closest(".comparison-field");
    expect(modelField?.className).toContain("comparison-field-highlight");
    const captureField = screen.getByText("Output capture state").closest(".comparison-field");
    expect(captureField?.className).toContain("comparison-field-caution");
    expect(captureField?.className).not.toContain("comparison-field-highlight");
    expect(screen.queryByText("repeat-left-1")).toBeNull();
  });

  it("keeps ambiguity and unavailable records visible under Changed only", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(comparisonResponse()));
    render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);
    await screen.findByText("Trace-level differences");

    fireEvent.click(screen.getByRole("checkbox", { name: "Changed only" }));
    expect(screen.queryByText("tool / Stable span")).toBeNull();
    expect(screen.getByText("tool / Changed tool")).toBeTruthy();
    expect(screen.getByText("left_only")).toBeTruthy();
    expect(screen.getByText("right_only")).toBeTruthy();
    expect(screen.getByText("ambiguous_group")).toBeTruthy();
    expect(screen.getByText("ambiguous_parent")).toBeTruthy();
  });

  it("handles loading, same-trace, 404, and 413 states without raw errors", async () => {
    const pending = new Promise<Response>(() => undefined);
    const fetchMock = vi.fn().mockReturnValue(pending);
    vi.stubGlobal("fetch", fetchMock);
    render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);
    expect(screen.getByText("Loading comparison...")).toBeTruthy();
    cleanup();

    const sameTraceFetch = vi.fn();
    vi.stubGlobal("fetch", sameTraceFetch);
    render(<TraceComparison leftTraceId={leftId} rightTraceId={leftId} onBack={vi.fn()} />);
    expect(screen.getByText("Choose two different traces to compare.")).toBeTruthy();
    expect(sameTraceFetch).not.toHaveBeenCalled();
    cleanup();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("C:\\secret\\db.sqlite", { status: 404 })));
    render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);
    expect(await screen.findByText("One or both selected traces were not found.")).toBeTruthy();
    expect(screen.queryByText(/db\.sqlite/)).toBeNull();
    cleanup();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("internal details", { status: 413 })));
    render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);
    expect(await screen.findByText("This comparison is too large to display. Choose smaller traces.")).toBeTruthy();
  });

  it("handles an empty comparison and invalid request state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(emptyComparisonResponse()));
    render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);
    expect(await screen.findByText("No span comparison records were returned.")).toBeTruthy();
    cleanup();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("invalid details", { status: 400 })));
    render(<TraceComparison leftTraceId={leftId} rightTraceId={rightId} onBack={vi.fn()} />);
    expect(await screen.findByText("The comparison request was invalid.")).toBeTruthy();
  });
});

describe("trace comparison selection", () => {
  it("selects two stored traces from the existing list without manual IDs", async () => {
    const onStartComparison = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(traceListResponse()));
    render(<TraceList onStartComparison={onStartComparison} />);

    await screen.findByText("Baseline run");
    const leftButtons = screen.getAllByRole("button", { name: "Use as left" });
    const rightButtons = screen.getAllByRole("button", { name: "Use as right" });
    fireEvent.click(leftButtons[0]);
    fireEvent.click(rightButtons[1]);
    expect(screen.getAllByText("Baseline run").some((node) => node.closest(".comparison-selection-slot") !== null)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Compare selected traces" }));
    await waitFor(() => expect(onStartComparison).toHaveBeenCalledWith(leftId, rightId));
  });

  it("navigates from the existing list into the comparison route", async () => {
    window.history.replaceState({}, "", "#/");
    const fetchMock = vi.fn((url: string) => Promise.resolve(url.startsWith("/api/v1/") ? traceListResponse() : comparisonResponse()));
    vi.stubGlobal("fetch", fetchMock);
    render(<TraceMotiveApp />);

    await screen.findByText("Baseline run");
    fireEvent.click(screen.getAllByRole("button", { name: "Use as left" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "Use as right" })[1]);
    fireEvent.click(screen.getByRole("button", { name: "Compare selected traces" }));
    expect(window.location.hash).toBe(comparisonRoute(leftId, rightId));
    expect(await screen.findByText("Trace-level differences")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(comparisonUrl(leftId, rightId), expect.objectContaining({ method: "GET" }));
  });
});
