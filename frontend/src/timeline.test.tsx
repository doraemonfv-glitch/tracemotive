import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { buildTimelineModel, parseCanonicalTimestampToUs, Timeline } from "./timeline";
import type { SpanRecord, TraceHeader } from "./types";

const traceA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const traceB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const start = "2026-08-10T13:00:00.000000Z";

function trace(overrides: Partial<TraceHeader> = {}): TraceHeader {
  return {
    trace_id: traceA,
    name: "Timeline trace",
    started_at: start,
    ended_at: "2026-08-10T13:00:00.001000Z",
    status: "ok",
    ...overrides,
  };
}

function span(span_id: string, overrides: Partial<SpanRecord> = {}): SpanRecord {
  return {
    trace_id: traceA,
    span_id,
    parent_span_id: null,
    type: "agent",
    operation: "agent.run",
    name: span_id,
    started_at: start,
    ended_at: "2026-08-10T13:00:00.001000Z",
    status: "ok",
    latency_ms: 1,
    ...overrides,
  };
}

function geometry(model: ReturnType<typeof buildTimelineModel>, spanId: string) {
  const row = model.rows.find((entry) => entry.span.span_id === spanId);
  if (row === undefined || row.geometry === null) {
    throw new Error(`No geometry for ${spanId}`);
  }
  return row.geometry;
}

function legacyNumberPercent(ratio: { numerator: bigint; denominator: bigint }): number {
  return (Number(ratio.numerator) / Number(ratio.denominator)) * 100;
}

afterEach(() => cleanup());

