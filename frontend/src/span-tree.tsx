import { useMemo, useState } from "react";
import type { SpanRecord } from "./types";

export type SpanRelationship = "root" | "orphan" | "cycle";

export interface SpanTreeNode {
  key: string;
  span: SpanRecord;
  children: SpanTreeNode[];
  relationship: SpanRelationship;
  depth: number;
  siblingIndex: number;
  siblingCount: number;
}

export interface SpanTree {
  roots: SpanTreeNode[];
}

export interface VisibleSpanNode {
  node: SpanTreeNode;
  depth: number;
}

export function spanIdentityKey(traceId: string, spanId: string): string {
  return `${traceId.length}:${traceId}${spanId.length}:${spanId}`;
}

function cycleNodesFor(
  nodes: SpanTreeNode[],
  candidateParents: Map<SpanTreeNode, SpanTreeNode | null>,
): Set<SpanTreeNode> {
  const completed = new Set<SpanTreeNode>();
  const cycleNodes = new Set<SpanTreeNode>();

  for (const start of nodes) {
    if (completed.has(start)) {
      continue;
    }
    const path: SpanTreeNode[] = [];
    const pathIndex = new Map<SpanTreeNode, number>();
    let current: SpanTreeNode | null = start;
    while (current !== null && !completed.has(current) && !pathIndex.has(current)) {
      pathIndex.set(current, path.length);
      path.push(current);
      current = candidateParents.get(current) ?? null;
    }
    if (current !== null) {
      const cycleStart = pathIndex.get(current);
      if (cycleStart !== undefined) {
        for (let index = cycleStart; index < path.length; index += 1) {
          cycleNodes.add(path[index]);
        }
      }
    }
    for (const node of path) {
      completed.add(node);
    }
  }

  return cycleNodes;
}

function annotateSiblings(nodes: SpanTreeNode[]): void {
  // The tree is already ordered by the API response.  Sibling annotations are
  // assigned while walking each parent group, without sorting or recursion.
  const stack: SpanTreeNode[][] = [nodes];
  while (stack.length > 0) {
    const siblings = stack.pop();
    if (siblings === undefined) {
      continue;
    }
    siblings.forEach((node, index) => {
      node.siblingIndex = index;
      node.siblingCount = siblings.length;
      if (node.children.length > 0) {
        stack.push(node.children);
      }
    });
  }
}

export function buildSpanTree(spans: SpanRecord[]): SpanTree {
  const nodes: SpanTreeNode[] = [];
  const byIdentity = new Map<string, SpanTreeNode>();

  for (const span of spans) {
    const key = spanIdentityKey(span.trace_id, span.span_id);
    if (byIdentity.has(key)) {
      throw new Error("Duplicate span identity");
    }
    const node: SpanTreeNode = {
      key,
      span,
      children: [],
      relationship: "root",
      depth: 0,
      siblingIndex: 0,
      siblingCount: 1,
    };
    nodes.push(node);
    byIdentity.set(key, node);
  }

  const candidateParents = new Map<SpanTreeNode, SpanTreeNode | null>();
  for (const node of nodes) {
    const parentId = node.span.parent_span_id;
    candidateParents.set(
      node,
      parentId === null ? null : byIdentity.get(spanIdentityKey(node.span.trace_id, parentId)) ?? null,
    );
  }

  const cycleNodes = cycleNodesFor(nodes, candidateParents);
  const roots: SpanTreeNode[] = [];
  for (const node of nodes) {
    const candidateParent = candidateParents.get(node) ?? null;
    const parent = cycleNodes.has(node) ? null : candidateParent;
    if (parent === null) {
      if (cycleNodes.has(node)) {
        node.relationship = "cycle";
      } else if (node.span.parent_span_id !== null && candidateParent === null) {
        node.relationship = "orphan";
      }
      roots.push(node);
    } else {
      parent.children.push(node);
    }
  }

  annotateSiblings(roots);
  const stack: Array<{ node: SpanTreeNode; depth: number }> = roots.map((node) => ({ node, depth: 0 }));
  while (stack.length > 0) {
    const current = stack.pop();
    if (current === undefined) {
      continue;
    }
    current.node.depth = current.depth;
    for (let index = current.node.children.length - 1; index >= 0; index -= 1) {
      stack.push({ node: current.node.children[index], depth: current.depth + 1 });
    }
  }

  return { roots };
}

