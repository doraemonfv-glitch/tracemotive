import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComparisonInsight, projectLaterObservations } from "./comparison-insight";
import type {
  ComparisonPathSegment,
  ComparisonSpanRef,
  InvestigationCoordinate,
  InvestigationFinding,
  InvestigationFindingType,
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

function laterFinding(
  findingId: string,
  name: string,
  type: InvestigationFindingType = "new_error",
  scope: InvestigationFinding["scope"] = "behavioral",
): InvestigationFinding {
  return finding({
    finding_id: findingId,
    type,
    coordinate: coordinate(name),
    left: ref(leftTraceId, findingId.slice(-16).padStart(16, "0")),
    right: ref(rightTraceId, findingId.slice(-16).padStart(16, "1")),
    field_path: type === "new_error" ? "/error" : "/output",
    scope,
  });
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

function withLaterObservations(
  response: TraceInsightResponse,
  items: Array<{ finding: InvestigationFinding; relation: string; structural_relation: string }>,
): TraceInsightResponse {
  return {
    ...response,
    findings: [...response.findings, ...items.map((item) => item.finding)],
    investigation: {
      ...response.investigation,
      evidence_summary: items.map((item) => ({
        finding_id: item.finding.finding_id,
        relation: item.relation,
        structural_relation: item.structural_relation,
      })),
    },
    summary: {
      ...response.summary,
      finding_count: BigInt(response.findings.length + items.length),
    },
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
  it("renders the identified state with all supported primary actions", () => {
    const onOpenSpan = vi.fn();
    const onOpenDetails = vi.fn();
    const { container } = renderInsight(baseResponse(), onOpenSpan, onOpenDetails);

    expect(container.querySelectorAll("article[aria-label='Investigation cockpit'] > section")).toHaveLength(4);
    for (const heading of ["Look here", "What changed", "Evidence", "What TraceMotive does not know"]) {
      expect(screen.getByRole("heading", { name: heading, level: 2 })).toBeTruthy();
    }
    expect(screen.queryByRole("heading", { name: "Next", level: 2 })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Later observations", level: 2 })).toBeNull();
    expect(screen.getByText("Identified")).toBeTruthy();
    expect(screen.getByText("agent / Root / tool / Changed tool")).toBeTruthy();
    expect(screen.getByText("Last reliably matched point")).toBeTruthy();
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

describe("V05-01 later observations", () => {
  it("renders allowlisted later observations in evidence_summary order", () => {
    const laterSame = laterFinding("finding-0002", "Same region tool", "tool_input_changed");
    const laterDescendant = laterFinding("finding-0003", "Descendant tool");
    const laterIndependent = laterFinding("finding-0004", "Independent tool", "tool_added");
    const response = withLaterObservations(baseResponse(), [
      { finding: laterSame, relation: "same_structural_region", structural_relation: "same_coordinate" },
      { finding: laterDescendant, relation: "descendant_evidence", structural_relation: "descendant" },
      { finding: laterIndependent, relation: "observed_after", structural_relation: "structurally_later_independent" },
    ]);

    renderInsight(response);

    expect(screen.getByRole("heading", { name: "Later observations", level: 2 })).toBeTruthy();
    expect(screen.getByText("These are additional supported observations in structural triage order, not runtime chronology or cause.")).toBeTruthy();
    const laterList = screen.getByRole("list", { name: "Later observations" });
    expect(laterList.textContent).toContain("same structural region");
    expect(laterList.textContent).toContain("descendant of the starting point");
    expect(laterList.textContent).toContain("later in structural triage order");
    expect(Array.from(laterList.querySelectorAll(".cockpit-later-item strong")).map((item) => item.textContent)).toEqual([
      "Tool input changed",
      "New error observed",
      "Tool appeared",
    ]);
    expect(screen.getByText("agent / Root / tool / Changed tool")).toBeTruthy();
    expect(projectLaterObservations(response.investigation, response.findings).items.map((item) => item.finding.finding_id)).toEqual([
      "finding-0002",
      "finding-0003",
      "finding-0004",
    ]);
  });

  it("excludes unsafe, unknown, context-only, unresolved, and primary later items", () => {
    const primary = finding();
    const unsafeBranch = laterFinding("finding-0002", "Unrelated tool");
    const siblingFiller = laterFinding("finding-0003", "Sibling filler");
    const blocked = laterFinding("finding-0004", "Blocked tool");
    const contextOnly = laterFinding("finding-0005", "Model metadata", "model_changed", "context_only");
    const unknownCombo = laterFinding("finding-0006", "Unknown combo");
    const unresolved = laterFinding("finding-0007", "Missing finding");
    const safe = laterFinding("finding-0008", "Safe later tool", "tool_added");
    const response = withLaterObservations(baseResponse(primary), [
      { finding: primary, relation: "same_structural_region", structural_relation: "same_coordinate" },
      { finding: unsafeBranch, relation: "observed_after", structural_relation: "unrelated_branch" },
      { finding: siblingFiller, relation: "observed_after", structural_relation: "additional_observation" },
      { finding: blocked, relation: "blocked_by_uncertainty", structural_relation: "additional_observation" },
      { finding: contextOnly, relation: "same_structural_region", structural_relation: "same_coordinate" },
      { finding: unknownCombo, relation: "future_relation", structural_relation: "future_structure" },
      { finding: unresolved, relation: "descendant_evidence", structural_relation: "descendant" },
      { finding: safe, relation: "observed_after", structural_relation: "structurally_later_independent" },
    ]);
    response.findings = response.findings.filter((item) => item.finding_id !== "finding-0007");

    renderInsight(response);

    const laterList = screen.getByRole("list", { name: "Later observations" });
    expect(laterList.querySelectorAll(".cockpit-later-item")).toHaveLength(1);
    expect(laterList.textContent).toContain("Tool appeared");
    expect(laterList.textContent).toContain("later in structural triage order");
    expect(laterList.textContent).not.toContain("finding-0001");
    expect(screen.queryByText("agent / Root / tool / Unrelated tool")).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Sibling filler")).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Blocked tool")).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Model metadata")).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Unknown combo")).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Missing finding")).toBeNull();
    expect(projectLaterObservations(response.investigation, response.findings).items.map((item) => item.finding.finding_id)).toEqual(["finding-0008"]);
  });

  it("excludes an allowlisted later item when the finding_id matches more than one finding", () => {
    const later = laterFinding("finding-0002", "Duplicate id tool", "tool_added");
    const duplicate = laterFinding("finding-0002", "Duplicate id twin", "new_error");
    const response = withLaterObservations(baseResponse(), [
      { finding: later, relation: "observed_after", structural_relation: "structurally_later_independent" },
    ]);
    response.findings = [...response.findings, duplicate];

    renderInsight(response);

    expect(screen.queryByRole("heading", { name: "Later observations", level: 2 })).toBeNull();
    expect(screen.queryByRole("list", { name: "Later observations" })).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Duplicate id tool")).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Duplicate id twin")).toBeNull();
    expect(projectLaterObservations(response.investigation, response.findings).items).toEqual([]);
  });

  it("excludes an allowlisted behavioral finding listed in context_finding_ids", () => {
    const later = laterFinding("finding-0002", "Context listed tool", "tool_added");
    const response = withLaterObservations(baseResponse(), [
      { finding: later, relation: "same_structural_region", structural_relation: "same_coordinate" },
    ]);
    response.investigation = {
      ...response.investigation,
      context_finding_ids: ["finding-0002"],
    };

    renderInsight(response);

    expect(screen.queryByRole("heading", { name: "Later observations", level: 2 })).toBeNull();
    expect(screen.queryByRole("list", { name: "Later observations" })).toBeNull();
    expect(screen.queryByText("agent / Root / tool / Context listed tool")).toBeNull();
    expect(projectLaterObservations(response.investigation, response.findings).items).toEqual([]);
  });

  it("renders at most five later observations in evidence_summary order", () => {
    const extras = Array.from({ length: 6 }, (_, index) => laterFinding(`finding-000${index + 2}`, `Later tool ${index + 1}`, "tool_added"));
    const response = withLaterObservations(baseResponse(), extras.map((item) => ({
      finding: item,
      relation: "observed_after",
      structural_relation: "structurally_later_independent",
    })));

    renderInsight(response);

    const laterItems = screen.getByRole("list", { name: "Later observations" }).querySelectorAll(".cockpit-later-item");
    expect(laterItems).toHaveLength(5);
    expect(Array.from(laterItems).map((item) => item.querySelector(".cockpit-path")?.textContent)).toEqual([
      "agent / Root / tool / Later tool 1",
      "agent / Root / tool / Later tool 2",
      "agent / Root / tool / Later tool 3",
      "agent / Root / tool / Later tool 4",
      "agent / Root / tool / Later tool 5",
    ]);
    expect(screen.getByText("Additional supported observations are available in Full comparison.")).toBeTruthy();
    expect(screen.queryByText("agent / Root / tool / Later tool 6")).toBeNull();
  });

  it("renders zero later observations for uncertain and none", () => {
    const later = laterFinding("finding-0002", "Later tool", "tool_added");
    const uncertain = withLaterObservations(makeUncertain(), [
      { finding: later, relation: "observed_after", structural_relation: "structurally_later_independent" },
    ]);
    const none = withLaterObservations(makeNone(), [
      { finding: later, relation: "same_structural_region", structural_relation: "same_coordinate" },
    ]);

    const first = renderInsight(uncertain);
    expect(screen.queryByRole("heading", { name: "Later observations", level: 2 })).toBeNull();
    expect(screen.queryByRole("list", { name: "Later observations" })).toBeNull();
    expect(projectLaterObservations(uncertain.investigation, uncertain.findings).items).toEqual([]);
    first.unmount();

    renderInsight(none);
    expect(screen.queryByRole("heading", { name: "Later observations", level: 2 })).toBeNull();
    expect(screen.queryByRole("list", { name: "Later observations" })).toBeNull();
    expect(projectLaterObservations(none.investigation, none.findings).items).toEqual([]);
  });

  it("keeps hostile later names inert and does not change existing actions", () => {
    const hostile = "<script>alert('later')</script>";
    const later = laterFinding("finding-0002", hostile, "new_error");
    const onOpenSpan = vi.fn();
    const onOpenDetails = vi.fn();
    const { container } = renderInsight(
      withLaterObservations(baseResponse(), [
        { finding: later, relation: "descendant_evidence", structural_relation: "descendant" },
      ]),
      onOpenSpan,
      onOpenDetails,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain(hostile);
    expect(screen.getByText("descendant of the starting point")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open left span" }));
    fireEvent.click(screen.getByRole("button", { name: "Open right span" }));
    fireEvent.click(screen.getByRole("button", { name: "Full comparison" }));
    expect(onOpenSpan.mock.calls).toEqual([[leftTraceId, leftSpanId], [rightTraceId, rightSpanId]]);
    expect(onOpenDetails).toHaveBeenCalledOnce();
  });
});