describe("Issue 12 exact timeline timestamps", () => {
  it("parses only canonical UTC-Z timestamps into exact microseconds", () => {
    const base = parseCanonicalTimestampToUs("2026-08-10T13:00:00Z");
    expect(base).not.toBeNull();
    expect([
      "2026-08-10T13:00:00Z",
      "2026-08-10T13:00:00.000001Z",
      "2026-08-10T13:00:00.000010Z",
      "2026-08-10T13:00:00.000100Z",
      "2026-08-10T13:00:00.001000Z",
      "2026-08-10T13:00:00.999999Z",
    ].map(parseCanonicalTimestampToUs)).toEqual([
      base,
      base! + 1n,
      base! + 10n,
      base! + 100n,
      base! + 1_000n,
      base! + 999_999n,
    ]);
    for (const invalid of [
      "2026-08-10T13:00:00+00:00",
      "2026-08-10T13:00:00.1234567Z",
      "2026-02-30T13:00:00Z",
      "2026-08-10T24:00:00Z",
      "not-a-timestamp",
    ]) {
      expect(parseCanonicalTimestampToUs(invalid)).toBeNull();
    }
  });

  it("keeps microsecond starts and widths distinct without Date millisecond collapse", () => {
    const model = buildTimelineModel(trace(), [
      span("one", { ended_at: "2026-08-10T13:00:00.000001Z", latency_ms: 0.001 }),
      span("ten", { started_at: "2026-08-10T13:00:00.000001Z", ended_at: "2026-08-10T13:00:00.000011Z", latency_ms: 0.01 }),
      span("hundred", { started_at: "2026-08-10T13:00:00.000011Z", ended_at: "2026-08-10T13:00:00.000111Z", latency_ms: 0.1 }),
      span("millisecond", { started_at: "2026-08-10T13:00:00.000000Z", ended_at: "2026-08-10T13:00:00.001000Z", latency_ms: 1 }),
    ]);

    expect(model.rows.map((row) => row.span.span_id)).toEqual(["one", "ten", "hundred", "millisecond"]);
    expect(geometry(model, "one")).toMatchObject({ startPercent: "0%", widthPercent: "0.1%", marker: false });
    expect(geometry(model, "ten")).toMatchObject({ startPercent: "0.1%", widthPercent: "1%", marker: false });
    expect(geometry(model, "hundred")).toMatchObject({ startPercent: "1.1%", widthPercent: "10%", marker: false });
    expect(geometry(model, "millisecond")).toMatchObject({ startPercent: "0%", widthPercent: "100%", marker: false });
  });

  it("preserves distinct one-microsecond positions across the complete valid timestamp range", () => {
    const model = buildTimelineModel(trace({
      started_at: "0001-01-01T00:00:00.000000Z",
      ended_at: "9999-12-31T23:59:59.999999Z",
    }), [
      span("one-microsecond", { started_at: "0001-01-01T00:00:00.000001Z", ended_at: "0001-01-01T00:00:00.000002Z", latency_ms: 0.001 }),
      span("beginning-next", { started_at: "0001-01-01T00:00:00.000002Z", ended_at: "0001-01-01T00:00:00.000003Z", latency_ms: 0.001 }),
      span("middle", { started_at: "5000-01-01T00:00:00.000000Z", ended_at: "5000-01-01T00:00:00.000001Z", latency_ms: 0.001 }),
      span("middle-next", { started_at: "5000-01-01T00:00:00.000001Z", ended_at: "5000-01-01T00:00:00.000002Z", latency_ms: 0.001 }),
      span("near-end", { started_at: "9999-12-31T23:59:59.999997Z", ended_at: "9999-12-31T23:59:59.999998Z", latency_ms: 0.001 }),
      span("near-end-next", { started_at: "9999-12-31T23:59:59.999998Z", ended_at: "9999-12-31T23:59:59.999999Z", latency_ms: 0.001 }),
    ]);

    expect(model.domainState).toBe("complete");
    expect(geometry(model, "one-microsecond").startPercent).not.toBe(geometry(model, "beginning-next").startPercent);
    expect(geometry(model, "middle").startPercent).not.toBe(geometry(model, "middle-next").startPercent);
    expect(geometry(model, "near-end").startPercent).not.toBe(geometry(model, "near-end-next").startPercent);
    expect(geometry(model, "near-end").startPercent).not.toBe("100%");
    expect(geometry(model, "near-end-next").startPercent).not.toBe("100%");
    expect(geometry(model, "near-end").widthPercent).not.toBe("0%");
    // This is the previously shipped Number-ratio failure reproduced in the
    // test: the old projection collapses both pairs before CSS is involved.
    expect(legacyNumberPercent(geometry(model, "middle").startRatio)).toBe(
      legacyNumberPercent(geometry(model, "middle-next").startRatio),
    );
    expect(legacyNumberPercent(geometry(model, "near-end").startRatio)).toBe(
      legacyNumberPercent(geometry(model, "near-end-next").startRatio),
    );
  });

  it("uses Trace start and end as the observed domain, clamps out-of-range spans, and never emits invalid geometry", () => {
    const model = buildTimelineModel(trace(), [
      span("before", { started_at: "2026-08-09T23:59:59.999990Z", ended_at: "2026-08-10T13:00:00.000010Z", latency_ms: 0.02 }),
      span("after", { started_at: "2026-08-10T13:00:00.001500Z", ended_at: "2026-08-10T13:00:00.001600Z", latency_ms: 0.1 }),
    ]);

    expect(model.domainEndUs).toBe(parseCanonicalTimestampToUs("2026-08-10T13:00:00.001000Z"));
    expect(geometry(model, "before")).toMatchObject({ startPercent: "0%", widthPercent: "1%", clipped: true });
    expect(geometry(model, "after")).toMatchObject({ startPercent: "100%", widthPercent: "0%", marker: true, clipped: true });
    for (const row of model.rows) {
      expect(row.geometry?.startPercent).toMatch(/^(?:0|[1-9]\d?|100)(?:\.\d+)?%$/);
      expect(row.geometry?.widthPercent).toMatch(/^(?:0|[1-9]\d?|100)(?:\.\d+)?%$/);
    }
  });

  it("represents sequential, parallel, same-start, and same-end observations without inferring relationships", () => {
    const model = buildTimelineModel(trace({ ended_at: "2026-08-10T13:00:00.001000Z" }), [
      span("sequential-a", { started_at: "2026-08-10T13:00:00.000000Z", ended_at: "2026-08-10T13:00:00.000250Z", latency_ms: 0.25 }),
      span("sequential-b", { started_at: "2026-08-10T13:00:00.000250Z", ended_at: "2026-08-10T13:00:00.000500Z", latency_ms: 0.25 }),
      span("parallel", { started_at: "2026-08-10T13:00:00.000100Z", ended_at: "2026-08-10T13:00:00.000600Z", latency_ms: 0.5 }),
      span("same-start", { started_at: "2026-08-10T13:00:00.000100Z", ended_at: "2026-08-10T13:00:00.000400Z", latency_ms: 0.3 }),
      span("same-end", { started_at: "2026-08-10T13:00:00.000200Z", ended_at: "2026-08-10T13:00:00.000600Z", latency_ms: 0.4 }),
    ]);

    expect(geometry(model, "sequential-a").startRatio.numerator + geometry(model, "sequential-a").widthRatio.numerator).toBe(geometry(model, "sequential-b").startRatio.numerator);
    expect(geometry(model, "parallel").startRatio.numerator).toBe(geometry(model, "same-start").startRatio.numerator);
    expect(geometry(model, "parallel").startRatio.numerator + geometry(model, "parallel").widthRatio.numerator).toBe(geometry(model, "same-end").startRatio.numerator + geometry(model, "same-end").widthRatio.numerator);
    expect(model.rows.map((row) => row.span.span_id)).toEqual(["sequential-a", "sequential-b", "parallel", "same-start", "same-end"]);
  });

  it("uses only the latest observed span timestamp for an unfinished Trace and preserves zero domains", () => {
    const incompleteTrace = trace({ ended_at: null });
    const model = buildTimelineModel(incompleteTrace, [
      span("completed", { ended_at: "2026-08-10T13:00:00.000100Z", latency_ms: 0.1 }),
      span("running", { started_at: "2026-08-10T13:00:00.000120Z", ended_at: null, latency_ms: null, status: "unset" }),
    ]);
    expect(model.domainEndUs).toBe(parseCanonicalTimestampToUs("2026-08-10T13:00:00.000120Z"));
    expect(model.domainState).toBe("unfinished");
    expect(geometry(model, "running")).toMatchObject({ startPercent: "100%", widthPercent: "0%", marker: true });

    const zeroModel = buildTimelineModel(trace({ ended_at: null }), [
      span("zero", { ended_at: start, latency_ms: 0 }),
    ]);
    expect(zeroModel.domainState).toBe("unfinished");
    expect(geometry(zeroModel, "zero")).toMatchObject({ startPercent: "0%", widthPercent: "0%", marker: true });
  });

  it("distinguishes valid, unfinished, malformed, and invalid-order Trace timing", () => {
    const valid = buildTimelineModel(trace(), [span("valid")]);
    expect(valid.domainState).toBe("complete");
    expect(valid.domainEndUs).toBe(parseCanonicalTimestampToUs("2026-08-10T13:00:00.001000Z"));

    const unfinished = buildTimelineModel(trace({ ended_at: null }), [span("observed", { started_at: "2026-08-10T13:00:00.000500Z" })]);
    expect(unfinished.domainState).toBe("unfinished");
    expect(unfinished.domainEndUs).toBe(parseCanonicalTimestampToUs("2026-08-10T13:00:00.001000Z"));

    for (const malformedTrace of [
      trace({ ended_at: "not-a-timestamp" }),
      trace({ ended_at: "2026-08-10T12:59:59.999999Z" }),
      trace({ started_at: "not-a-timestamp" }),
    ]) {
      const malformed = buildTimelineModel(malformedTrace, [span("candidate", { started_at: "2026-08-10T13:00:00.000500Z" })]);
      expect(malformed.domainState).toBe("malformed");
      expect(malformed.domainEndUs).toBeNull();
      expect(malformed.rows[0].timing).toBe("trace-malformed");
      expect(malformed.rows[0].geometry).toBeNull();
    }

    const noUsableSpanTime = buildTimelineModel(trace({ ended_at: null }), [span("bad-span", { started_at: "not-a-timestamp" })]);
    expect(noUsableSpanTime.domainState).toBe("unfinished");
    expect(noUsableSpanTime.domainEndUs).toBe(parseCanonicalTimestampToUs(start));
    expect(noUsableSpanTime.rows[0].timing).toBe("invalid");
    expect(noUsableSpanTime.rows[0].geometry).toBeNull();
  });

  it("keeps malformed timing controlled while topology stays irrelevant to timeline rows", () => {
    const model = buildTimelineModel(trace(), [
      span("self", { parent_span_id: "self" }),
      span("cycle-a", { parent_span_id: "cycle-b" }),
      span("cycle-b", { parent_span_id: "cycle-a" }),
      span("bad-start", { started_at: "not-a-timestamp" }),
      span("bad-end", { ended_at: "2026-08-10T12:59:59.999999Z" }),
    ]);
    expect(model.rows.map((row) => row.span.span_id)).toEqual(["self", "cycle-a", "cycle-b", "bad-start", "bad-end"]);
    expect(model.rows.slice(0, 3).every((row) => row.geometry !== null)).toBe(true);
    expect(model.rows.slice(3).map((row) => row.timing)).toEqual(["invalid", "invalid"]);
    expect(model.rows.slice(3).every((row) => row.geometry === null)).toBe(true);
  });

  it("uses composite span identity and stays linear for large/deep trace inputs", () => {
    const sameSpanId = "shared";
    const a = buildTimelineModel(trace(), [span(sameSpanId)]);
    const b = buildTimelineModel(trace({ trace_id: traceB }), [span(sameSpanId, { trace_id: traceB })]);
    expect(a.rows[0].key).not.toBe(b.rows[0].key);

    const large = Array.from({ length: 2500 }, (_, index) => span(`span-${index}`, {
      parent_span_id: index === 0 ? null : `span-${index - 1}`,
      started_at: "2026-08-10T13:00:00.000000Z",
      ended_at: "2026-08-10T13:00:00.000001Z",
      latency_ms: 0.001,
    }));
    const model = buildTimelineModel(trace(), large);
    expect(model.rows).toHaveLength(2500);
    expect(model.rows.at(-1)?.geometry?.widthPercent).toBe("0.1%");
  });
});

