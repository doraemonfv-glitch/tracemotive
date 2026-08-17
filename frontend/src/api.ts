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
  ComparisonAlignmentSummary,
  ComparisonAmbiguousGroup,
  ComparisonFieldRecord,
  ComparisonFieldState,
  ComparisonGroupSignature,
  ComparisonPathSegment,
  ComparisonSpanRecord,
  ComparisonSpanRef,
  ComparisonSummary,
  ComparisonTraceField,
  ComparisonTraceView,
  ComparisonUnavailableSpan,
  StructuredDiffObservation,
  StructuredDiffRecord,
  InvestigationCoordinate,
  InvestigationEvidenceReference,
  InvestigationFinding,
  InvestigationFindingType,
  InvestigationState,
  InvestigationStartingPoint,
  InvestigationSummaryView,
  InvestigationUncertainty,
  InsightDetailEndpoint,
  InsightTraceIdentity,
  TraceDetailResponse,
  TraceHeader,
  TraceInsightResponse,
  TraceInsightV4Response,
  V4Action,
  TraceListFilters,
  TraceListResponse,
  TraceStats,
  TraceSummary,
  TraceComparisonResponse,
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

const COMPARISON_FIELD_STATES: readonly ComparisonFieldState[] = ["same", "different", "left_only", "right_only", "unknown"];

function decodeComparisonRef(value: unknown): ComparisonSpanRef | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["trace_id", "span_id"]) || typeof value.trace_id !== "string" || typeof value.span_id !== "string" || value.trace_id.length === 0 || value.span_id.length === 0) {
    return undefined;
  }
  return { trace_id: value.trace_id, span_id: value.span_id };
}

function decodeComparisonPath(value: unknown): ComparisonPathSegment[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const segments: ComparisonPathSegment[] = [];
  for (const segment of value) {
    if (!isRecord(segment) || !hasExactKeys(segment, ["type", "operation", "name", "ordinal"]) || typeof segment.type !== "string" || typeof segment.operation !== "string" || typeof segment.name !== "string") {
      return undefined;
    }
    const ordinal = exactNonnegativeInteger(segment.ordinal);
    if (ordinal === undefined || ordinal > BigInt(Number.MAX_SAFE_INTEGER)) {
      return undefined;
    }
    segments.push({ type: segment.type, operation: segment.operation, name: segment.name, ordinal: Number(ordinal) });
  }
  return segments;
}

function decodeComparisonField(value: unknown): ComparisonFieldRecord | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["path", "state", "left", "right", "reason"]) || typeof value.path !== "string" || typeof value.reason !== "string" && value.reason !== null || !COMPARISON_FIELD_STATES.includes(value.state as ComparisonFieldState)) {
    return undefined;
  }
  return {
    path: value.path,
    state: value.state as ComparisonFieldState,
    left: value.left as ComparisonFieldRecord["left"],
    right: value.right as ComparisonFieldRecord["right"],
    reason: value.reason,
  };
}

function decodeComparisonFields(value: unknown): ComparisonFieldRecord[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const fields: ComparisonFieldRecord[] = [];
  for (const field of value) {
    const decoded = decodeComparisonField(field);
    if (decoded === undefined) {
      return undefined;
    }
    fields.push(decoded);
  }
  return fields;
}

function decodeComparisonSpan(value: unknown): ComparisonSpanRecord | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["alignment", "semantic_path", "left", "right", "differences", "uncertainties"])) {
    return undefined;
  }
  if (value.alignment !== "exact_match" && value.alignment !== "left_only" && value.alignment !== "right_only") {
    return undefined;
  }
  const semanticPath = decodeComparisonPath(value.semantic_path);
  const differences = decodeComparisonFields(value.differences);
  const uncertainties = decodeComparisonFields(value.uncertainties);
  const left = value.left === null ? null : decodeComparisonRef(value.left);
  const right = value.right === null ? null : decodeComparisonRef(value.right);
  if (semanticPath === undefined || differences === undefined || uncertainties === undefined || (value.left !== null && left === undefined) || (value.right !== null && right === undefined)) {
    return undefined;
  }
  if ((value.alignment === "exact_match" && (left === null || right === null)) || (value.alignment === "left_only" && (left === null || right !== null)) || (value.alignment === "right_only" && (left !== null || right === null))) {
    return undefined;
  }
  return { alignment: value.alignment, semantic_path: semanticPath, left: left ?? null, right: right ?? null, differences, uncertainties };
}

function decodeComparisonSignature(value: unknown): ComparisonGroupSignature | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["type", "operation", "name"]) || typeof value.type !== "string" || typeof value.operation !== "string" || typeof value.name !== "string") {
    return undefined;
  }
  return { type: value.type, operation: value.operation, name: value.name };
}

function decodeComparisonRefs(value: unknown): ComparisonSpanRef[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const refs: ComparisonSpanRef[] = [];
  for (const item of value) {
    const ref = decodeComparisonRef(item);
    if (ref === undefined) {
      return undefined;
    }
    refs.push(ref);
  }
  return refs;
}

