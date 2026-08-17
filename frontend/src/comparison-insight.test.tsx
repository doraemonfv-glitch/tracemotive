import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComparisonInsight } from "./comparison-insight";
import type {
  ComparisonPathSegment,
  ComparisonSpanRef,
  InvestigationCoordinate,
  InvestigationFinding,
  TraceInsightResponse,
} from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const leftTraceId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const rightTraceId = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const leftSpanId = "1111111111111111";
const rightSpanId = "2222222222222222";

function ref(traceId: string, spanId: string): ComparisonSpanRef {
  return { trace_id: traceId, span_id: spanId };
}

function path(name: string): ComparisonPathSegment[] {
  return [
    { type: "agent", operation: "agent.run", name: "Root", ordinal: 0 },
    { type: "tool", operation: "tool.call", name, ordinal: 0 },
  ];
}

function coordinate(name = "Changed tool", kind: InvestigationCoordinate["kind"] = "span"): InvestigationCoordinate {
  return { kind, semantic_path: path(name), group_signature: kind === "sibling_group" ? { type: "tool", operation: "tool.call", name } : null };
}

function finding(overrides: Partial<InvestigationFinding> = {}): InvestigationFinding {
  return {
    finding_id: "finding-0001",
    type: "tool_output_changed",
    coordinate: coordinate(),
    left: ref(leftTraceId, leftSpanId),
    right: ref(rightTraceId, rightSpanId),
    field_path: "/output",
    scope: "behavioral",
    observation_state: "confirmed_observation",
    reason_code: "captured_values_differ",
    observed: {
      left: { state: "captured", value: { result: "before" } },
      right: { state: "captured", value: { result: "after" } },
    },
    evidence: [{ kind: "field_observation", state: "different" }],
    relationships: [],
    ...overrides,
  };
}

function baseResponse(primary = finding()): TraceInsightResponse {
  return {
    comparison_version: "0.3",
    left_trace: { trace_id: leftTraceId, name: "Left run", status: "ok" },
    right_trace: { trace_id: rightTraceId, name: "Right run", status: "error" },
    summary: {
      alignment: { matched_spans: 1n, left_only_spans: 0n, right_only_spans: 0n, ambiguous_groups: 0n, unavailable_spans: 0n },
      finding_count: 1n,
      uncertainty_count: 0n,
      trace_fields: [],
    },
    investigation: {
      state: "identified",
      ordering_basis: "structural_triage_order",
      starting_point: {
        kind: primary.coordinate.kind,
        semantic_path: primary.coordinate.semantic_path,
        group_signature: primary.coordinate.group_signature,
        left: primary.left,
        right: primary.right,
        finding_id: primary.finding_id,
        label: "Inspect observed tool output change",
      },
      first_meaningful_divergence: { state: "identified", ordering_basis: "structural_triage_order", finding_id: primary.finding_id, reason_code: primary.reason_code },
      last_reliably_matched_point: { semantic_path: path("Stable"), left: ref(leftTraceId, "3333333333333333"), right: ref(rightTraceId, "4444444444444444"), state: "matched", reason: "before_first_finding" },
      evidence_summary: [],
      context_finding_ids: [],
      blocking_uncertainty_ids: [],
      limitations: [],
    },
    findings: [primary],
    uncertainties: [],
    detail_endpoint: { method: "GET", path: `/api/v2/compare/${leftTraceId}/${rightTraceId}`, comparison_version: "0.2" },
  };
}

function makeUncertain(): TraceInsightResponse {
  const response = baseResponse();
  response.investigation = {
    ...response.investigation,
    state: "uncertain",
    starting_point: null,
    first_meaningful_divergence: { state: "uncertain", ordering_basis: "structural_triage_order", finding_id: null, reason_code: null },
    blocking_uncertainty_ids: ["uncertainty-0001"],
    limitations: [{ uncertainty_id: "uncertainty-0001", reason_code: "repeated_sibling_ambiguity", side: "both", coordinate: coordinate("Repeated lookup", "sibling_group"), blocks_earlier_claim: true }],
  };
  response.findings = [];
  response.summary.finding_count = 0n;
  response.summary.uncertainty_count = 1n;
  response.uncertainties = [{ uncertainty_id: "uncertainty-0001", coordinate: coordinate("Repeated lookup", "sibling_group"), reason_code: "repeated_sibling_ambiguity", side: "both", blocks_earlier_claim: true, evidence: [] }];
  return response;
}

function makeNone(): TraceInsightResponse {
  const response = baseResponse();
  response.investigation = {
    ...response.investigation,
    state: "none",
    starting_point: null,
    first_meaningful_divergence: { state: "none", ordering_basis: "structural_triage_order", finding_id: null, reason_code: null },
    last_reliably_matched_point: { semantic_path: [], left: null, right: null, state: "none", reason: "no_prior_resolved_point" },
  };
  response.findings = [];
  response.summary.finding_count = 0n;
  return response;
}

function renderInsight(response: TraceInsightResponse, onOpenSpan = vi.fn(), onOpenDetails = vi.fn()) {
  return render(<ComparisonInsight response={response} onOpenSpan={onOpenSpan} onOpenDetails={onOpenDetails} detailsState="closed" />);
}

