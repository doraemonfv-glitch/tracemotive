import { useCallback, useEffect, useRef, useState } from "react";
import { fetchSpanDetail, fetchSpanList, fetchTraceDetail, QueryApiError } from "./api";
import { SpanInspector, type SpanInspectorState } from "./span-inspector";
import { spanIdentityKey, SpanTree } from "./span-tree";
import { Timeline } from "./timeline";
import type { SpanIdentity, SpanListResponse, SpanRecord, TraceDetailResponse, TraceStatus } from "./types";

type DetailState =
  | { kind: "loading" }
  | { kind: "loaded"; response: TraceDetailResponse }
  | { kind: "not-found" }
  | { kind: "error" };

type SpanState =
  | { kind: "loading" }
  | { kind: "loaded"; response: SpanListResponse }
  | { kind: "not-found" }
  | { kind: "error" };

function isNotFound(error: unknown): boolean {
  return error instanceof QueryApiError && error.status === 404;
}

function formatExactInteger(value: bigint): string {
  return value.toLocaleString("en-US");
}

function formatNullableExactInteger(value: bigint | null): string {
  return value === null ? "Unknown" : formatExactInteger(value);
}

function formatDuration(value: number | null): string {
  return value === null ? "Unavailable" : `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value)} ms`;
}

function formatEndTime(value: string | null): string {
  return value === null ? "Not ended" : value;
}

function statusLabel(status: TraceStatus): string {
  return { unset: "Unset", ok: "OK", error: "Error" }[status];
}

function sameIdentity(left: SpanIdentity, right: SpanIdentity): boolean {
  return left.trace_id === right.trace_id && left.span_id === right.span_id;
}

function inspectorStateForRender(
  state: SpanInspectorState,
  selectedSpan: SpanIdentity | null,
  traceId: string,
): SpanInspectorState {
  if (selectedSpan === null || selectedSpan.trace_id !== traceId) {
    return { kind: "no-selection" };
  }
  if (state.kind === "loaded" || state.kind === "loading" || state.kind === "not-found" || state.kind === "error") {
    return sameIdentity(state.identity, selectedSpan) ? state : { kind: "loading", identity: selectedSpan };
  }
  return { kind: "loading", identity: selectedSpan };
}