function decodeComparisonAmbiguousGroup(value: unknown): ComparisonAmbiguousGroup | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["alignment", "parent_path", "group_signature", "left_count", "right_count", "resolved_members", "ambiguous_members", "left_only_count", "right_only_count", "reason"]) || value.alignment !== "ambiguous_group" || typeof value.reason !== "string") {
    return undefined;
  }
  const parentPath = decodeComparisonPath(value.parent_path);
  const signature = decodeComparisonSignature(value.group_signature);
  const leftCount = exactNonnegativeInteger(value.left_count);
  const rightCount = exactNonnegativeInteger(value.right_count);
  const resolvedMembers = decodeComparisonRefs(value.resolved_members);
  const members = isRecord(value.ambiguous_members) && hasExactKeys(value.ambiguous_members, ["left", "right"])
    ? { left: decodeComparisonRefs(value.ambiguous_members.left), right: decodeComparisonRefs(value.ambiguous_members.right) }
    : undefined;
  const leftOnlyCount = value.left_only_count === null ? null : exactNonnegativeInteger(value.left_only_count);
  const rightOnlyCount = value.right_only_count === null ? null : exactNonnegativeInteger(value.right_only_count);
  if (parentPath === undefined || signature === undefined || leftCount === undefined || rightCount === undefined || resolvedMembers === undefined || members === undefined || members.left === undefined || members.right === undefined || leftOnlyCount === undefined || rightOnlyCount === undefined) {
    return undefined;
  }
  return {
    alignment: "ambiguous_group",
    parent_path: parentPath,
    group_signature: signature,
    left_count: leftCount,
    right_count: rightCount,
    resolved_members: resolvedMembers,
    ambiguous_members: { left: members.left, right: members.right },
    left_only_count: leftOnlyCount,
    right_only_count: rightOnlyCount,
    reason: value.reason,
  };
}

function decodeComparisonUnavailable(value: unknown): ComparisonUnavailableSpan | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["alignment", "side", "span", "reason"]) || value.alignment !== "unavailable" || (value.side !== "left" && value.side !== "right") || typeof value.reason !== "string") {
    return undefined;
  }
  const span = decodeComparisonRef(value.span);
  return span === undefined ? undefined : { alignment: "unavailable", side: value.side, span, reason: value.reason };
}

function decodeComparisonTraceField(value: unknown): ComparisonTraceField | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["path", "state", "left", "right", "reason"]) || typeof value.path !== "string" || (value.state !== "same" && value.state !== "different" && value.state !== "unknown") || (typeof value.reason !== "string" && value.reason !== null)) {
    return undefined;
  }
  return { path: value.path, state: value.state, left: value.left as ComparisonTraceField["left"], right: value.right as ComparisonTraceField["right"], reason: value.reason };
}

function decodeComparisonTraceFields(value: unknown): ComparisonTraceField[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const fields: ComparisonTraceField[] = [];
  for (const field of value) {
    const decoded = decodeComparisonTraceField(field);
    if (decoded === undefined) {
      return undefined;
    }
    fields.push(decoded);
  }
  return fields;
}

function decodeComparisonTraceView(value: unknown, expectedTraceId: string): ComparisonTraceView | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["trace", "stats"])) {
    return undefined;
  }
  const trace = traceHeader(value.trace, expectedTraceId);
  const stats = traceStats(value.stats);
  return trace === undefined || stats === undefined ? undefined : { trace, stats };
}

function decodeComparisonSummary(value: unknown): ComparisonSummary | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["trace_fields", "alignment", "difference_count", "uncertainty_count"]) || !isRecord(value.alignment)) {
    return undefined;
  }
  const traceFields = decodeComparisonTraceFields(value.trace_fields);
  const alignmentValue = value.alignment;
  if (!hasExactKeys(alignmentValue, ["matched_spans", "left_only_spans", "right_only_spans", "ambiguous_groups", "unavailable_spans"])) {
    return undefined;
  }
  const alignment: Partial<ComparisonAlignmentSummary> = {};
  for (const key of ["matched_spans", "left_only_spans", "right_only_spans", "ambiguous_groups", "unavailable_spans"] as const) {
    const parsed = exactNonnegativeInteger(alignmentValue[key]);
    if (parsed === undefined) {
      return undefined;
    }
    alignment[key] = parsed;
  }
  const differenceCount = exactNonnegativeInteger(value.difference_count);
  const uncertaintyCount = exactNonnegativeInteger(value.uncertainty_count);
  if (traceFields === undefined || differenceCount === undefined || uncertaintyCount === undefined) {
    return undefined;
  }
  return { trace_fields: traceFields, alignment: alignment as ComparisonAlignmentSummary, difference_count: differenceCount, uncertainty_count: uncertaintyCount };
}

