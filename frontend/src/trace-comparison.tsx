import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchComparisonDetail, fetchInsightComparison, QueryApiError } from "./api";
import { ComparisonInsight } from "./comparison-insight";
import type {
  ComparisonFieldRecord,
  ComparisonSpanRecord,
  ComparisonTraceField,
  ComparisonTraceView,
  TraceComparisonResponse,
  TraceInsightResponse,
  TraceStats,
} from "./types";

type ComparisonViewState =
  | { kind: "loading" }
  | { kind: "loaded"; response: TraceInsightResponse }
  | { kind: "same-trace" }
  | { kind: "not-found" }
  | { kind: "invalid" }
  | { kind: "too-large" }
  | { kind: "error" };

type DetailViewState =
  | { kind: "closed" }
  | { kind: "loading" }
  | { kind: "loaded"; response: TraceComparisonResponse }
  | { kind: "not-found" }
  | { kind: "invalid" }
  | { kind: "too-large" }
  | { kind: "error" };

const durationFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 });

function formatInteger(value: bigint): string {
  return value.toLocaleString("en-US");
}

function formatNullableInteger(value: bigint | null): string {
  return value === null ? "Unknown" : formatInteger(value);
}

function formatDuration(value: number | null): string {
  return value === null ? "Unavailable" : `${durationFormatter.format(value)} ms`;
}

function statusLabel(status: ComparisonTraceView["trace"]["status"]): string {
  return { unset: "Unset", ok: "OK", error: "Error" }[status];
}

function stateLabel(state: "same" | "different" | "left_only" | "right_only" | "unknown"): string {
  return { same: "Unchanged", different: "Changed", left_only: "Left only", right_only: "Right only", unknown: "Unavailable / unknown" }[state];
}

function formatValue(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "bigint") {
    return value.toLocaleString("en-US");
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value, (_key, nested) => typeof nested === "bigint" ? nested.toString() : nested, 2) ?? "Unavailable";
  } catch {
    return "Unavailable";
  }
}

function reasonLabel(reason: string | null): string | null {
  if (reason === null) {
    return null;
  }
  return reason.replaceAll("_", " ");
}

function fieldLabel(path: string): string {
  const labels: Record<string, string> = {
    status: "Status",
    "error.type": "Error type",
    "error.message": "Error message",
    latency_ms: "Duration / latency",
    "details.request_model": "Model",
    "details.response_model": "Response model",
    "details.request_parameters": "Request parameters",
    "details.usage.input_tokens": "Input tokens",
    "details.usage.output_tokens": "Output tokens",
    "details.usage.reasoning_output_tokens": "Reasoning output tokens",
    input: "Tool / span input",
    output: "Tool / span output",
    "capture.input": "Input capture state",
    "capture.output": "Output capture state",
  };
  if (labels[path] !== undefined) {
    return labels[path];
  }
  if (path === "") {
    return "Span presence";
  }
  return path.replaceAll(".", " / ").replaceAll("/", " / ");
}

function traceFieldMap(fields: ComparisonTraceField[]): Map<string, ComparisonTraceField> {
  return new Map(fields.map((field) => [field.path, field]));
}

function traceMetricState(fields: Map<string, ComparisonTraceField>, path: string): "same" | "different" | "unknown" {
  return fields.get(path)?.state ?? "unknown";
}

function traceMetricValue(stats: TraceStats, path: string): string {
  switch (path) {
    case "latency_ms":
      return formatDuration(stats.latency_ms);
    case "span_count":
      return formatInteger(stats.span_count);
    case "error_count":
      return formatInteger(stats.error_count);
    case "input_tokens":
      return formatNullableInteger(stats.input_tokens);
    case "output_tokens":
      return formatNullableInteger(stats.output_tokens);
    default:
      return "Unknown";
  }
}

