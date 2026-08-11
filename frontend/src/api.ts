import { isLosslessNumber, parse, toSafeNumberOrThrow } from "lossless-json";
import type {
  SpanListResponse,
  SpanCapture,
  SpanDetail,
  SpanDetailResponse,
  SpanDetails,
  SpanError,
  CaptureInfo,
  CanonicalJsonObject,
  EstimatedCost,
  LLMUsage,
  SpanRecord,
  SpanType,
  TraceDetailResponse,
  TraceHeader,
  TraceListFilters,
  TraceListResponse,
  TraceStats,
  TraceSummary,
} from "./types";

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

function exactIntegerOrNull(value: unknown): bigint | null | undefined {
  return value === null ? null : exactNonnegativeInteger(value);
}

function traceHeader(value: unknown, expectedTraceId?: string): TraceHeader | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  if (
    typeof value.trace_id !== "string" ||
    (expectedTraceId !== undefined && value.trace_id !== expectedTraceId) ||
    typeof value.name !== "string" ||
    typeof value.started_at !== "string" ||
    (typeof value.ended_at !== "string" && value.ended_at !== null) ||
    (value.status !== "unset" && value.status !== "ok" && value.status !== "error")
  ) {
    return undefined;
  }
  return {
    trace_id: value.trace_id,
    name: value.name,
    started_at: value.started_at,
    ended_at: value.ended_at,
    status: value.status,
  };
}

function traceStats(value: unknown): TraceStats | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const spanCount = exactNonnegativeInteger(value.span_count);
  const errorCount = exactNonnegativeInteger(value.error_count);
  const llmCallCount = exactNonnegativeInteger(value.llm_call_count);
  const inputTokens = exactIntegerOrNull(value.input_tokens);
  const outputTokens = exactIntegerOrNull(value.output_tokens);
  const latency = safeLatency(value.latency_ms);
  if (
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
    latency_ms: latency,
    span_count: spanCount,
    error_count: errorCount,
    llm_call_count: llmCallCount,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
  };
}

const SPAN_TYPES: readonly SpanType[] = ["agent", "llm", "tool", "handoff", "retrieval", "custom"];
const CAPTURE_REASONS = ["disabled", "source_unavailable", "not_yet_available", "size_limit", "serialization_error"] as const;

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function isCanonicalJsonObject(value: unknown): value is CanonicalJsonObject {
  return isRecord(value) && !isLosslessNumber(value);
}

function decodeCaptureInfo(value: unknown): CaptureInfo | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["state", "reason", "redacted"])) {
    return undefined;
  }
  if (value.state !== "captured" && value.state !== "not_captured") {
    return undefined;
  }
  if (typeof value.reason !== "string" && value.reason !== null) {
    return undefined;
  }
  const reason = value.reason as CaptureInfo["reason"];
  if (value.state === "captured" && reason !== null) {
    return undefined;
  }
  if (value.state === "not_captured" && (reason === null || !CAPTURE_REASONS.includes(reason))) {
    return undefined;
  }
  if (typeof value.redacted !== "boolean" || (value.state === "not_captured" && value.redacted)) {
    return undefined;
  }
  return { state: value.state, reason, redacted: value.redacted };
}

function decodeCapture(value: unknown): SpanCapture | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["input", "output"])) {
    return undefined;
  }
  const input = decodeCaptureInfo(value.input);
  const output = decodeCaptureInfo(value.output);
  return input === undefined || output === undefined ? undefined : { input, output };
}

function decodeError(value: unknown): SpanError | null | undefined {
  if (value === null) {
    return null;
  }
  if (!isRecord(value) || !hasExactKeys(value, ["type", "message"])) {
    return undefined;
  }
  if ((typeof value.type !== "string" && value.type !== null) || (typeof value.message !== "string" && value.message !== null)) {
    return undefined;
  }
  if (value.type === null && value.message === null) {
    return undefined;
  }
  return { type: value.type, message: value.message };
}

function decodeSource(value: unknown): Record<string, unknown> | undefined {
  if (!isRecord(value) || !hasExactKeys(value, [
    "framework",
    "framework_version",
    "integration",
    "integration_version",
    "native_trace_id",
    "native_span_id",
    "native_parent_span_id",
  ])) {
    return undefined;
  }
  const nullableStrings = ["framework", "framework_version", "native_trace_id", "native_span_id", "native_parent_span_id"];
  if (nullableStrings.some((key) => typeof value[key] !== "string" && value[key] !== null)) {
    return undefined;
  }
  if (typeof value.integration !== "string" || typeof value.integration_version !== "string") {
    return undefined;
  }
  return value;
}

function decodeUsage(value: unknown): LLMUsage | undefined {
  if (!isRecord(value) || !hasExactKeys(value, [
    "input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
  ])) {
    return undefined;
  }
  const usage: Partial<LLMUsage> = {};
  for (const key of [
    "input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
  ] as const) {
    const parsed = exactIntegerOrNull(value[key]);
    if (parsed === undefined) {
      return undefined;
    }
    usage[key] = parsed;
  }
  return usage as LLMUsage;
}