export function decodeComparisonResponse(text: string, leftTraceId: string, rightTraceId: string): TraceComparisonResponse {
  const value: unknown = parse(text);
  if (!isRecord(value) || !hasExactKeys(value, ["comparison_version", "left_trace", "right_trace", "summary", "spans", "ambiguous_groups", "unavailable_spans"]) || value.comparison_version !== "0.2") {
    throw new Error("Trace comparison response was invalid");
  }
  const leftTrace = decodeComparisonTraceView(value.left_trace, leftTraceId);
  const rightTrace = decodeComparisonTraceView(value.right_trace, rightTraceId);
  const summary = decodeComparisonSummary(value.summary);
  const spans = Array.isArray(value.spans) ? value.spans.map(decodeComparisonSpan) : undefined;
  const groups = Array.isArray(value.ambiguous_groups) ? value.ambiguous_groups.map(decodeComparisonAmbiguousGroup) : undefined;
  const unavailable = Array.isArray(value.unavailable_spans) ? value.unavailable_spans.map(decodeComparisonUnavailable) : undefined;
  if (leftTrace === undefined || rightTrace === undefined || summary === undefined || spans === undefined || spans.some((item) => item === undefined) || groups === undefined || groups.some((item) => item === undefined) || unavailable === undefined || unavailable.some((item) => item === undefined)) {
    throw new Error("Trace comparison response was invalid");
  }
  return {
    comparison_version: "0.2",
    left_trace: leftTrace,
    right_trace: rightTrace,
    summary,
    spans: spans as ComparisonSpanRecord[],
    ambiguous_groups: groups as ComparisonAmbiguousGroup[],
    unavailable_spans: unavailable as ComparisonUnavailableSpan[],
  };
}

const INVESTIGATION_STATES = ["identified", "uncertain", "none"] as const;
const INVESTIGATION_FINDING_TYPES: readonly InvestigationFindingType[] = [
  "new_error",
  "resolved_error",
  "tool_input_changed",
  "tool_output_changed",
  "tool_added",
  "tool_removed",
  "execution_subtree_added",
  "execution_subtree_removed",
  "tool_repetition_changed",
  "model_changed",
  "request_parameters_changed",
  "trace_status_changed",
];

function decodeCanonicalObject(value: unknown): CanonicalJsonObject | undefined {
  return isRecord(value) ? value as CanonicalJsonObject : undefined;
}

function decodeCanonicalObjectArray(value: unknown): CanonicalJsonObject[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const objects = value.map(decodeCanonicalObject);
  return objects.some((item) => item === undefined) ? undefined : objects as CanonicalJsonObject[];
}

function decodeInsightCoordinate(value: unknown): InvestigationCoordinate | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["kind", "semantic_path", "group_signature"])) {
    return undefined;
  }
  if (value.kind !== "span" && value.kind !== "sibling_group" && value.kind !== "trace_summary") {
    return undefined;
  }
  const semanticPath = decodeComparisonPath(value.semantic_path);
  const groupSignature = value.group_signature === null ? null : decodeComparisonSignature(value.group_signature);
  if (semanticPath === undefined || (value.group_signature !== null && groupSignature === undefined)) {
    return undefined;
  }
  return { kind: value.kind, semantic_path: semanticPath, group_signature: groupSignature ?? null };
}

function decodeInsightRef(value: unknown): ComparisonSpanRef | null | undefined {
  return value === null ? null : decodeComparisonRef(value);
}

function decodeInsightFinding(value: unknown): InvestigationFinding | undefined {
  if (!isRecord(value) || !hasExactKeys(value, [
    "finding_id",
    "type",
    "coordinate",
    "left",
    "right",
    "field_path",
    "scope",
    "observation_state",
    "reason_code",
    "observed",
    "evidence",
    "relationships",
  ])) {
    return undefined;
  }
  const coordinate = decodeInsightCoordinate(value.coordinate);
  const left = decodeInsightRef(value.left);
  const right = decodeInsightRef(value.right);
  const observed = decodeCanonicalObject(value.observed);
  const evidence = decodeCanonicalObjectArray(value.evidence);
  const relationships = Array.isArray(value.relationships) ? value.relationships.map((item) => {
    if (!isRecord(item) || typeof item.relation !== "string" || (item.structural_relation !== undefined && typeof item.structural_relation !== "string")) {
      return undefined;
    }
    return { relation: item.relation, structural_relation: item.structural_relation as string | undefined };
  }) : undefined;
  if (
    typeof value.finding_id !== "string" ||
    !INVESTIGATION_FINDING_TYPES.includes(value.type as InvestigationFindingType) ||
    coordinate === undefined ||
    left === undefined ||
    right === undefined ||
    (typeof value.field_path !== "string" && value.field_path !== null) ||
    (value.scope !== "behavioral" && value.scope !== "context_only") ||
    (value.observation_state !== "confirmed_observation" && value.observation_state !== "observation_limited") ||
    typeof value.reason_code !== "string" ||
    observed === undefined ||
    evidence === undefined ||
    relationships === undefined ||
    relationships.some((item) => item === undefined)
  ) {
    return undefined;
  }
  return {
    finding_id: value.finding_id,
    type: value.type as InvestigationFindingType,
    coordinate,
    left,
    right,
    field_path: value.field_path,
    scope: value.scope,
    observation_state: value.observation_state,
    reason_code: value.reason_code,
    observed,
    evidence,
    relationships: relationships as InvestigationFinding["relationships"],
  };
}