export function flattenSpanTree(tree: SpanTree): VisibleSpanNode[] {
  const visible: VisibleSpanNode[] = [];
  const stack: Array<{ node: SpanTreeNode; depth: number }> = [];
  for (let index = tree.roots.length - 1; index >= 0; index -= 1) {
    stack.push({ node: tree.roots[index], depth: 0 });
  }
  while (stack.length > 0) {
    const current = stack.pop();
    if (current === undefined) {
      continue;
    }
    visible.push({ node: current.node, depth: current.depth });
    for (let index = current.node.children.length - 1; index >= 0; index -= 1) {
      stack.push({ node: current.node.children[index], depth: current.depth + 1 });
    }
  }
  return visible;
}

function statusLabel(status: SpanRecord["status"]): string {
  return { unset: "Unset", ok: "OK", error: "Error" }[status];
}

function formatDuration(value: number | null): string {
  if (value === null) {
    return "Unavailable";
  }
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value)} ms`;
}

function relationshipLabel(node: SpanTreeNode): string | null {
  if (node.relationship === "orphan") {
    return `Orphan - parent ${node.span.parent_span_id} is not in this trace result`;
  }
  if (node.relationship === "cycle") {
    return "Cycle detected - shown at trace root";
  }
  return null;
}

export function SpanTree({ spans }: { spans: SpanRecord[] }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const treeState = useMemo(() => {
    try {
      return { tree: buildSpanTree(spans), error: false };
    } catch {
      return { tree: null, error: true };
    }
  }, [spans]);

  if (treeState.error || treeState.tree === null) {
    return (
      <section className="state-message state-error" role="alert">
        <p>Unable to render the span tree.</p>
      </section>
    );
  }

  if (spans.length === 0) {
    return (
      <section className="state-message empty-state" aria-live="polite">
        <p>This trace contains no spans.</p>
      </section>
    );
  }

  const rows = flattenSpanTree(treeState.tree);
  return (
    <section className="span-tree-section" aria-label="Span hierarchy">
      <div className="span-tree-scroll">
        <div className="span-tree" role="tree" aria-label="Span hierarchy">
          {rows.map(({ node, depth }) => {
            const selected = selectedKey === node.key;
            const relationship = relationshipLabel(node);
            return (
              <div
                className={`span-tree-row span-row-${node.span.status}${node.relationship !== "root" ? " span-row-fallback" : ""}`}
                key={node.key}
                role="treeitem"
                aria-level={depth + 1}
                aria-posinset={node.siblingIndex + 1}
                aria-setsize={node.siblingCount}
                aria-selected={selected}
                style={{ paddingInlineStart: `${Math.min(320, 16 + depth * 24)}px` }}
              >
                <button
                  type="button"
                  className="span-node"
                  aria-label={`Select span ${node.span.name}`}
                  aria-pressed={selected}
                  onClick={() => setSelectedKey(node.key)}
                >
                  <span className="span-node-main">
                    <span className="span-node-name">{node.span.name}</span>
                    <span className="span-node-type">{node.span.type}</span>
                  </span>
                  <span className="span-node-facts">
                    <code>{node.span.span_id}</code>
                    <span className={`status status-${node.span.status}`}>{statusLabel(node.span.status)}</span>
                    <span>{node.span.ended_at === null ? "In progress" : "Completed"}</span>
                    <span>{formatDuration(node.span.latency_ms)}</span>
                  </span>
                  {relationship !== null && <span className="span-node-relationship">{relationship}</span>}
                </button>
              </div>
            );
          })}
        </div>
      </div>
      <p className="selection-note" aria-live="polite">
        {selectedKey === null ? "Select a span to mark it for later inspection." : "Span selected for later inspection."}
      </p>
    </section>
  );
}