function TraceMetric({
  label,
  path,
  left,
  right,
  state,
}: {
  label: string;
  path: string;
  left: string;
  right: string;
  state: "same" | "different" | "unknown";
}) {
  return (
    <div className={`comparison-metric comparison-metric-${state}`} data-testid={`comparison-metric-${path}`}>
      <dt>{label}</dt>
      <dd className="comparison-metric-values">
        <span><strong>Left</strong> {left}</span>
        <span><strong>Right</strong> {right}</span>
      </dd>
      <span className="comparison-state-label">{stateLabel(state)}</span>
    </div>
  );
}

function TraceSummaryComparison({ response }: { response: TraceComparisonResponse }) {
  const fields = traceFieldMap(response.summary.trace_fields);
  const metrics = [
    ["Duration", "latency_ms"],
    ["Spans", "span_count"],
    ["Errors", "error_count"],
    ["Input tokens", "input_tokens"],
    ["Output tokens", "output_tokens"],
  ] as const;
  return (
    <section className="comparison-summary" aria-label="Comparison trace summary">
      <div className="comparison-summary-heading">
        <div>
          <p className="eyebrow">Observed execution summary</p>
          <h2>Trace-level differences</h2>
          <p className="comparison-note">These are observed values and differences. They do not identify a cause or assign responsibility.</p>
        </div>
        <div className="comparison-alignment-counts" aria-label="Alignment counts">
          <span>{formatInteger(response.summary.alignment.matched_spans)} exact</span>
          <span>{formatInteger(response.summary.alignment.left_only_spans)} left-only</span>
          <span>{formatInteger(response.summary.alignment.right_only_spans)} right-only</span>
          <span>{formatInteger(response.summary.alignment.ambiguous_groups)} ambiguous</span>
          <span>{formatInteger(response.summary.alignment.unavailable_spans)} unavailable</span>
        </div>
      </div>
      <div className="comparison-trace-headings">
        <TraceHeading label="Left trace" trace={response.left_trace} />
        <TraceHeading label="Right trace" trace={response.right_trace} />
      </div>
      <dl className="comparison-metric-grid">
        <TraceMetric
          label="Status"
          path="status"
          left={statusLabel(response.left_trace.trace.status)}
          right={statusLabel(response.right_trace.trace.status)}
          state={traceMetricState(fields, "status")}
        />
        {metrics.map(([label, path]) => (
          <TraceMetric
            key={path}
            label={label}
            path={path}
            left={traceMetricValue(response.left_trace.stats, path)}
            right={traceMetricValue(response.right_trace.stats, path)}
            state={traceMetricState(fields, path)}
          />
        ))}
      </dl>
      {response.summary.trace_fields.some((field) => field.state !== "same") && (
        <div className="comparison-trace-fields" aria-label="Trace field differences">
          {response.summary.trace_fields.filter((field) => field.state !== "same").map((field) => (
            <FieldDifference key={field.path} field={field} />
          ))}
        </div>
      )}
    </section>
  );
}

function TraceHeading({ label, trace }: { label: string; trace: ComparisonTraceView }) {
  return (
    <div className="comparison-trace-heading">
      <span className="comparison-side-label">{label}</span>
      <strong>{trace.trace.name}</strong>
      <code>{trace.trace.trace_id}</code>
    </div>
  );
}

function spanTitle(item: ComparisonSpanRecord): string {
  const segment = item.semantic_path[item.semantic_path.length - 1];
  if (segment === undefined) {
    return "Span result";
  }
  return `${segment.type} / ${segment.name}`;
}

function spanPath(item: ComparisonSpanRecord): string {
  return item.semantic_path.map((segment) => `${segment.type}:${segment.name} [${segment.ordinal}]`).join(" → ");
}

function referenceLabel(ref: ComparisonSpanRecord["left"]): string {
  return ref === null ? "Not observed" : `${ref.trace_id} / ${ref.span_id}`;
}

function isAttentionRequired(item: ComparisonSpanRecord): boolean {
  return item.alignment !== "exact_match" || item.differences.length > 0 || item.uncertainties.length > 0;
}