function decodeInsightUncertainty(value: unknown): InvestigationUncertainty | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["uncertainty_id", "coordinate", "reason_code", "side", "blocks_earlier_claim", "evidence"])) {
    return undefined;
  }
  const coordinate = value.coordinate === null ? null : decodeInsightCoordinate(value.coordinate);
  const evidence = decodeCanonicalObjectArray(value.evidence);
  if (
    typeof value.uncertainty_id !== "string" ||
    (value.coordinate !== null && coordinate === undefined) ||
    typeof value.reason_code !== "string" ||
    (value.side !== "left" && value.side !== "right" && value.side !== "both") ||
    typeof value.blocks_earlier_claim !== "boolean" ||
    evidence === undefined
  ) {
    return undefined;
  }
  return {
    uncertainty_id: value.uncertainty_id,
    coordinate: coordinate ?? null,
    reason_code: value.reason_code,
    side: value.side,
    blocks_earlier_claim: value.blocks_earlier_claim,
    evidence,
  };
}

function decodeInsightStartingPoint(value: unknown): InvestigationStartingPoint | null | undefined {
  if (value === null) {
    return null;
  }
  if (!isRecord(value) || !hasExactKeys(value, ["kind", "semantic_path", "group_signature", "left", "right", "finding_id", "label"])) {
    return undefined;
  }
  const coordinate = decodeInsightCoordinate({
    kind: value.kind,
    semantic_path: value.semantic_path,
    group_signature: value.group_signature,
  });
  const left = decodeInsightRef(value.left);
  const right = decodeInsightRef(value.right);
  if (coordinate === undefined || left === undefined || right === undefined || typeof value.finding_id !== "string" || typeof value.label !== "string") {
    return undefined;
  }
  return { ...coordinate, left, right, finding_id: value.finding_id, label: value.label };
}

function decodeLastReliablyMatchedPoint(value: unknown): InvestigationSummaryView["last_reliably_matched_point"] | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["semantic_path", "left", "right", "state", "reason"])) {
    return undefined;
  }
  const semanticPath = decodeComparisonPath(value.semantic_path);
  const left = decodeInsightRef(value.left);
  const right = decodeInsightRef(value.right);
  if (semanticPath === undefined || left === undefined || right === undefined || (value.state !== "none" && value.state !== "matched") || typeof value.reason !== "string") {
    return undefined;
  }
  return { semantic_path: semanticPath, left, right, state: value.state, reason: value.reason };
}

function decodeInvestigationSummary(value: unknown): InvestigationSummaryView | undefined {
  if (!isRecord(value) || !hasExactKeys(value, [
    "state",
    "ordering_basis",
    "starting_point",
    "first_meaningful_divergence",
    "last_reliably_matched_point",
    "evidence_summary",
    "context_finding_ids",
    "blocking_uncertainty_ids",
    "limitations",
  ]) || !INVESTIGATION_STATES.includes(value.state as InvestigationSummaryView["state"]) || value.ordering_basis !== "structural_triage_order") {
    return undefined;
  }
  const startingPoint = decodeInsightStartingPoint(value.starting_point);
  const first = isRecord(value.first_meaningful_divergence) && hasExactKeys(value.first_meaningful_divergence, ["state", "ordering_basis", "finding_id", "reason_code"])
    ? value.first_meaningful_divergence
    : undefined;
  const last = decodeLastReliablyMatchedPoint(value.last_reliably_matched_point);
  const evidenceSummary = Array.isArray(value.evidence_summary) ? value.evidence_summary.map((item) => {
    if (!isRecord(item) || typeof item.finding_id !== "string" || typeof item.relation !== "string" || typeof item.structural_relation !== "string") {
      return undefined;
    }
    return { finding_id: item.finding_id, relation: item.relation, structural_relation: item.structural_relation } satisfies InvestigationEvidenceReference;
  }) : undefined;
  const limitations = Array.isArray(value.limitations) ? value.limitations.map((item) => {
    if (!isRecord(item) || typeof item.uncertainty_id !== "string" || typeof item.reason_code !== "string" || (item.side !== "left" && item.side !== "right" && item.side !== "both") || typeof item.blocks_earlier_claim !== "boolean") {
      return undefined;
    }
    const coordinate = item.coordinate === null ? null : decodeInsightCoordinate(item.coordinate);
    return coordinate === undefined && item.coordinate !== null ? undefined : {
      uncertainty_id: item.uncertainty_id,
      reason_code: item.reason_code,
      side: item.side,
      coordinate: coordinate ?? null,
      blocks_earlier_claim: item.blocks_earlier_claim,
    };
  }) : undefined;
  if (
    startingPoint === undefined ||
    first === undefined ||
    !INVESTIGATION_STATES.includes(first.state as InvestigationState) ||
    first.ordering_basis !== "structural_triage_order" ||
    (typeof first.finding_id !== "string" && first.finding_id !== null) ||
    (typeof first.reason_code !== "string" && first.reason_code !== null) ||
    last === undefined ||
    evidenceSummary === undefined ||
    evidenceSummary.some((item) => item === undefined) ||
    !Array.isArray(value.context_finding_ids) ||
    value.context_finding_ids.some((item) => typeof item !== "string") ||
    !Array.isArray(value.blocking_uncertainty_ids) ||
    value.blocking_uncertainty_ids.some((item) => typeof item !== "string") ||
    limitations === undefined ||
    limitations.some((item) => item === undefined)
  ) {
    return undefined;
  }
  return {
    state: value.state as InvestigationSummaryView["state"],
    ordering_basis: "structural_triage_order",
    starting_point: startingPoint,
    first_meaningful_divergence: {
      state: first.state as InvestigationSummaryView["state"],
      ordering_basis: "structural_triage_order",
      finding_id: first.finding_id,
      reason_code: first.reason_code,
    },
    last_reliably_matched_point: last,
    evidence_summary: evidenceSummary as InvestigationEvidenceReference[],
    context_finding_ids: value.context_finding_ids,
    blocking_uncertainty_ids: value.blocking_uncertainty_ids,
    limitations: limitations as InvestigationSummaryView["limitations"],
  };
}

