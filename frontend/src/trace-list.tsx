import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchTraceList } from "./api";
import type { TraceListFilters, TraceListResponse, TraceStatus, TraceSummary } from "./types";

const PAGE_SIZE = 50;
const PAGE_INCREMENT = BigInt(PAGE_SIZE);

type ViewState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "loaded"; response: TraceListResponse };

const durationFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 3,
});

function formatExactInteger(value: bigint): string {
  return value.toLocaleString("en-US");
}

function formatNullableExactInteger(value: bigint | null): string {
  return value === null ? "Unknown" : formatExactInteger(value);
}

function formatDuration(value: number | null): string {
  return value === null ? "Unavailable" : `${durationFormatter.format(value)} ms`;
}

function formatEndTime(value: string | null): string {
  return value === null ? "Not ended" : value;
}

function statusLabel(status: TraceStatus): string {
  return { unset: "Unset", ok: "OK", error: "Error" }[status];
}

function TraceRow({ trace, onOpenTrace }: { trace: TraceSummary; onOpenTrace: (traceId: string) => void }) {
  return (
    <tr>
      <td className="trace-identity">
        <button type="button" className="trace-link" onClick={() => onOpenTrace(trace.trace_id)}>
          <span className="trace-name">{trace.name}</span>
          <code>{trace.trace_id}</code>
        </button>
      </td>
      <td>
        <span className={`status status-${trace.status}`}>{statusLabel(trace.status)}</span>
      </td>
      <td>{trace.started_at}</td>
      <td>{formatEndTime(trace.ended_at)}</td>
      <td>{formatDuration(trace.latency_ms)}</td>
      <td>{formatExactInteger(trace.span_count)}</td>
      <td>{formatExactInteger(trace.error_count)}</td>
      <td>{formatExactInteger(trace.llm_call_count)}</td>
      <td>{formatNullableExactInteger(trace.input_tokens)}</td>
      <td>{formatNullableExactInteger(trace.output_tokens)}</td>
    </tr>
  );
}

export function TraceList({ onOpenTrace = () => undefined }: { onOpenTrace?: (traceId: string) => void } = {}) {
  const [status, setStatus] = useState<TraceStatus | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [offset, setOffset] = useState(0n);
  const [view, setView] = useState<ViewState>({ kind: "loading" });
  const requestIdentity = useRef(0);

  const filters = useMemo<TraceListFilters>(
    () => ({ limit: PAGE_SIZE, offset, status, name }),
    [name, offset, status],
  );

  useEffect(() => {
    const controller = new AbortController();
    const currentRequest = requestIdentity.current + 1;
    requestIdentity.current = currentRequest;
    setView({ kind: "loading" });

    void fetchTraceList(filters, controller.signal).then(
      (response) => {
        if (requestIdentity.current === currentRequest) {
          // A concurrent deletion can make the current server-side offset
          // out of range. Reset to the last valid Query API page rather than
          // leaving the user on a stale page such as "Page 2 of 1".
          if (response.items.length === 0 && response.total > 0n && response.offset >= response.total) {
            const pageSize = BigInt(response.limit);
            const lastOffset = ((response.total - 1n) / pageSize) * pageSize;
            if (lastOffset !== offset) {
              setOffset(lastOffset);
              return;
            }
          }
          setView({ kind: "loaded", response });
        }
      },
      () => {
        if (!controller.signal.aborted && requestIdentity.current === currentRequest) {
          setView({ kind: "error" });
        }
      },
    );

    return () => controller.abort();
  }, [filters]);

  const updateStatus = useCallback((nextStatus: string) => {
    setStatus(nextStatus === "" ? null : (nextStatus as TraceStatus));
    setOffset(0n);
  }, []);

  const updateName = useCallback((nextName: string) => {
    setName(nextName);
    setOffset(0n);
  }, []);

  const filtersAreActive = status !== null || (name !== null && name !== "");

  return (
    <main className="trace-list-page">
      <header className="page-header">
        <p className="eyebrow">AgentLens / Local trace observer</p>
        <h1>Traces</h1>
      </header>

      <section className="filters" aria-label="Trace filters">
        <label>
          Status
          <select
            aria-label="Status filter"
            value={status ?? ""}
            onChange={(event) => updateStatus(event.target.value)}
          >
            <option value="">All statuses</option>
            <option value="unset">Unset</option>
            <option value="ok">OK</option>
            <option value="error">Error</option>
          </select>
        </label>
        <label className="name-filter">
          Trace name
          <input
            aria-label="Trace name filter"
            type="search"
            value={name ?? ""}
            onChange={(event) => updateName(event.target.value)}
            placeholder="Filter by name"
          />
        </label>
      </section>

      {view.kind === "loading" && (
        <section className="state-message" aria-live="polite" aria-busy="true">
          Loading trace list...
        </section>
      )}

      {view.kind === "error" && (
        <section className="state-message state-error" role="alert">
          <p>Unable to load traces.</p>
        </section>
      )}

      {view.kind === "loaded" && (
        <TraceResults
          response={view.response}
          filtersAreActive={filtersAreActive}
          onOpenTrace={onOpenTrace}
          onPrevious={() => setOffset((current) => current > PAGE_INCREMENT ? current - PAGE_INCREMENT : 0n)}
          onNext={() => setOffset((current) => current + PAGE_INCREMENT)}
        />
      )}
    </main>
  );
}