function isNonnegativeCanonicalJsonNumber(value: unknown): value is EstimatedCost["amount"] {
  if (isLosslessNumber(value)) {
    const text = value.toString();
    if (!text.startsWith("-")) {
      return true;
    }
    // A signed zero is numerically non-negative; inspect its digits instead of
    // converting an exact value through Number and losing precision/overflow.
    const mantissa = text.slice(1).split(/[eE]/, 1)[0].replace(".", "");
    return /^0+$/.test(mantissa);
  }
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function decodeEstimatedCost(value: unknown): EstimatedCost | null | undefined {
  if (value === null) {
    return null;
  }
  if (!isRecord(value) || !hasExactKeys(value, ["amount", "currency", "estimated"])) {
    return undefined;
  }
  if (!isNonnegativeCanonicalJsonNumber(value.amount) || typeof value.currency !== "string" || value.currency === "" || value.currency !== value.currency.toUpperCase() || value.estimated !== true) {
    return undefined;
  }
  return { amount: value.amount, currency: value.currency, estimated: true };
}

function decodeDetails(value: unknown, expectedType: SpanType): SpanDetails | undefined {
  if (!isRecord(value) || typeof value.kind !== "string" || value.kind !== expectedType) {
    return undefined;
  }
  switch (expectedType) {
    case "agent":
      if (!hasExactKeys(value, ["kind", "agent_name", "agent_version"]) || (typeof value.agent_name !== "string" && value.agent_name !== null) || (typeof value.agent_version !== "string" && value.agent_version !== null)) {
        return undefined;
      }
      return { kind: "agent", agent_name: value.agent_name, agent_version: value.agent_version };
    case "llm": {
      if (!hasExactKeys(value, ["kind", "provider", "request_model", "response_model", "response_id", "usage", "finish_reasons", "request_parameters", "estimated_cost"]) || ["provider", "request_model", "response_model", "response_id"].some((key) => typeof value[key] !== "string" && value[key] !== null)) {
        return undefined;
      }
      const usage = decodeUsage(value.usage);
      const estimatedCost = decodeEstimatedCost(value.estimated_cost);
      if (usage === undefined || estimatedCost === undefined || !Array.isArray(value.finish_reasons) || value.finish_reasons.some((reason) => typeof reason !== "string") || (value.request_parameters !== null && !isCanonicalJsonObject(value.request_parameters))) {
        return undefined;
      }
      const provider = value.provider as string | null;
      const requestModel = value.request_model as string | null;
      const responseModel = value.response_model as string | null;
      const responseId = value.response_id as string | null;
      return {
        kind: "llm",
        provider,
        request_model: requestModel,
        response_model: responseModel,
        response_id: responseId,
        usage,
        finish_reasons: value.finish_reasons,
        request_parameters: value.request_parameters,
        estimated_cost: estimatedCost,
      };
    }
    case "tool":
      if (!hasExactKeys(value, ["kind", "tool_name", "tool_call_id"]) || typeof value.tool_name !== "string" || value.tool_name.length === 0 || (typeof value.tool_call_id !== "string" && value.tool_call_id !== null)) {
        return undefined;
      }
      return { kind: "tool", tool_name: value.tool_name, tool_call_id: value.tool_call_id };
    case "handoff":
      if (!hasExactKeys(value, ["kind", "from_agent", "to_agent"]) || (typeof value.from_agent !== "string" && value.from_agent !== null) || (typeof value.to_agent !== "string" && value.to_agent !== null)) {
        return undefined;
      }
      return { kind: "handoff", from_agent: value.from_agent, to_agent: value.to_agent };
    case "retrieval":
      return hasExactKeys(value, ["kind"]) ? { kind: "retrieval" } : undefined;
    case "custom":
      if (!hasExactKeys(value, ["kind", "source_type"]) || (typeof value.source_type !== "string" && value.source_type !== null)) {
        return undefined;
      }
      return { kind: "custom", source_type: value.source_type };
  }
}

function decodeSpanDetail(value: unknown, expectedTraceId: string, latency_ms: number | null | undefined): SpanDetail | undefined {
  if (!isRecord(value) || !hasExactKeys(value, [
    "schema_version",
    "trace_id",
    "span_id",
    "parent_span_id",
    "type",
    "operation",
    "name",
    "started_at",
    "ended_at",
    "status",
    "error",
    "input",
    "output",
    "capture",
    "source",
    "metadata",
    "attributes",
    "details",
  ]) || value.schema_version !== "0.1") {
    return undefined;
  }
  const summary = spanRecord(value, expectedTraceId, latency_ms);
  if (summary === undefined) {
    return undefined;
  }
  const error = decodeError(value.error);
  const capture = decodeCapture(value.capture);
  const source = decodeSource(value.source);
  const details = decodeDetails(value.details, summary.type);
  if (
    error === undefined ||
    capture === undefined ||
    source === undefined ||
    !isCanonicalJsonObject(value.metadata) ||
    !isCanonicalJsonObject(value.attributes) ||
    details === undefined ||
    (summary.ended_at === null && (summary.status !== "unset" || error !== null)) ||
    (summary.ended_at !== null && summary.status === "unset") ||
    (capture.input.state === "not_captured" && value.input !== null) ||
    (capture.output.state === "not_captured" && value.output !== null) ||
    (summary.ended_at === null && (capture.output.state !== "not_captured" || capture.output.reason !== "not_yet_available" || capture.output.redacted))
  ) {
    return undefined;
  }
  return {
    ...summary,
    schema_version: "0.1",
    error,
    input: value.input as SpanDetail["input"],
    output: value.output as SpanDetail["output"],
    capture,
    source,
    metadata: value.metadata,
    attributes: value.attributes,
    details,
  };
}

function spanRecord(value: unknown, expectedTraceId: string, latency_ms: number | null | undefined): SpanRecord | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const type = value.type;
  if (
    typeof value.trace_id !== "string" ||
    value.trace_id !== expectedTraceId ||
    typeof value.span_id !== "string" ||
    (typeof value.parent_span_id !== "string" && value.parent_span_id !== null) ||
    typeof type !== "string" ||
    !SPAN_TYPES.includes(type as SpanType) ||
    typeof value.operation !== "string" ||
    typeof value.name !== "string" ||
    typeof value.started_at !== "string" ||
    (typeof value.ended_at !== "string" && value.ended_at !== null) ||
    (value.status !== "unset" && value.status !== "ok" && value.status !== "error")
  ) {
    return undefined;
  }
  if (latency_ms === undefined) {
    return undefined;
  }
  return {
    trace_id: value.trace_id,
    span_id: value.span_id,
    parent_span_id: value.parent_span_id,
    type: type as SpanType,
    operation: value.operation,
    name: value.name,
    started_at: value.started_at,
    ended_at: value.ended_at,
    status: value.status,
    latency_ms,
  };
}