function validateInsightReferences(
  investigation: InvestigationSummaryView,
  findings: InvestigationFinding[],
  uncertainties: InvestigationUncertainty[],
): boolean {
  const findingsById = new Map<string, InvestigationFinding>();
  for (const finding of findings) {
    if (findingsById.has(finding.finding_id)) {
      return false;
    }
    findingsById.set(finding.finding_id, finding);
  }
  const uncertaintiesById = new Map<string, InvestigationUncertainty>();
  for (const uncertainty of uncertainties) {
    if (uncertaintiesById.has(uncertainty.uncertainty_id)) {
      return false;
    }
    uncertaintiesById.set(uncertainty.uncertainty_id, uncertainty);
  }

  const behavioralFinding = (findingId: string): boolean => findingsById.get(findingId)?.scope === "behavioral";
  const contextFinding = (findingId: string): boolean => findingsById.get(findingId)?.scope === "context_only";
  const unique = (items: string[]): boolean => new Set(items).size === items.length;

  if (investigation.state === "identified" && investigation.starting_point === null) {
    return false;
  }
  if (investigation.starting_point !== null && !behavioralFinding(investigation.starting_point.finding_id)) {
    return false;
  }
  const firstFindingId = investigation.first_meaningful_divergence.finding_id;
  if (firstFindingId !== null && !behavioralFinding(firstFindingId)) {
    return false;
  }
  if (investigation.state === "identified" && (firstFindingId === null || investigation.starting_point?.finding_id !== firstFindingId)) {
    return false;
  }

  const evidenceIds = investigation.evidence_summary.map((reference) => reference.finding_id);
  if (!unique(evidenceIds) || evidenceIds.some((findingId) => !behavioralFinding(findingId))) {
    return false;
  }
  const contextIds = investigation.context_finding_ids;
  if (!unique(contextIds) || contextIds.some((findingId) => !contextFinding(findingId))) {
    return false;
  }
  const blockingIds = investigation.blocking_uncertainty_ids;
  if (!unique(blockingIds) || blockingIds.some((uncertaintyId) => !uncertaintiesById.has(uncertaintyId))) {
    return false;
  }
  const limitationIds = investigation.limitations.map((limitation) => limitation.uncertainty_id);
  return unique(limitationIds) && limitationIds.every((uncertaintyId) => uncertaintiesById.has(uncertaintyId));
}

function decodeInsightTraceIdentity(value: unknown, expectedTraceId: string): InsightTraceIdentity | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["trace_id", "name", "status"]) || value.trace_id !== expectedTraceId || typeof value.name !== "string" || (value.status !== "unset" && value.status !== "ok" && value.status !== "error")) {
    return undefined;
  }
  return { trace_id: value.trace_id, name: value.name, status: value.status };
}

function decodeInsightAlignment(value: unknown): ComparisonAlignmentSummary | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["matched_spans", "left_only_spans", "right_only_spans", "ambiguous_groups", "unavailable_spans"])) {
    return undefined;
  }
  const fields = ["matched_spans", "left_only_spans", "right_only_spans", "ambiguous_groups", "unavailable_spans"] as const;
  const counts = fields.map((field) => exactNonnegativeInteger(value[field]));
  return counts.some((item) => item === undefined) ? undefined : {
    matched_spans: counts[0] as bigint,
    left_only_spans: counts[1] as bigint,
    right_only_spans: counts[2] as bigint,
    ambiguous_groups: counts[3] as bigint,
    unavailable_spans: counts[4] as bigint,
  };
}

function decodeInsightDetailEndpoint(value: unknown, leftTraceId: string, rightTraceId: string): InsightDetailEndpoint | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["method", "path", "comparison_version"]) || value.method !== "GET" || value.comparison_version !== "0.2" || value.path !== comparisonUrl(leftTraceId, rightTraceId)) {
    return undefined;
  }
  return { method: "GET", path: value.path, comparison_version: "0.2" };
}

