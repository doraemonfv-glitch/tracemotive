import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { buildSpanTree, flattenSpanTree, SpanTree } from "./span-tree";
import type { SpanRecord } from "./types";

const traceA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const traceB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function span(
  trace_id: string,
  span_id: string,
  parent_span_id: string | null = null,
  overrides: Partial<SpanRecord> = {},
): SpanRecord {
  return {
    trace_id,
    span_id,
    parent_span_id,
    type: "agent",
    operation: "agent.run",
    name: span_id,
    started_at: "2026-08-10T13:00:00.000000Z",
    ended_at: "2026-08-10T13:00:01.000000Z",
    status: "ok",
    latency_ms: 1000,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("Span tree", () => {
  it("preserves API order, supports multiple roots, and scopes parent identity to the trace", () => {
    const tree = buildSpanTree([
      span(traceA, "shared"),
      span(traceA, "child", "shared"),
      span(traceB, "shared"),
      span(traceB, "child", "shared"),
      span(traceA, "orphan", "missing"),
    ]);

    expect(tree.roots.map((node) => node.span.span_id)).toEqual(["shared", "shared", "orphan"]);
    expect(tree.roots[0].children.map((node) => node.span.span_id)).toEqual(["child"]);
    expect(tree.roots[1].children.map((node) => node.span.span_id)).toEqual(["child"]);
    expect(tree.roots[2].relationship).toBe("orphan");
    expect(flattenSpanTree(tree).map(({ node }) => node.span.span_id)).toEqual([
      "shared", "child", "shared", "child", "orphan",
    ]);
  });

  it("puts self-parent and cyclic spans in controlled root fallback positions", () => {
    const tree = buildSpanTree([
      span(traceA, "self", "self"),
      span(traceA, "a", "b"),
      span(traceA, "b", "a"),
      span(traceA, "descendant", "a"),
    ]);

    expect(tree.roots.map((node) => [node.span.span_id, node.relationship])).toEqual([
      ["self", "cycle"],
      ["a", "cycle"],
      ["b", "cycle"],
    ]);
    expect(tree.roots[1].children.map((node) => node.span.span_id)).toEqual(["descendant"]);
    expect(flattenSpanTree(tree)).toHaveLength(4);
  });

  it("constructs and flattens a deep chain iteratively", () => {
    const spans = Array.from({ length: 2500 }, (_, index) =>
      span(traceA, `span-${index}`, index === 0 ? null : `span-${index - 1}`),
    );
    const tree = buildSpanTree(spans);
    const rows = flattenSpanTree(tree);

    expect(tree.roots).toHaveLength(1);
    expect(rows).toHaveLength(2500);
    expect(rows.at(-1)?.depth).toBe(2499);
  });

  it("keeps hostile names inert and exposes only scan-level row information", () => {
    const hostile = "<script>alert(1)</script><img src=x onerror=alert(1)>";
    render(<SpanTree spans={[span(traceA, "hostile", null, { name: hostile, status: "error" })]} />);

    expect(screen.getByText(hostile)).toBeTruthy();
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText("Error")).toBeTruthy();
    expect(screen.queryByText(/input|output|metadata|attributes/i)).toBeNull();
  });
});