function ComparisonSpanCard({ item }: { item: ComparisonSpanRecord }) {
  // The API's root side-only record contains the full Canonical snapshot. The
  // comparison UI reports the side-only classification without expanding that
  // raw snapshot into the primary result list.
  const fields = [...item.differences, ...item.uncertainties].filter((field) => field.path !== "");
  return (
    <article className={`comparison-result comparison-result-${item.alignment}`}>
      <header className="comparison-result-header">
        <div>
          <span className={`comparison-alignment comparison-alignment-${item.alignment}`}>{item.alignment}</span>
          <h3>{spanTitle(item)}</h3>
          <p className="comparison-semantic-path">{spanPath(item)}</p>
        </div>
        <div className="comparison-span-refs">
          <span><strong>Left</strong> <code>{referenceLabel(item.left)}</code></span>
          <span><strong>Right</strong> <code>{referenceLabel(item.right)}</code></span>
        </div>
      </header>
      {item.alignment === "exact_match" && fields.length === 0 && (
        <p className="comparison-unchanged">No observed field changes.</p>
      )}
      {item.alignment !== "exact_match" && (
        <p className="comparison-side-only-message">This span was observed on {item.alignment === "left_only" ? "the left" : "the right"} only. No counterpart was inferred.</p>
      )}
      {fields.length > 0 && (
        <div className="comparison-field-list" aria-label={`${spanTitle(item)} field observations`}>
          {fields.map((field, index) => <FieldDifference key={`${field.path}-${index}`} field={field} />)}
        </div>
      )}
    </article>
  );
}

function FieldDifference({ field }: { field: ComparisonFieldRecord }) {
  const caution = field.reason !== null;
  const highlight = field.state !== "unknown" && field.state !== "same" && !caution;
  const label = field.state === "unknown" ? "Unknown" : field.state === "same" ? "Unchanged" : stateLabel(field.state);
  return (
    <div className={`comparison-field ${highlight ? "comparison-field-highlight" : ""} ${caution ? "comparison-field-caution" : ""}`}>
      <div className="comparison-field-heading">
        <strong>{fieldLabel(field.path)}</strong>
        <span className="comparison-field-state">{label}</span>
        {reasonLabel(field.reason) !== null && <span className="comparison-field-reason">{reasonLabel(field.reason)}</span>}
      </div>
      <div className="comparison-field-sides">
        <div className="comparison-field-side">
          <span className="comparison-side-label">Left</span>
          <pre>{field.state === "unknown" && field.left === null ? "Unknown" : formatValue(field.left)}</pre>
        </div>
        <div className="comparison-field-side">
          <span className="comparison-side-label">Right</span>
          <pre>{field.state === "unknown" && field.right === null ? "Unknown" : formatValue(field.right)}</pre>
        </div>
      </div>
    </div>
  );
}

function AmbiguousGroupCard({ group }: { group: TraceComparisonResponse["ambiguous_groups"][number] }) {
  return (
    <article className="comparison-result comparison-result-ambiguous_group">
      <header className="comparison-result-header">
        <div>
          <span className="comparison-alignment comparison-alignment-ambiguous_group">ambiguous_group</span>
          <h3>{group.group_signature.type} / {group.group_signature.name}</h3>
          <p className="comparison-semantic-path">{group.parent_path.map((segment) => `${segment.type}:${segment.name} [${segment.ordinal}]`).join(" → ") || "Trace root"}</p>
        </div>
      </header>
      <div className="ambiguous-group-grid">
        <Count label="Left count" value={group.left_count} />
        <Count label="Right count" value={group.right_count} />
        <Count label="Resolved members" value={BigInt(group.resolved_members.length)} />
        <Count label="Ambiguous left" value={BigInt(group.ambiguous_members.left.length)} />
        <Count label="Ambiguous right" value={BigInt(group.ambiguous_members.right.length)} />
        <Count label="Left-only count" value={group.left_only_count} />
        <Count label="Right-only count" value={group.right_only_count} />
      </div>
      <p className="comparison-ambiguity-note">Repeated members were not paired because ordinal position alone cannot establish their identity.</p>
    </article>
  );
}