export function decodeInsightComparisonResponse(text: string, leftTraceId: string, rightTraceId: string): TraceInsightResponse {
  const value: unknown = parse(text);
  if (!isRecord(value) || !hasExactKeys(value, ["comparison_version", "left_trace", "right_trace", "summary", "investigation", "findings", "uncertainties", "detail_endpoint"]) || value.comparison_version !== "0.3") {
    throw new Error("Trace insight response was invalid");
  }
  const leftTrace = decodeInsightTraceIdentity(value.left_trace, leftTraceId);
  const rightTrace = decodeInsightTraceIdentity(value.right_trace, rightTraceId);
  const summaryValue = isRecord(value.summary) && hasExactKeys(value.summary, ["alignment", "finding_count", "uncertainty_count", "trace_fields"]) ? value.summary : undefined;
  const alignment = summaryValue === undefined ? undefined : decodeInsightAlignment(summaryValue.alignment);
  const findingCount = summaryValue === undefined ? undefined : exactNonnegativeInteger(summaryValue.finding_count);
  const uncertaintyCount = summaryValue === undefined ? undefined : exactNonnegativeInteger(summaryValue.uncertainty_count);
  const traceFields = summaryValue === undefined ? undefined : decodeComparisonTraceFields(summaryValue.trace_fields);
  const investigation = decodeInvestigationSummary(value.investigation);
  const findings = Array.isArray(value.findings) ? value.findings.map(decodeInsightFinding) : undefined;
  const uncertainties = Array.isArray(value.uncertainties) ? value.uncertainties.map(decodeInsightUncertainty) : undefined;
  const detailEndpoint = decodeInsightDetailEndpoint(value.detail_endpoint, leftTraceId, rightTraceId);
  if (
    leftTrace === undefined ||
    rightTrace === undefined ||
    summaryValue === undefined ||
    alignment === undefined ||
    findingCount === undefined ||
    uncertaintyCount === undefined ||
    traceFields === undefined ||
    investigation === undefined ||
    findings === undefined ||
    findings.some((item) => item === undefined) ||
    uncertainties === undefined ||
    uncertainties.some((item) => item === undefined) ||
    detailEndpoint === undefined
  ) {
    throw new Error("Trace insight response was invalid");
  }
  if (findingCount !== BigInt(findings.length) || uncertaintyCount !== BigInt(uncertainties.length)) {
    throw new Error("Trace insight response was invalid");
  }
  if (!validateInsightReferences(investigation, findings as InvestigationFinding[], uncertainties as InvestigationUncertainty[])) {
    throw new Error("Trace insight response was invalid");
  }
  return {
    comparison_version: "0.3",
    left_trace: leftTrace,
    right_trace: rightTrace,
    summary: { alignment, finding_count: findingCount, uncertainty_count: uncertaintyCount, trace_fields: traceFields },
    investigation,
    findings: findings as InvestigationFinding[],
    uncertainties: uncertainties as InvestigationUncertainty[],
    detail_endpoint: detailEndpoint,
  };
}

function decodeStructuredDiffObservation(value: unknown): StructuredDiffObservation | undefined {
  if (!isRecord(value) || (value.state !== "present" && value.state !== "absent" && value.state !== "unavailable" && value.state !== "redacted")) {
    return undefined;
  }
  if (value.state === "present" && !Object.prototype.hasOwnProperty.call(value, "value")) {
    return undefined;
  }
  if ((value.state === "unavailable" || value.state === "redacted") && Object.prototype.hasOwnProperty.call(value, "value")) {
    return undefined;
  }
  return {
    state: value.state,
    ...(Object.prototype.hasOwnProperty.call(value, "value") ? { value: value.value as StructuredDiffObservation["value"] } : {}),
  };
}

function decodeStructuredDiff(value: unknown): StructuredDiffRecord | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["op", "path", "left", "right", "reason"]) || (value.op !== "add" && value.op !== "remove" && value.op !== "replace") || typeof value.path !== "string" || (value.path !== "" && !value.path.startsWith("/")) || (typeof value.reason !== "string" && value.reason !== null)) {
    return undefined;
  }
  const left = decodeStructuredDiffObservation(value.left);
  const right = decodeStructuredDiffObservation(value.right);
  return left === undefined || right === undefined ? undefined : {
    op: value.op,
    path: value.path,
    left,
    right,
    reason: value.reason,
  };
}

function decodeV4Action(value: unknown): V4Action | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["type", "target"])) {
    return undefined;
  }
  if (value.type === "open_left" || value.type === "open_right") {
    const target = decodeComparisonRef(value.target);
    return target === undefined ? undefined : { type: value.type, target };
  }
  if (value.type === "full_comparison" || value.type === "copy_local_reference") {
    if (!isRecord(value.target) || !hasExactKeys(value.target, ["hash"]) || typeof value.target.hash !== "string" || !value.target.hash.startsWith("#/compare/")) {
      return undefined;
    }
    return { type: value.type, target: { hash: value.target.hash } };
  }
  if (value.type === "copy_evidence") {
    if (!isRecord(value.target) || !hasExactKeys(value.target, ["finding_id"]) || typeof value.target.finding_id !== "string") {
      return undefined;
    }
    return { type: value.type, target: { finding_id: value.target.finding_id } };
  }
  return undefined;
}

