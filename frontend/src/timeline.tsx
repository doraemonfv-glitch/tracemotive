import { useMemo } from "react";
import { formatDuration, spanIdentityKey } from "./span-tree";
import type { SpanRecord, TraceHeader } from "./types";

const TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$/;
const MICROSECONDS_PER_SECOND = 1_000_000n;
const MICROSECONDS_PER_DAY = 86_400n * MICROSECONDS_PER_SECOND;

type SpanTiming =
  | { kind: "completed"; startedAtUs: bigint; endedAtUs: bigint }
  | { kind: "incomplete"; startedAtUs: bigint }
  | { kind: "invalid" };

export type TimelineDomainState = "complete" | "unfinished" | "malformed";

export interface ExactTimelineRatio {
  numerator: bigint;
  denominator: bigint;
}

export interface TimelineGeometry {
  startRatio: ExactTimelineRatio;
  widthRatio: ExactTimelineRatio;
  startPercent: string;
  widthPercent: string;
  marker: boolean;
  clipped: boolean;
}

export interface TimelineRow {
  key: string;
  span: SpanRecord;
  geometry: TimelineGeometry | null;
  timing: "completed" | "incomplete" | "invalid" | "trace-malformed";
}

export interface TimelineModel {
  domainState: TimelineDomainState;
  domainEndUs: bigint | null;
  rows: TimelineRow[];
}

const PERCENT_DECIMAL_PLACES = 24;
const PERCENT_DECIMAL_SCALE = 10n ** BigInt(PERCENT_DECIMAL_PLACES);
const SAFE_PERCENT_RE = /^(?:0|[1-9]\d{0,1}|100)(?:\.\d+)?%$/;

function isLeapYear(year: bigint): boolean {
  return year % 4n === 0n && (year % 100n !== 0n || year % 400n === 0n);
}

function daysInMonth(year: bigint, month: number): number {
  if (month === 2) {
    return isLeapYear(year) ? 29 : 28;
  }
  return month === 4 || month === 6 || month === 9 || month === 11 ? 30 : 31;
}

// Canonical timestamps use the same four-digit, UTC-Z grammar as the backend
// validator. This conversion uses proleptic Gregorian calendar arithmetic and
// never passes a timestamp through Date or a floating-point epoch value.
function daysFromCivil(year: bigint, month: number, day: number): bigint {
  const adjustedYear = year - (month <= 2 ? 1n : 0n);
  const era = adjustedYear / 400n;
  const yearOfEra = adjustedYear - era * 400n;
  const marchMonth = BigInt(month + (month > 2 ? -3 : 9));
  const dayOfYear = (153n * marchMonth + 2n) / 5n + BigInt(day - 1);
  const dayOfEra = yearOfEra * 365n + yearOfEra / 4n - yearOfEra / 100n + dayOfYear;
  return era * 146_097n + dayOfEra - 719_468n;
}

export function parseCanonicalTimestampToUs(value: string): bigint | null {
  const match = TIMESTAMP_RE.exec(value);
  if (match === null) {
    return null;
  }

  const year = BigInt(match[1]);
  // These are bounded calendar fields only; timestamp deltas and the epoch
  // microsecond value remain bigint-backed throughout geometry construction.
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (
    year < 1n ||
    year > 9999n ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > daysInMonth(year, month) ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return null;
  }

  const fraction = match[7] ?? "";
  const fractionalMicroseconds = BigInt(fraction.padEnd(6, "0"));
  const secondsWithinDay = BigInt(hour * 3_600 + minute * 60 + second);
  return daysFromCivil(year, month, day) * MICROSECONDS_PER_DAY + secondsWithinDay * MICROSECONDS_PER_SECOND + fractionalMicroseconds;
}

