import { isLosslessNumber, parse, toSafeNumberOrThrow } from "lossless-json";
import type { TraceListFilters, TraceListResponse, TraceSummary } from "./types";

export const TRACE_LIST_PATH = "/api/v1/traces";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactNonnegativeInteger(value: unknown): bigint | undefined {
  if (!isLosslessNumber(value)) {
    return undefined;
  }
  const text = value.toString();
  if (!/^(?:0|[1-9]\d*)$/.test(text)) {
    return undefined;
  }
  return BigInt(text);
}

function safeLimit(value: unknown): number | undefined {
  const integer = exactNonnegativeInteger(value);
  if (integer === undefined || integer < 1n || integer > 100n) {
    return undefined;
  }
  return Number(integer);
}

function safeLatency(value: unknown): number | null | undefined {
  if (value === null) {
    return null;
  }
  if (!isLosslessNumber(value)) {
    return undefined;
  }
  try {
    const latency = toSafeNumberOrThrow(value.toString());
    return latency >= 0 ? latency : undefined;
  } catch {
    return undefined;
  }
}

function traceSummary(value: unknown): TraceSummary | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const spanCount = exactNonnegativeInteger(value.span_count);
  const errorCount = exactNonnegativeInteger(value.error_count);
  const llmCallCount = exactNonnegativeInteger(value.llm_call_count);
  const inputTokens = value.input_tokens === null ? null : exactNonnegativeInteger(value.input_tokens);
  const outputTokens = value.output_tokens === null ? null : exactNonnegativeInteger(value.output_tokens);
  const latency = safeLatency(value.latency_ms);
  if (
    typeof value.trace_id !== "string" ||
    typeof value.name !== "string" ||
    typeof value.started_at !== "string" ||
    (typeof value.ended_at !== "string" && value.ended_at !== null) ||
    (value.status !== "unset" && value.status !== "ok" && value.status !== "error") ||
    spanCount === undefined ||
    errorCount === undefined ||
    llmCallCount === undefined ||
    inputTokens === undefined ||
    outputTokens === undefined ||
    latency === undefined
  ) {
    return undefined;
  }
  return {
    trace_id: value.trace_id,
    name: value.name,
    started_at: value.started_at,
    ended_at: value.ended_at,
    status: value.status,
    latency_ms: latency,
    span_count: spanCount,
    error_count: errorCount,
    llm_call_count: llmCallCount,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
  };
}

export function decodeTraceListResponse(text: string): TraceListResponse {
  const value: unknown = parse(text);
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new Error("Trace list response was invalid");
  }
  const limit = safeLimit(value.limit);
  const offset = exactNonnegativeInteger(value.offset);
  const total = exactNonnegativeInteger(value.total);
  const items: TraceSummary[] = [];
  for (const item of value.items) {
    const parsedItem = traceSummary(item);
    if (parsedItem === undefined) {
      throw new Error("Trace list response was invalid");
    }
    items.push(parsedItem);
  }
  if (limit === undefined || offset === undefined || total === undefined) {
    throw new Error("Trace list response was invalid");
  }
  return { items, limit, offset, total };
}

export function traceListUrl(filters: TraceListFilters): string {
  const parameters = new URLSearchParams({
    limit: String(filters.limit),
    offset: String(filters.offset),
  });
  if (filters.status !== null) {
    parameters.set("status", filters.status);
  }
  // null means the filter is omitted.  An empty string deliberately remains
  // name= so the Query API retains its distinct empty-filter semantics.
  if (filters.name !== null) {
    parameters.set("name", filters.name);
  }
  return `${TRACE_LIST_PATH}?${parameters.toString()}`;
}

export async function fetchTraceList(
  filters: TraceListFilters,
  signal: AbortSignal,
): Promise<TraceListResponse> {
  const response = await fetch(traceListUrl(filters), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error("Trace list request failed");
  }
  return decodeTraceListResponse(await response.text());
}