function decodeV4LastReliablyMatchedPoint(value: unknown): InvestigationSummaryView["last_reliably_matched_point"] | undefined {
  if (!isRecord(value) || !hasExactKeys(value, ["state", "left", "right", "coordinate", "reason"])) {
    return undefined;
  }
  const semanticPath = decodeComparisonPath(value.coordinate);
  const left = decodeInsightRef(value.left);
  const right = decodeInsightRef(value.right);
  if (semanticPath === undefined || left === undefined || right === undefined || (value.state !== "none" && value.state !== "matched") || typeof value.reason !== "string") {
    return undefined;
  }
  return { semantic_path: semanticPath, left, right, state: value.state, reason: value.reason };
}

function decodeV4Finding(value: unknown): TraceInsightV4Response["findings"][number] | undefined {
  const requiredKeys = [
    "id", "type", "coordinate", "left", "right", "scope", "observation_state",
    "reason_code", "field_path", "observed", "evidence", "structured_diff_available",
    "structured_diff_reason", "relationships", "actions",
  ] as const;
  if (!isRecord(value) || !requiredKeys.every((key) => Object.prototype.hasOwnProperty.call(value, key))) {
    return undefined;
  }
  const coordinate = decodeInsightCoordinate(value.coordinate);
  const left = decodeInsightRef(value.left);
  const right = decodeInsightRef(value.right);
  const observed = decodeCanonicalObject(value.observed);
  const evidence = decodeCanonicalObjectArray(value.evidence);
  const relationships = isRecord(value.relationships) && Array.isArray(value.relationships.supports) && Array.isArray(value.relationships.limited_by) ? { supports: value.relationships.supports, limited_by: value.relationships.limited_by } : undefined;
  const actions = Array.isArray(value.actions) ? value.actions.map(decodeV4Action) : undefined;
  const structuredDiff = Array.isArray(value.structured_diff) ? value.structured_diff.map(decodeStructuredDiff) : undefined;
  const hasTruncated = Object.prototype.hasOwnProperty.call(value, "structured_diff_truncated");
  if (
    typeof value.id !== "string" ||
    !INVESTIGATION_FINDING_TYPES.includes(value.type as InvestigationFindingType) ||
    coordinate === undefined ||
    left === undefined ||
    right === undefined ||
    (typeof value.field_path !== "string" && value.field_path !== null) ||
    (value.scope !== "behavioral" && value.scope !== "context_only") ||
    (value.observation_state !== "confirmed_observation" && value.observation_state !== "observation_limited") ||
    typeof value.reason_code !== "string" ||
    observed === undefined ||
    evidence === undefined ||
    typeof value.structured_diff_available !== "boolean" ||
    (typeof value.structured_diff_reason !== "string" && value.structured_diff_reason !== null) ||
    relationships === undefined ||
    actions === undefined ||
    actions.some((item) => item === undefined) ||
    (value.structured_diff_available && (structuredDiff === undefined || structuredDiff.some((item) => item === undefined) || !hasTruncated || typeof value.structured_diff_truncated !== "boolean")) ||
    (!value.structured_diff_available && (structuredDiff !== undefined || hasTruncated))
  ) {
    return undefined;
  }
  return {
    id: value.id,
    type: value.type as InvestigationFindingType,
    coordinate,
    left,
    right,
    scope: value.scope,
    observation_state: value.observation_state,
    reason_code: value.reason_code,
    field_path: value.field_path,
    observed,
    evidence,
    structured_diff_available: value.structured_diff_available,
    ...(structuredDiff !== undefined ? { structured_diff: structuredDiff as StructuredDiffRecord[] } : {}),
    ...(hasTruncated ? { structured_diff_truncated: value.structured_diff_truncated as boolean } : {}),
    structured_diff_reason: value.structured_diff_reason,
    relationships,
    actions: actions as V4Action[],
  };
}