describe("V04-03 investigation cockpit", () => {
  it("renders the five sections and identified state with all supported primary actions", () => {
    const onOpenSpan = vi.fn();
    const onOpenDetails = vi.fn();
    const { container } = renderInsight(baseResponse(), onOpenSpan, onOpenDetails);

    expect(container.querySelectorAll("article[aria-label='Investigation cockpit'] > section")).toHaveLength(5);
    for (const heading of ["Look here", "What changed", "Evidence", "Next", "What TraceMotive does not know"]) {
      expect(screen.getByRole("heading", { name: heading, level: 2 })).toBeTruthy();
    }
    expect(screen.getByText("Identified")).toBeTruthy();
    expect(screen.getByText("agent / Root / tool / Changed tool")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open left span" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open right span" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Full comparison" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Open left span" }));
    fireEvent.click(screen.getByRole("button", { name: "Open right span" }));
    fireEvent.click(screen.getByRole("button", { name: "Full comparison" }));
    expect(onOpenSpan.mock.calls).toEqual([[leftTraceId, leftSpanId], [rightTraceId, rightSpanId]]);
    expect(onOpenDetails).toHaveBeenCalledOnce();
    expect(screen.getByText("This observed divergence is not proof of cause.")).toBeTruthy();
  });

  it("uses the plain-language label for a supported new-error finding", () => {
    const errorFinding = finding({
      type: "new_error",
      field_path: "/error",
      reason_code: "error_observed",
      observed: { left: { state: "present", value: null }, right: { state: "present", value: { type: "TimeoutError" } } },
    });

    renderInsight(baseResponse(errorFinding));

    expect(screen.getByRole("heading", { name: "New error observed" })).toBeTruthy();
    expect(screen.getByText("Observed error evidence differs between the left and right trace.")).toBeTruthy();
    expect(screen.queryByText("new_error")).toBeNull();
  });

  it("renders uncertain as a limitation and omits unsupported span actions", () => {
    renderInsight(makeUncertain());

    expect(screen.getByText("Uncertain")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "No supported starting point" })).toBeTruthy();
    expect(screen.getAllByText("Repeated members are ambiguous on both traces.")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Open left span" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open right span" })).toBeNull();
    expect(screen.getByRole("button", { name: "Full comparison" })).toBeTruthy();
  });

  it("uses safe generic copy for an unknown uncertainty reason", () => {
    const response = makeUncertain();
    const hostileReason = "future_<script>alert('xss')</script>";
    response.uncertainties[0].reason_code = hostileReason;

    const { container } = renderInsight(response);

    expect(screen.getAllByText("TraceMotive could not safely resolve this observation on both traces.")).toHaveLength(2);
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).not.toContain(hostileReason);
  });

  it("renders none distinctly without inventing a change or navigation target", () => {
    renderInsight(makeNone());

    expect(screen.getByText("None")).toBeTruthy();
    expect(screen.getAllByText("No supported behavioral divergence was found in the available observations.")).toHaveLength(2);
    expect(screen.getByText("No prior reliable structural match was available.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open left span" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open right span" })).toBeNull();
    expect(screen.getByRole("button", { name: "Full comparison" })).toBeTruthy();
  });

  it("distinguishes redacted and unavailable evidence without rendering hidden values", () => {
    const response = baseResponse(finding({
      observed: {
        left: { state: "redacted", value: "secret-left" },
        right: { state: "not_captured", value: "secret-right" },
      },
      evidence: [{ secret: "secret-evidence" }],
    }));
    renderInsight(response);

    expect(screen.getByText("Redacted; source value not shown")).toBeTruthy();
    expect(screen.getByText("Unavailable; source value was not captured")).toBeTruthy();
    expect(screen.queryByText("secret-left")).toBeNull();
    expect(screen.queryByText("secret-right")).toBeNull();
    expect(screen.queryByText("secret-evidence")).toBeNull();
  });

  it("keeps hostile names and captured values inert text", () => {
    const hostile = "<script>alert('xss')</script>";
    const response = baseResponse(finding({
      coordinate: coordinate(hostile),
      observed: { left: { state: "captured", value: hostile }, right: { state: "captured", value: "safe" } },
      evidence: [{ value: hostile }],
    }));
    const { container } = renderInsight(response);

    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain(hostile);
  });

  it("keeps large hostile values collapsed and inert", () => {
    const hostile = "<script>alert(1)</script>" + "x".repeat(1000);
    const response = baseResponse(finding({
      observed: { left: { state: "captured", value: "small" }, right: { state: "captured", value: hostile } },
    }));
    const { container } = renderInsight(response);
    const disclosure = screen.getByText("Right: large value, expand to inspect");
    const details = disclosure.closest("details");

    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain(hostile);
    fireEvent.click(disclosure);
    expect(details?.open).toBe(true);
  });

  it("omits actions for a group-level observation instead of forcing repeated-member identity", () => {
    const group = finding({
      coordinate: coordinate("Repeated lookup", "sibling_group"),
      left: null,
      right: null,
      field_path: null,
      type: "tool_repetition_changed",
      observed: { left: { state: "captured", value: 2 }, right: { state: "captured", value: 3 } },
    });
    const response = baseResponse(group);
    response.investigation.starting_point = { ...response.investigation.starting_point!, left: null, right: null, kind: "sibling_group" };
    renderInsight(response);

    expect(screen.getByText("A repeated group changed; no individual member identity was inferred.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open left span" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open right span" })).toBeNull();
  });

  it("provides a selectable local-hash fallback when clipboard access is denied", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    renderInsight(baseResponse());

    fireEvent.click(screen.getByRole("button", { name: "Copy local reference" }));
    expect(await screen.findByText("Clipboard unavailable. Select the text below.")).toBeTruthy();
    expect(screen.getByText(`#/compare/${leftTraceId}/${rightTraceId}`)).toBeTruthy();
    expect(screen.queryByText(/https?:\/\//)).toBeNull();
  });
});