export class QueryApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super("Query API request failed");
    this.name = "QueryApiError";
    this.status = status;
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

export function decodeTraceDetailResponse(text: string, expectedTraceId: string): TraceDetailResponse {
  const value: unknown = parse(text);
  if (!isRecord(value)) {
    throw new Error("Trace detail response was invalid");
  }
  const trace = traceHeader(value.trace, expectedTraceId);
  const stats = traceStats(value.stats);
  if (trace === undefined || stats === undefined) {
    throw new Error("Trace detail response was invalid");
  }
  return { trace, stats };
}

export function decodeSpanListResponse(text: string, expectedTraceId: string): SpanListResponse {
  const value: unknown = parse(text);
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new Error("Span list response was invalid");
  }
  const items: SpanRecord[] = [];
  const identities = new Set<string>();
  for (const item of value.items) {
    if (!isRecord(item)) {
      throw new Error("Span list response was invalid");
    }
    const latency = safeLatency(item.latency_ms);
    const span = spanRecord(item.span, expectedTraceId, latency);
    if (span === undefined) {
      throw new Error("Span list response was invalid");
    }
    const identity = `${span.trace_id}\u0000${span.span_id}`;
    if (identities.has(identity)) {
      throw new Error("Span list response was invalid");
    }
    identities.add(identity);
    items.push(span);
  }
  return { items };
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

export function traceDetailUrl(traceId: string): string {
  return `${TRACE_LIST_PATH}/${encodeURIComponent(traceId)}`;
}

export function spanListUrl(traceId: string): string {
  return `${traceDetailUrl(traceId)}/spans`;
}

export function spanDetailUrl(traceId: string, spanId: string): string {
  return `${spanListUrl(traceId)}/${encodeURIComponent(spanId)}`;
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

async function fetchQueryApiText(url: string, signal: AbortSignal): Promise<string> {
  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new QueryApiError(response.status);
  }
  return response.text();
}

export async function fetchTraceDetail(traceId: string, signal: AbortSignal): Promise<TraceDetailResponse> {
  const responseText = await fetchQueryApiText(traceDetailUrl(traceId), signal);
  return decodeTraceDetailResponse(responseText, traceId);
}

export async function fetchSpanList(traceId: string, signal: AbortSignal): Promise<SpanListResponse> {
  const responseText = await fetchQueryApiText(spanListUrl(traceId), signal);
  return decodeSpanListResponse(responseText, traceId);
}

export function decodeSpanDetailResponse(text: string, expectedTraceId: string, expectedSpanId: string): SpanDetailResponse {
  const value: unknown = parse(text);
  if (!isRecord(value) || !hasExactKeys(value, ["span", "latency_ms"])) {
    throw new Error("Span detail response was invalid");
  }
  const latency = safeLatency(value.latency_ms);
  if (latency === undefined) {
    throw new Error("Span detail response was invalid");
  }
  const span = decodeSpanDetail(value.span, expectedTraceId, latency);
  if (span === undefined || span.span_id !== expectedSpanId) {
    throw new Error("Span detail response was invalid");
  }
  return { span, latency_ms: latency };
}

export async function fetchSpanDetail(traceId: string, spanId: string, signal: AbortSignal): Promise<SpanDetailResponse> {
  const responseText = await fetchQueryApiText(spanDetailUrl(traceId, spanId), signal);
  return decodeSpanDetailResponse(responseText, traceId, spanId);
}