export function decodeV4ComparisonResponse(text: string, leftTraceId: string, rightTraceId: string): TraceInsightV4Response {
  const value: unknown = parse(text);
  if (!isRecord(value) || !hasExactKeys(value, ["comparison_version", "left", "right", "summary", "investigation", "findings", "uncertainties"]) || value.comparison_version !== "0.4") {
    throw new Error("Trace v4 comparison response was invalid");
  }
  const left = decodeInsightTraceIdentity(value.left, leftTraceId);
  const right = decodeInsightTraceIdentity(value.right, rightTraceId);
  const summary = isRecord(value.summary) && hasExactKeys(value.summary, ["alignment_state", "investigation_state", "last_reliably_matched_point"]) ? value.summary : undefined;
  const lastPoint = summary === undefined ? undefined : decodeV4LastReliablyMatchedPoint(summary.last_reliably_matched_point);
  const investigation = isRecord(value.investigation) && hasExactKeys(value.investigation, ["primary_finding_id", "finding_ids", "uncertainty_ids", "actions"]) ? value.investigation : undefined;
  const findings = Array.isArray(value.findings) ? value.findings.map(decodeV4Finding) : undefined;
  const uncertainties = Array.isArray(value.uncertainties) ? value.uncertainties.map(decodeInsightUncertainty) : undefined;
  const findingIds = investigation === undefined || !Array.isArray(investigation.finding_ids) || investigation.finding_ids.some((item) => typeof item !== "string") ? undefined : investigation.finding_ids;
  const uncertaintyIds = investigation === undefined || !Array.isArray(investigation.uncertainty_ids) || investigation.uncertainty_ids.some((item) => typeof item !== "string") ? undefined : investigation.uncertainty_ids;
  const actions = investigation === undefined || !Array.isArray(investigation.actions) ? undefined : investigation.actions.map(decodeV4Action);
  if (
    left === undefined ||
    right === undefined ||
    summary === undefined ||
    (summary.alignment_state !== "complete" && summary.alignment_state !== "uncertain") ||
    !INVESTIGATION_STATES.includes(summary.investigation_state as InvestigationState) ||
    lastPoint === undefined ||
    investigation === undefined ||
    (typeof investigation.primary_finding_id !== "string" && investigation.primary_finding_id !== null) ||
    findingIds === undefined ||
    uncertaintyIds === undefined ||
    actions === undefined ||
    actions.some((item) => item === undefined) ||
    findings === undefined ||
    findings.some((item) => item === undefined) ||
    uncertainties === undefined ||
    uncertainties.some((item) => item === undefined)
  ) {
    throw new Error("Trace v4 comparison response was invalid");
  }
  const findingItems = findings as TraceInsightV4Response["findings"];
  const uncertaintyItems = uncertainties as InvestigationUncertainty[];
  if (
    findingItems.length !== findingIds.length ||
    findingItems.some((item) => !findingIds.includes(item.id)) ||
    uncertaintyItems.length !== uncertaintyIds.length ||
    uncertaintyItems.some((item) => !uncertaintyIds.includes(item.uncertainty_id))
  ) {
    throw new Error("Trace v4 comparison response was invalid");
  }
  return {
    comparison_version: "0.4",
    left,
    right,
    summary: {
      alignment_state: summary.alignment_state,
      investigation_state: summary.investigation_state as InvestigationState,
      last_reliably_matched_point: lastPoint,
    },
    investigation: {
      primary_finding_id: investigation.primary_finding_id,
      finding_ids: findingIds,
      uncertainty_ids: uncertaintyIds,
      actions: actions as V4Action[],
    },
    findings: findingItems,
    uncertainties: uncertaintyItems,
  };
}

export function mergeStructuredDiff(
  response: TraceInsightResponse,
  v4: TraceInsightV4Response,
): TraceInsightResponse {
  if (response.left_trace.trace_id !== v4.left.trace_id || response.right_trace.trace_id !== v4.right.trace_id) {
    return response;
  }
  const v4ById = new Map(v4.findings.map((finding) => [finding.id, finding]));
  return {
    ...response,
    findings: response.findings.map((finding) => {
      const structured = v4ById.get(finding.finding_id);
      if (structured === undefined) {
        return finding;
      }
      return {
        ...finding,
        structured_diff_available: structured.structured_diff_available,
        structured_diff: structured.structured_diff,
        structured_diff_truncated: structured.structured_diff_truncated,
        structured_diff_reason: structured.structured_diff_reason,
      };
    }),
  };
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

export function comparisonUrl(leftTraceId: string, rightTraceId: string): string {
  return `/api/v2/compare/${encodeURIComponent(leftTraceId)}/${encodeURIComponent(rightTraceId)}`;
}

export function insightComparisonUrl(leftTraceId: string, rightTraceId: string): string {
  return `/api/v3/compare/${encodeURIComponent(leftTraceId)}/${encodeURIComponent(rightTraceId)}`;
}

export function structuredDiffComparisonUrl(leftTraceId: string, rightTraceId: string): string {
  return `/api/v4/compare/${encodeURIComponent(leftTraceId)}/${encodeURIComponent(rightTraceId)}`;
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

export async function fetchTraceComparison(leftTraceId: string, rightTraceId: string, signal: AbortSignal): Promise<TraceComparisonResponse> {
  const responseText = await fetchQueryApiText(comparisonUrl(leftTraceId, rightTraceId), signal);
  return decodeComparisonResponse(responseText, leftTraceId, rightTraceId);
}

export async function fetchInsightComparison(leftTraceId: string, rightTraceId: string, signal: AbortSignal): Promise<TraceInsightResponse> {
  const responseText = await fetchQueryApiText(insightComparisonUrl(leftTraceId, rightTraceId), signal);
  return decodeInsightComparisonResponse(responseText, leftTraceId, rightTraceId);
}

export async function fetchStructuredDiffComparison(leftTraceId: string, rightTraceId: string, signal: AbortSignal): Promise<TraceInsightV4Response> {
  const responseText = await fetchQueryApiText(structuredDiffComparisonUrl(leftTraceId, rightTraceId), signal);
  return decodeV4ComparisonResponse(responseText, leftTraceId, rightTraceId);
}

export async function fetchComparisonDetail(detailEndpoint: InsightDetailEndpoint, leftTraceId: string, rightTraceId: string, signal: AbortSignal): Promise<TraceComparisonResponse> {
  if (detailEndpoint.method !== "GET" || detailEndpoint.comparison_version !== "0.2" || detailEndpoint.path !== comparisonUrl(leftTraceId, rightTraceId)) {
    throw new Error("Trace detail endpoint was invalid");
  }
  const responseText = await fetchQueryApiText(detailEndpoint.path, signal);
  return decodeComparisonResponse(responseText, leftTraceId, rightTraceId);
}