describe("Issue 12 rendering and untrusted data", () => {
  it("renders timing as safe text, marks zero and incomplete spans, and gives visual bars equivalent labels", () => {
    const hostile = "<img src=x onerror=alert(1)><script>alert(1)</script>";
    render(
      <Timeline
        trace={trace()}
        spans={[
          span("zero", { name: hostile, started_at: "2026-08-10T13:00:00.000500Z", ended_at: "2026-08-10T13:00:00.000500Z", latency_ms: 0 }),
          span("running", { ended_at: null, latency_ms: null, status: "unset" }),
          span("invalid", { started_at: "0%;color:red", ended_at: null, latency_ms: null }),
        ]}
      />,
    );

    expect(screen.getByRole("img", { name: /onerror=alert/ })).toBeTruthy();
    expect(screen.getByText("0 ms")).toBeTruthy();
    expect(screen.getByText("Zero-duration observed")).toBeTruthy();
    expect(screen.getByText("In progress; no end observed")).toBeTruthy();
    expect(screen.getByText("Invalid observed timing")).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    for (const element of document.querySelectorAll<HTMLElement>(".timeline-bar, .timeline-point")) {
      expect(element.style.left).toMatch(/^(?:0|[1-9]\d?|100)(?:\.\d+)?%$/);
      if (element.classList.contains("timeline-bar")) {
        expect(element.style.width).toMatch(/^(?:0|[1-9]\d?|100)(?:\.\d+)?%$/);
      }
    }
  });

  it("includes span_id in accessible labels for otherwise identical spans", () => {
    const sharedFacts = {
      name: "same span name",
      type: "tool" as const,
      started_at: "2026-08-10T13:00:00.000100Z",
      ended_at: "2026-08-10T13:00:00.000200Z",
      status: "ok" as const,
      latency_ms: 0.1,
    };
    render(<Timeline trace={trace()} spans={[span("span-one", sharedFacts), span("span-two", sharedFacts)]} />);

    expect(screen.getByRole("img", { name: /id span-one/ })).toBeTruthy();
    expect(screen.getByRole("img", { name: /id span-two/ })).toBeTruthy();
    expect(screen.getByRole("img", { name: /id span-one/ }).getAttribute("aria-label")).not.toBe(
      screen.getByRole("img", { name: /id span-two/ }).getAttribute("aria-label"),
    );
  });

  it("renders a neutral controlled state for malformed Trace bounds", () => {
    render(<Timeline trace={trace({ ended_at: "not-a-timestamp" })} spans={[span("malformed-trace-span")]} />);

    expect(screen.getByText("Trace timing unavailable; observed spans are not positioned.")).toBeTruthy();
    expect(screen.queryByText(/In progress/)).toBeNull();
    expect(document.querySelectorAll(".timeline-bar")).toHaveLength(0);
    expect(screen.getByText("Timing unavailable")).toBeTruthy();
  });
});