function spanTiming(span: SpanRecord): SpanTiming {
  const startedAtUs = parseCanonicalTimestampToUs(span.started_at);
  if (startedAtUs === null) {
    return { kind: "invalid" };
  }
  if (span.ended_at === null) {
    return { kind: "incomplete", startedAtUs };
  }
  const endedAtUs = parseCanonicalTimestampToUs(span.ended_at);
  if (endedAtUs === null || endedAtUs < startedAtUs) {
    return { kind: "invalid" };
  }
  return { kind: "completed", startedAtUs, endedAtUs };
}

function clamp(value: bigint, minimum: bigint, maximum: bigint): bigint {
  return value < minimum ? minimum : value > maximum ? maximum : value;
}

function exactRatio(numerator: bigint, denominator: bigint): ExactTimelineRatio {
  return { numerator, denominator };
}

function ratioPercent(ratio: ExactTimelineRatio): string {
  if (ratio.denominator <= 0n || ratio.numerator <= 0n) {
    return "0%";
  }
  if (ratio.numerator >= ratio.denominator) {
    return "100%";
  }

  // The ratio remains exact until this bounded presentation projection. A
  // fixed 24 fractional places is enough to distinguish one microsecond over
  // the complete four-digit CanonicalTimestamp domain (~3.2e17 microseconds),
  // while keeping CSS strings bounded and deterministic.
  const scaled = (ratio.numerator * 100n * PERCENT_DECIMAL_SCALE) / ratio.denominator;
  const whole = scaled / PERCENT_DECIMAL_SCALE;
  const fraction = scaled % PERCENT_DECIMAL_SCALE;
  if (fraction === 0n) {
    return `${whole}%`;
  }
  const fractionText = fraction.toString().padStart(PERCENT_DECIMAL_PLACES, "0").replace(/0+$/, "");
  return `${whole}.${fractionText}%`;
}

function latestObservedDomainEnd(traceStartUs: bigint, timings: SpanTiming[]): bigint {
  let domainEndUs = traceStartUs;
  for (const timing of timings) {
    if (timing.kind === "invalid") {
      continue;
    }
    if (timing.startedAtUs > domainEndUs) {
      domainEndUs = timing.startedAtUs;
    }
    if (timing.kind === "completed" && timing.endedAtUs > domainEndUs) {
      domainEndUs = timing.endedAtUs;
    }
  }
  return domainEndUs;
}

function geometryFor(timing: Exclude<SpanTiming, { kind: "invalid" }>, traceStartUs: bigint, domainEndUs: bigint): TimelineGeometry {
  const domainLength = domainEndUs - traceStartUs;
  const rawStart = timing.startedAtUs - traceStartUs;
  const rawEnd = (timing.kind === "completed" ? timing.endedAtUs : timing.startedAtUs) - traceStartUs;
  const clampedStart = clamp(rawStart, 0n, domainLength);
  const clampedEnd = clamp(rawEnd, clampedStart, domainLength);
  const width = clampedEnd - clampedStart;
  const startRatio = exactRatio(clampedStart, domainLength);
  const widthRatio = exactRatio(width, domainLength);
  return {
    startRatio,
    widthRatio,
    startPercent: ratioPercent(startRatio),
    widthPercent: ratioPercent(widthRatio),
    marker: timing.kind === "incomplete" || width === 0n,
    clipped: rawStart !== clampedStart || rawEnd !== clampedEnd,
  };
}