export function TraceDetail({ traceId, onBack }: { traceId: string; onBack: () => void }) {
  const [detail, setDetail] = useState<DetailState>({ kind: "loading" });
  const [spans, setSpans] = useState<SpanState>({ kind: "loading" });
  const [selectedSpan, setSelectedSpan] = useState<SpanIdentity | null>(null);
  const [inspector, setInspector] = useState<SpanInspectorState>({ kind: "no-selection" });
  const requestIdentity = useRef(0);
  const inspectorRequestIdentity = useRef(0);

  const selectSpan = useCallback((span: SpanRecord) => {
    const identity = { trace_id: span.trace_id, span_id: span.span_id };
    inspectorRequestIdentity.current += 1;
    setSelectedSpan(identity);
    setInspector({ kind: "loading", identity });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const currentRequest = requestIdentity.current + 1;
    requestIdentity.current = currentRequest;
    setSelectedSpan(null);
    setInspector({ kind: "no-selection" });
    setDetail({ kind: "loading" });
    setSpans({ kind: "loading" });

    void fetchTraceDetail(traceId, controller.signal).then(
      (response) => {
        if (requestIdentity.current === currentRequest) {
          setDetail({ kind: "loaded", response });
        }
      },
      (error: unknown) => {
        if (!controller.signal.aborted && requestIdentity.current === currentRequest) {
          setDetail({ kind: isNotFound(error) ? "not-found" : "error" });
        }
      },
    );

    void fetchSpanList(traceId, controller.signal).then(
      (response) => {
        if (requestIdentity.current === currentRequest) {
          setSpans({ kind: "loaded", response });
        }
      },
      (error: unknown) => {
        if (!controller.signal.aborted && requestIdentity.current === currentRequest) {
          setSpans({ kind: isNotFound(error) ? "not-found" : "error" });
        }
      },
    );

    return () => controller.abort();
  }, [traceId]);

  useEffect(() => {
    const currentRequest = inspectorRequestIdentity.current + 1;
    inspectorRequestIdentity.current = currentRequest;
    const selected = selectedSpan;
    if (selected === null || selected.trace_id !== traceId) {
      setInspector({ kind: "no-selection" });
      return;
    }

    const controller = new AbortController();
    setInspector({ kind: "loading", identity: selected });
    void fetchSpanDetail(selected.trace_id, selected.span_id, controller.signal).then(
      (response) => {
        if (inspectorRequestIdentity.current === currentRequest) {
          setInspector({ kind: "loaded", identity: selected, span: response.span });
        }
      },
      (error: unknown) => {
        if (!controller.signal.aborted && inspectorRequestIdentity.current === currentRequest) {
          setInspector({ kind: isNotFound(error) ? "not-found" : "error", identity: selected });
        }
      },
    );

    return () => controller.abort();
  }, [selectedSpan, traceId]);

  const visibleSelectedSpan = selectedSpan?.trace_id === traceId ? selectedSpan : null;
  const visibleInspector = inspectorStateForRender(inspector, selectedSpan, traceId);

  return (
    <main className="trace-detail-page">
      <header className="detail-page-header">
        <button type="button" className="back-button" onClick={onBack}>
          Back to traces
        </button>
        <p className="eyebrow">TraceMotive / Trace detail</p>
        <h1>{detail.kind === "loaded" ? detail.response.trace.name : "Trace detail"}</h1>
        <code className="detail-trace-id">{traceId}</code>
      </header>

      {detail.kind === "loading" && (
        <section className="state-message" aria-live="polite" aria-busy="true">
          Loading trace detail...
        </section>
      )}

      {detail.kind === "not-found" && (
        <section className="state-message empty-state" aria-live="polite">
          <p>Trace not found.</p>
        </section>
      )}

      {detail.kind === "error" && (
        <section className="state-message state-error" role="alert">
          <p>Unable to load trace detail.</p>
        </section>
      )}

      {detail.kind === "loading" && (
        <section className="span-hierarchy-panel" aria-labelledby="span-hierarchy-loading-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Observed parent relationships</p>
              <h2 id="span-hierarchy-loading-heading">Span hierarchy</h2>
            </div>
          </div>
          <section className="state-message" aria-live="polite" aria-busy="true">
            Loading span tree...
          </section>
        </section>
      )}

      {detail.kind === "loaded" && <TraceSummary response={detail.response} />}

      {detail.kind === "loaded" && (
        <section className="span-hierarchy-panel" aria-labelledby="span-hierarchy-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Observed parent relationships</p>
              <h2 id="span-hierarchy-heading">Span hierarchy</h2>
            </div>
            <span className="section-count">{formatExactInteger(detail.response.stats.span_count)} spans</span>
          </div>

          {spans.kind === "loading" && (
            <section className="state-message" aria-live="polite" aria-busy="true">
              Loading span tree...
            </section>
          )}
          {spans.kind === "not-found" && (
            <section className="state-message empty-state" aria-live="polite">
              <p>The span result is no longer available for this trace.</p>
            </section>
          )}
          {spans.kind === "error" && (
            <section className="state-message state-error" role="alert">
              <p>Unable to load the span tree.</p>
            </section>
          )}
          {spans.kind === "loaded" && (
            <SpanTree
              spans={spans.response.items}
              selectedSpanKey={visibleSelectedSpan === null ? null : spanIdentityKey(visibleSelectedSpan.trace_id, visibleSelectedSpan.span_id)}
              onSelectSpan={selectSpan}
            />
          )}
        </section>
      )}

      {detail.kind === "loaded" && <SpanInspector state={visibleInspector} />}

      {detail.kind === "loaded" && (
        <section className="timeline-panel" aria-labelledby="timeline-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Observed execution timing</p>
              <h2 id="timeline-heading">Timeline</h2>
            </div>
          </div>

          {spans.kind === "loading" && (
            <section className="state-message" aria-live="polite" aria-busy="true">
              Loading timeline...
            </section>
          )}
          {spans.kind === "not-found" && (
            <section className="state-message empty-state" aria-live="polite">
              <p>The timeline is no longer available for this trace.</p>
            </section>
          )}
          {spans.kind === "error" && (
            <section className="state-message state-error" role="alert">
              <p>Unable to load the timeline.</p>
            </section>
          )}
          {spans.kind === "loaded" && <Timeline trace={detail.response.trace} spans={spans.response.items} />}
        </section>
      )}
    </main>
  );
}

function TraceSummary({ response }: { response: TraceDetailResponse }) {
  const { trace, stats } = response;
  return (
    <section className="trace-summary" aria-label="Trace summary">
      <div className="trace-summary-heading">
        <div>
          <p className="eyebrow">Trace summary</p>
          <span className={`status status-${trace.status}`}>{statusLabel(trace.status)}</span>
        </div>
        <div className="trace-summary-times">
          <span><strong>Started</strong> {trace.started_at}</span>
          <span><strong>Ended</strong> {formatEndTime(trace.ended_at)}</span>
        </div>
      </div>
      <dl className="metric-grid">
        <Metric label="Duration" value={formatDuration(stats.latency_ms)} />
        <Metric label="Spans" value={formatExactInteger(stats.span_count)} />
        <Metric label="Errors" value={formatExactInteger(stats.error_count)} />
        <Metric label="LLM calls" value={formatExactInteger(stats.llm_call_count)} />
        <Metric label="Input tokens" value={formatNullableExactInteger(stats.input_tokens)} />
        <Metric label="Output tokens" value={formatNullableExactInteger(stats.output_tokens)} />
      </dl>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