function TraceResults({
  response,
  filtersAreActive,
  onOpenTrace,
  onPrevious,
  onNext,
}: {
  response: TraceListResponse;
  filtersAreActive: boolean;
  onOpenTrace: (traceId: string) => void;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const { items, limit, offset, total } = response;
  const pageSize = BigInt(limit);
  const currentPage = offset / pageSize + 1n;
  const pageCount = total === 0n ? 1n : (total + pageSize - 1n) / pageSize;
  const canGoPrevious = offset > 0n;
  const canGoNext = offset + BigInt(items.length) < total;

  if (items.length === 0) {
    const message =
      total > 0n
        ? "No traces are available on this page."
        : filtersAreActive
          ? "No traces match the current filters."
          : "No traces recorded yet.";
    return (
      <section className="state-message empty-state" aria-live="polite">
        <p>{message}</p>
        <Pagination
          currentPage={currentPage}
          pageCount={pageCount}
          canGoPrevious={canGoPrevious}
          canGoNext={canGoNext}
          onPrevious={onPrevious}
          onNext={onNext}
        />
      </section>
    );
  }

  return (
    <section aria-label="Trace results">
      <div className="table-shell">
        <table className="trace-table">
          <thead>
            <tr>
              <th scope="col">Trace</th>
              <th scope="col">Status</th>
              <th scope="col">Start (UTC)</th>
              <th scope="col">End (UTC)</th>
              <th scope="col">Duration</th>
              <th scope="col">Spans</th>
              <th scope="col">Errors</th>
              <th scope="col">LLM calls</th>
              <th scope="col">Input tokens</th>
              <th scope="col">Output tokens</th>
            </tr>
          </thead>
          <tbody>{items.map((trace) => <TraceRow key={trace.trace_id} trace={trace} onOpenTrace={onOpenTrace} />)}</tbody>
        </table>
      </div>
      <Pagination
        currentPage={currentPage}
        pageCount={pageCount}
        canGoPrevious={canGoPrevious}
        canGoNext={canGoNext}
        onPrevious={onPrevious}
        onNext={onNext}
      />
    </section>
  );
}

function Pagination({
  currentPage,
  pageCount,
  canGoPrevious,
  canGoNext,
  onPrevious,
  onNext,
}: {
  currentPage: bigint;
  pageCount: bigint;
  canGoPrevious: boolean;
  canGoNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <nav className="pagination" aria-label="Trace list pagination">
      <button type="button" onClick={onPrevious} disabled={!canGoPrevious}>
        Previous
      </button>
      <span aria-live="polite">Page {currentPage.toString()} of {pageCount.toString()}</span>
      <button type="button" onClick={onNext} disabled={!canGoNext}>
        Next
      </button>
    </nav>
  );
}