export function buildTimelineModel(trace: Pick<TraceHeader, "started_at" | "ended_at">, spans: SpanRecord[]): TimelineModel {
  const traceStartUs = parseCanonicalTimestampToUs(trace.started_at);
  const timings = spans.map(spanTiming);
  if (traceStartUs === null) {
    return {
      domainState: "malformed",
      domainEndUs: null,
      rows: spans.map((span) => ({
        key: spanIdentityKey(span.trace_id, span.span_id),
        span,
        geometry: null,
        timing: "trace-malformed",
      })),
    };
  }

  const parsedTraceEndUs = trace.ended_at === null ? null : parseCanonicalTimestampToUs(trace.ended_at);
  if (trace.ended_at !== null && (parsedTraceEndUs === null || parsedTraceEndUs < traceStartUs)) {
    return {
      domainState: "malformed",
      domainEndUs: null,
      rows: spans.map((span) => ({
        key: spanIdentityKey(span.trace_id, span.span_id),
        span,
        geometry: null,
        timing: "trace-malformed",
      })),
    };
  }

  const domainState: TimelineDomainState = trace.ended_at === null ? "unfinished" : "complete";
  const domainEndUs = parsedTraceEndUs ?? latestObservedDomainEnd(traceStartUs, timings);

  return {
    domainState,
    domainEndUs,
    rows: spans.map((span, index) => {
      const timing = timings[index];
      if (timing.kind === "invalid") {
        return {
          key: spanIdentityKey(span.trace_id, span.span_id),
          span,
          geometry: null,
          timing: "invalid",
        };
      }
      return {
        key: spanIdentityKey(span.trace_id, span.span_id),
        span,
        geometry: geometryFor(timing, traceStartUs, domainEndUs),
        timing: timing.kind,
      };
    }),
  };
}

function safePercent(value: string): string {
  return SAFE_PERCENT_RE.test(value) ? value : "0%";
}

function timingText(row: TimelineRow): string {
  if (row.timing === "trace-malformed") {
    return "Trace timing unavailable";
  }
  if (row.timing === "invalid") {
    return "Invalid observed timing";
  }
  if (row.timing === "incomplete") {
    return row.geometry?.clipped ? "In progress; start clipped to trace domain" : "In progress; no end observed";
  }
  if (row.geometry?.clipped) {
    return "Observed timing clipped to trace domain";
  }
  return row.geometry?.marker ? "Zero-duration observed" : "Completed";
}

function accessibleBarLabel(row: TimelineRow): string {
  const end = row.span.ended_at === null ? "not ended" : row.span.ended_at;
  return `Span ${row.span.name}; id ${row.span.span_id}; type ${row.span.type}; status ${row.span.status}; started ${row.span.started_at}; ended ${end}; duration ${formatDuration(row.span.latency_ms)}; ${timingText(row)}.`;
}

function TimelineBar({ row }: { row: TimelineRow }) {
  if (row.geometry === null) {
    return <div className="timeline-unavailable" role="img" aria-label={accessibleBarLabel(row)}>Timing unavailable</div>;
  }
  const barStyle = {
    left: safePercent(row.geometry.startPercent),
    width: safePercent(row.geometry.widthPercent),
  };
  const markerStyle = { left: safePercent(row.geometry.startPercent) };
  return (
    <div
      className={`timeline-track timeline-track-${row.span.status}${row.timing === "incomplete" ? " timeline-track-incomplete" : ""}`}
      role="img"
      aria-label={accessibleBarLabel(row)}
    >
      <span className="timeline-bar" style={barStyle} />
      {row.geometry.marker && <span className="timeline-point" style={markerStyle} />}
    </div>
  );
}

export function Timeline({ trace, spans }: { trace: Pick<TraceHeader, "started_at" | "ended_at">; spans: SpanRecord[] }) {
  const model = useMemo(() => buildTimelineModel(trace, spans), [spans, trace]);

  if (model.rows.length === 0) {
    return (
      <section className="state-message empty-state" aria-live="polite">
        <p>This trace contains no spans to place on the timeline.</p>
      </section>
    );
  }

  return (
    <section className="timeline-section" aria-label="Execution timeline">
      <p className="timeline-description">
        {model.domainState === "malformed"
          ? "Trace timing unavailable; observed spans are not positioned."
          : "Observed timing relative to Trace start. Overlap shows time only, not causality."}
      </p>
      <div className="timeline-scroll">
        <div className="timeline-rows">
          {model.rows.map((row) => (
            <div className="timeline-row" key={row.key}>
              <div className="timeline-row-facts">
                <code>{row.span.span_id}</code>
                <span className="timeline-type">{row.span.type}</span>
                <span>{formatDuration(row.span.latency_ms)}</span>
                <span>{timingText(row)}</span>
              </div>
              <TimelineBar row={row} />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
