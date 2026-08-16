import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComparisonInsight } from "./comparison-insight";
import type {
  ComparisonPathSegment,
  ComparisonSpanRef,
  InvestigationCoordinate,
  InvestigationFinding,
  InvestigationEvidenceReference,
  TraceInsightResponse,
} from "./types";

afterEach(() => cleanup());

const leftTraceId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const rightTraceId = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function ref(traceId: string, spanId: string): ComparisonSpanRef {
  return { trace_id: traceId, span_id: spanId };
}

function path(name: string, ordinal = 0): ComparisonPathSegment[] {
  return [
    { type: "agent", operation: "agent.run", name: "Root", ordinal: 0 },
    { type: "tool", operation: "tool.call", name, ordinal },
  ];
}

function coordinate(name: string, kind: InvestigationCoordinate["kind"] = "span", ordinal = 0): InvestigationCoordinate {
  return { kind, semantic_path: path(name, ordinal), group_signature: kind === "sibling_group" ? { type: "tool", operation: "tool.call", name } : null };
}

function finding(overrides: Partial<InvestigationFinding> = {}): InvestigationFinding {
  const findingCoordinate = overrides.coordinate ?? coordinate("Changed tool");
  return {
    finding_id: "finding-0001",
    type: "tool_output_changed",
    coordinate: findingCoordinate,
    left: ref(leftTraceId, "left-span"),
    right: ref(rightTraceId, "right-span"),
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
    left_trace: { trace_id: leftTraceId, name: "Good run", status: "ok" },
    right_trace: { trace_id: rightTraceId, name: "Bad run", status: "error" },
    summary: {
      alignment: { matched_spans: 2n, left_only_spans: 0n, right_only_spans: 0n, ambiguous_groups: 0n, unavailable_spans: 0n },
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
      last_reliably_matched_point: { semantic_path: path("Stable"), left: ref(leftTraceId, "stable-left"), right: ref(rightTraceId, "stable-right"), state: "matched", reason: "before_first_finding" },
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

function addEvidence(response: TraceInsightResponse, refs: InvestigationEvidenceReference[], findings: InvestigationFinding[]): TraceInsightResponse {
  return {
    ...response,
    summary: { ...response.summary, finding_count: BigInt(findings.length) },
    investigation: { ...response.investigation, evidence_summary: refs },
    findings,
  };
}

describe("ComparisonInsight", () => {
  it("makes the identified behavioral observation the primary place to begin", () => {
    render(<ComparisonInsight response={baseResponse()} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getAllByRole("heading", { name: "Investigation starting point" }).length).toBe(2);
    expect(screen.getByRole("heading", { name: "Tool output changed" })).toBeTruthy();
    expect(screen.getByText("TraceMotive observed this difference and selected it as the first supported place to investigate. TraceMotive does not know whether the difference caused later behavior.")).toBeTruthy();
    expect(screen.getByText("Last reliably matched point")).toBeTruthy();
    expect(screen.queryByText("tool_output_changed")).toBeNull();
  });

  it("uses the plain-language label for a new error primary finding", () => {
    const errorFinding = finding({
      type: "new_error",
      field_path: "/error",
      reason_code: "error_observed",
      observed: { left: { state: "present", value: null }, right: { state: "present", value: { type: "TimeoutError" } } },
    });

    render(<ComparisonInsight response={baseResponse(errorFinding)} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByRole("heading", { name: "New error observed" })).toBeTruthy();
    expect(screen.queryByText("aligned_span_error_changed")).toBeNull();
  });

  it("shows blocking uncertainty without inventing a starting-point card", () => {
    const later = finding({ finding_id: "finding-0002", type: "tool_added", coordinate: coordinate("Later tool"), left: null, right: ref(rightTraceId, "later-right"), field_path: null, reason_code: "right_only" });
    const response = addEvidence(baseResponse(), [{ finding_id: later.finding_id, relation: "observed_after", structural_relation: "descendant" }], [later]);
    response.investigation = {
      ...response.investigation,
      state: "uncertain",
      starting_point: null,
      first_meaningful_divergence: { state: "uncertain", ordering_basis: "structural_triage_order", finding_id: null, reason_code: null },
      last_reliably_matched_point: { semantic_path: [], left: null, right: null, state: "none", reason: "blocking_uncertainty" },
      blocking_uncertainty_ids: ["uncertainty-0001"],
    };
    response.summary.uncertainty_count = 1n;
    response.uncertainties = [{ uncertainty_id: "uncertainty-0001", coordinate: coordinate("Repeated lookup", "sibling_group"), reason_code: "repeated_sibling_ambiguity", side: "both", blocks_earlier_claim: true, evidence: [] }];

    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByText("TraceMotive cannot safely choose the first point")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Repeated members are ambiguous" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Additional behavioral observations" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Tool appeared" })).toBeTruthy();
    expect(screen.getByText("Descendant observation")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Investigation starting point" })).toBeNull();
  });

  it("keeps context-only changes visible without presenting them as divergence", () => {
    const context = finding({
      finding_id: "finding-context",
      type: "model_changed",
      scope: "context_only",
      field_path: "/details/request_model",
      reason_code: "aligned_values_differ",
      observed: { left: { state: "captured", value: "model-a" }, right: { state: "captured", value: "model-b" } },
    });
    const response = baseResponse(context);
    response.summary.finding_count = 1n;
    response.investigation = {
      ...response.investigation,
      state: "none",
      starting_point: null,
      first_meaningful_divergence: { state: "none", ordering_basis: "structural_triage_order", finding_id: null, reason_code: null },
      evidence_summary: [],
      context_finding_ids: [context.finding_id],
    };
    response.findings = [context];

    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByRole("heading", { name: "No supported behavioral divergence found" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Model changed" })).toBeTruthy();
    expect(screen.getByText("These observations provide context. They are not behavioral divergence or an investigation starting point.")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Investigation starting point" })).toBeNull();
  });

  it("renders every frozen finding enum with a plain-language label", () => {
    const findingTypes = [
      "new_error", "resolved_error", "tool_input_changed", "tool_output_changed", "tool_added", "tool_removed",
      "execution_subtree_added", "execution_subtree_removed", "tool_repetition_changed", "model_changed",
      "request_parameters_changed", "trace_status_changed",
    ] as const;
    const findings = findingTypes.map((type, index) => finding({ finding_id: `finding-context-${index}`, type, scope: "context_only" }));
    const response = baseResponse(findings[0]);
    response.findings = findings;
    response.summary.finding_count = BigInt(findings.length);
    response.investigation = {
      ...response.investigation,
      state: "none",
      starting_point: null,
      first_meaningful_divergence: { state: "none", ordering_basis: "structural_triage_order", finding_id: null, reason_code: null },
      context_finding_ids: findings.map((item) => item.finding_id),
    };
    response.findings.forEach((item) => { item.scope = "context_only"; });

    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    for (const label of [
      "New error observed", "Error no longer observed", "Tool input changed", "Tool output changed", "Tool appeared", "Tool disappeared",
      "Execution subtree appeared", "Execution subtree disappeared", "Tool repetition changed", "Model changed", "Request parameters changed", "Trace status changed",
    ]) {
      expect(screen.getByRole("heading", { name: label })).toBeTruthy();
    }
  });

  it("renders every frozen uncertainty reason with plain-language copy", () => {
    const reasonCodes = [
      "repeated_sibling_ambiguity", "missing_parent", "cycle", "invalid_structure", "ambiguous_parent",
      "capture_unavailable", "redacted_observation", "incomplete_trace", "unsupported_observation",
    ];
    const response = baseResponse();
    response.investigation = {
      ...response.investigation,
      state: "uncertain",
      starting_point: null,
      first_meaningful_divergence: { state: "uncertain", ordering_basis: "structural_triage_order", finding_id: null, reason_code: null },
      blocking_uncertainty_ids: reasonCodes.map((_, index) => `uncertainty-${index}`),
    };
    response.findings = [];
    response.summary.finding_count = 0n;
    response.summary.uncertainty_count = BigInt(reasonCodes.length);
    response.uncertainties = reasonCodes.map((reason_code, index) => ({ uncertainty_id: `uncertainty-${index}`, coordinate: null, reason_code, side: "both" as const, blocks_earlier_claim: true, evidence: [] }));

    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    for (const label of [
      "Repeated members are ambiguous", "A parent observation is missing", "The trace contains a structural cycle",
      "The trace structure could not be resolved safely", "The parent location is ambiguous", "Captured content is unavailable on one side",
      "The observation was redacted", "One trace ended before the observed execution was complete", "This observation type is not supported for a safe comparison",
    ]) {
      expect(screen.getByRole("heading", { name: label })).toBeTruthy();
    }
  });

  it("uses a safe generic label for an unknown uncertainty reason", () => {
    const response = baseResponse();
    response.investigation = {
      ...response.investigation,
      state: "uncertain",
      starting_point: null,
      first_meaningful_divergence: { state: "uncertain", ordering_basis: "structural_triage_order", finding_id: null, reason_code: null },
      blocking_uncertainty_ids: ["uncertainty-future"],
    };
    response.uncertainties = [{ uncertainty_id: "uncertainty-future", coordinate: null, reason_code: "future_reason_code", side: "both", blocks_earlier_claim: true, evidence: [] }];
    response.summary.uncertainty_count = 1n;

    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByRole("heading", { name: "TraceMotive could not safely resolve this observation" })).toBeTruthy();
  });

  it("uses explicit structural relationship labels for additional observations", () => {
    const primary = finding();
    const additional = [
      finding({ finding_id: "finding-descendant", type: "tool_output_changed", coordinate: coordinate("Child"), relationships: [{ relation: "descendant" }] }),
      finding({ finding_id: "finding-later", type: "new_error", coordinate: coordinate("Later"), relationships: [{ relation: "structurally_later_independent" }] }),
      finding({ finding_id: "finding-branch", type: "tool_removed", coordinate: coordinate("Other branch"), relationships: [{ relation: "unrelated_branch" }] }),
    ];
    const response = addEvidence(baseResponse(primary), additional.map((item) => ({ finding_id: item.finding_id, relation: "observed_after", structural_relation: item.relationships[0].relation })), [primary, ...additional]);

    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByText("Descendant observation")).toBeTruthy();
    expect(screen.getByText("Structurally later independent observation")).toBeTruthy();
    expect(screen.getByText("Unrelated branch observation")).toBeTruthy();
  });

  it("does not reinterpret an unknown structural relationship as a known one", () => {
    const primary = finding();
    const additional = finding({ finding_id: "finding-future", coordinate: coordinate("Future relation") });
    const response = addEvidence(baseResponse(primary), [{ finding_id: additional.finding_id, relation: "observed_after", structural_relation: "future_relation_code" }], [primary, additional]);

    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByText("Structural relationship not specified")).toBeTruthy();
    expect(screen.queryByText("Additional observation")).toBeNull();
  });

  it("represents repetition changes as group-level evidence", () => {
    const repetition = finding({
      type: "tool_repetition_changed",
      coordinate: coordinate("Repeated lookup", "sibling_group"),
      field_path: null,
      reason_code: "group_cardinality_changed",
      observed: { left: { state: "captured", value: 1 }, right: { state: "captured", value: 3 } },
    });
    render(<ComparisonInsight response={baseResponse(repetition)} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByText("Group-level observation")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Tool repetition changed" })).toBeTruthy();
    expect(screen.getAllByText("captured calls").length).toBe(2);
  });

  it("collapses large hostile values and renders them as inert text", () => {
    const hostile = "<script>alert(1)</script>" + "x".repeat(1000);
    const large = finding({ observed: { left: { state: "captured", value: "small" }, right: { state: "captured", value: hostile } } });
    const { container } = render(<ComparisonInsight response={baseResponse(large)} onOpenDetails={vi.fn()} detailsState="closed" />);
    const disclosure = screen.getByText("Value: large value, expand to inspect");
    const details = disclosure.closest("details");

    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain(hostile);
    fireEvent.click(disclosure);
    expect(details?.open).toBe(true);
  });

  it("states when no prior reliable structural match is available", () => {
    const response = baseResponse();
    response.investigation.last_reliably_matched_point = { semantic_path: [], left: null, right: null, state: "none", reason: "no_prior_resolved_point" };
    render(<ComparisonInsight response={response} onOpenDetails={vi.fn()} detailsState="closed" />);

    expect(screen.getByText("No prior reliable structural match was available.")).toBeTruthy();
  });

  it("keeps the full v0.2 detail action explicit and lazy", () => {
    const onOpenDetails = vi.fn();
    render(<ComparisonInsight response={baseResponse()} onOpenDetails={onOpenDetails} detailsState="closed" />);

    fireEvent.click(screen.getByRole("button", { name: "View detailed comparison" }));
    expect(onOpenDetails).toHaveBeenCalledOnce();
  });
});