function Count({ label, value }: { label: string; value: bigint | null }) {
  return <div className="ambiguous-group-count"><span>{label}</span><strong>{value === null ? "Unknown" : formatInteger(value)}</strong></div>;
}

function UnavailableCard({ item }: { item: TraceComparisonResponse["unavailable_spans"][number] }) {
  return (
    <article className="comparison-result comparison-result-unavailable">
      <header className="comparison-result-header">
        <div>
          <span className="comparison-alignment comparison-alignment-unavailable">unavailable</span>
          <h3>Span subtree unavailable</h3>
          <p className="comparison-semantic-path">{item.side} side · {item.reason.replaceAll("_", " ")}</p>
        </div>
        <code>{item.span.trace_id} / {item.span.span_id}</code>
      </header>
      <p className="comparison-unavailable-note">Only the affected subtree is unavailable. Unrelated comparison results remain available.</p>
      <p className="comparison-unavailable-reason"><strong>Reason:</strong> {item.reason}</p>
    </article>
  );
}

function ComparisonResults({ response }: { response: TraceComparisonResponse }) {
  const [changedOnly, setChangedOnly] = useState(false);
  const visibleSpans = useMemo(
    () => changedOnly ? response.spans.filter(isAttentionRequired) : response.spans,
    [changedOnly, response.spans],
  );
  const hasResults = visibleSpans.length > 0 || response.ambiguous_groups.length > 0 || response.unavailable_spans.length > 0;

  return (
    <section className="comparison-results" aria-label="Comparison results">
      <div className="section-heading comparison-results-heading">
        <div>
          <p className="eyebrow">Structural observed differences</p>
          <h2>Span results</h2>
        </div>
        <label className="changed-only-toggle">
          <input type="checkbox" checked={changedOnly} onChange={(event) => setChangedOnly(event.target.checked)} />
          Changed only
        </label>
      </div>
      <p className="comparison-filter-note">All results are shown by default. Ambiguous groups and unavailable subtrees remain visible when this filter is active.</p>
      {!hasResults && (
        <section className="state-message empty-state">
          <p>{changedOnly ? "No changed span results. Unchanged exact matches are hidden." : "No span comparison records were returned."}</p>
        </section>
      )}
      {visibleSpans.map((item, index) => <ComparisonSpanCard key={`${item.alignment}-${index}`} item={item} />)}
      {response.ambiguous_groups.map((group, index) => <AmbiguousGroupCard key={`ambiguous-${index}`} group={group} />)}
      {response.unavailable_spans.map((item, index) => <UnavailableCard key={`unavailable-${item.side}-${item.span.span_id}-${index}`} item={item} />)}
    </section>
  );
}

function ComparisonDetail({ state }: { state: DetailViewState }) {
  if (state.kind === "closed") {
    return null;
  }
  if (state.kind === "loading") {
    return <section className="state-message comparison-detail-loading" aria-live="polite" aria-busy="true">Loading detailed comparison...</section>;
  }
  if (state.kind !== "loaded") {
    return <section className="comparison-detail-error"><ComparisonError kind={state.kind} /></section>;
  }
  return (
    <section className="comparison-detail-section" aria-label="Detailed structural comparison">
      <TraceSummaryComparison response={state.response} />
      <ComparisonResults response={state.response} />
    </section>
  );
}

type ComparisonErrorKind = "same-trace" | "not-found" | "invalid" | "too-large" | "error";

function ComparisonError({ kind }: { kind: ComparisonErrorKind }) {
  const messages: Record<typeof kind, string> = {
    "same-trace": "Choose two different traces to compare.",
    "not-found": "One or both selected traces were not found.",
    invalid: "The comparison request was invalid.",
    "too-large": "This comparison exceeds TraceMotive's supported analysis/response limit.",
    error: "Unable to load the comparison.",
  };
  return <section className={`state-message ${kind === "error" || kind === "too-large" ? "state-error" : ""}`} role={kind === "error" || kind === "too-large" ? "alert" : undefined}><p>{messages[kind]}</p></section>;
}

export function TraceComparison({ leftTraceId, rightTraceId, onBack }: { leftTraceId: string; rightTraceId: string; onBack: () => void }) {
  const [view, setView] = useState<ComparisonViewState>(() => leftTraceId === rightTraceId ? { kind: "same-trace" } : { kind: "loading" });
  const [detail, setDetail] = useState<DetailViewState>({ kind: "closed" });
  const requestIdentity = useRef(0);
  const detailRequestIdentity = useRef(0);
  const detailController = useRef<AbortController | null>(null);

  useEffect(() => {
    const currentRequest = requestIdentity.current + 1;
    requestIdentity.current = currentRequest;
    detailRequestIdentity.current += 1;
    detailController.current?.abort();
    detailController.current = null;
    setDetail({ kind: "closed" });
    if (leftTraceId === rightTraceId) {
      setView({ kind: "same-trace" });
      return;
    }
    const controller = new AbortController();
    setView({ kind: "loading" });
    void fetchInsightComparison(leftTraceId, rightTraceId, controller.signal).then(
      (response) => {
        if (requestIdentity.current === currentRequest) {
          setView({ kind: "loaded", response });
        }
      },
      (error: unknown) => {
        if (controller.signal.aborted || requestIdentity.current !== currentRequest) {
          return;
        }
        if (error instanceof QueryApiError) {
          setView({ kind: error.status === 404 ? "not-found" : error.status === 400 ? "invalid" : error.status === 413 ? "too-large" : "error" });
          return;
        }
        setView({ kind: "error" });
      },
    );
    return () => controller.abort();
  }, [leftTraceId, rightTraceId]);

  const openDetails = useCallback(() => {
    if (view.kind !== "loaded") {
      return;
    }
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    const currentRequest = detailRequestIdentity.current + 1;
    detailRequestIdentity.current = currentRequest;
    setDetail({ kind: "loading" });
    void fetchComparisonDetail(view.response.detail_endpoint, leftTraceId, rightTraceId, controller.signal).then(
      (response) => {
        if (detailRequestIdentity.current === currentRequest) {
          setDetail({ kind: "loaded", response });
        }
      },
      (error: unknown) => {
        if (controller.signal.aborted || detailRequestIdentity.current !== currentRequest) {
          return;
        }
        if (error instanceof QueryApiError) {
          setDetail({ kind: error.status === 404 ? "not-found" : error.status === 400 ? "invalid" : error.status === 413 ? "too-large" : "error" });
          return;
        }
        setDetail({ kind: "error" });
      },
    );
  }, [leftTraceId, rightTraceId, view]);

  return (
    <main className="comparison-page">
      <header className="comparison-page-header">
        <button type="button" className="back-button" onClick={onBack}>Back to traces</button>
        <p className="eyebrow">TraceMotive / Trace comparison</p>
        <h1>What changed between these observed executions?</h1>
        <p className="comparison-note">Structural observed differences only. TraceMotive does not infer causes from this comparison.</p>
      </header>
      {view.kind === "loading" && <section className="state-message" aria-live="polite" aria-busy="true">Loading comparison...</section>}
      {view.kind !== "loading" && view.kind !== "loaded" && <ComparisonError kind={view.kind} />}
      {view.kind === "loaded" && (
        <>
          <ComparisonInsight response={view.response} onOpenDetails={openDetails} detailsState={detail.kind} />
          <ComparisonDetail state={detail} />
        </>
      )}
    </main>
  );
}
