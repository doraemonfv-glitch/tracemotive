export type TraceStatus = "unset" | "ok" | "error";
import type { LosslessNumber } from "lossless-json";

export type ExactInteger = bigint;
export type CanonicalJsonNumber = number | LosslessNumber;
export type CanonicalJsonValue =
  | null
  | boolean
  | string
  | CanonicalJsonNumber
  | CanonicalJsonValue[]
  | { [key: string]: CanonicalJsonValue };
export type CanonicalJsonObject = { [key: string]: CanonicalJsonValue };
export type SpanStatus = TraceStatus;
export type SpanType = "agent" | "llm" | "tool" | "handoff" | "retrieval" | "custom";

// This mirrors the completed GET /api/v1/traces TraceSummary response only.
export interface TraceSummary {
  trace_id: string;
  name: string;
  started_at: string;
  ended_at: string | null;
  status: TraceStatus;
  latency_ms: number | null;
  span_count: ExactInteger;
  error_count: ExactInteger;
  llm_call_count: ExactInteger;
  input_tokens: ExactInteger | null;
  output_tokens: ExactInteger | null;
}

export interface TraceListResponse {
  items: TraceSummary[];
  limit: number;
  offset: ExactInteger;
  total: ExactInteger;
}

export interface TraceListFilters {
  limit: number;
  offset: ExactInteger;
  status: TraceStatus | null;
  name: string | null;
}

export interface TraceHeader {
  trace_id: string;
  name: string;
  started_at: string;
  ended_at: string | null;
  status: TraceStatus;
}

export interface TraceStats {
  latency_ms: number | null;
  span_count: ExactInteger;
  error_count: ExactInteger;
  llm_call_count: ExactInteger;
  input_tokens: ExactInteger | null;
  output_tokens: ExactInteger | null;
}

export interface TraceDetailResponse {
  trace: TraceHeader;
  stats: TraceStats;
}

export interface SpanRecord {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  type: SpanType;
  operation: string;
  name: string;
  started_at: string;
  ended_at: string | null;
  status: SpanStatus;
  latency_ms: number | null;
}

export interface SpanIdentity {
  trace_id: string;
  span_id: string;
}

export interface SpanListResponse {
  items: SpanRecord[];
}

export interface CaptureInfo {
  state: "captured" | "not_captured";
  reason: "disabled" | "source_unavailable" | "not_yet_available" | "size_limit" | "serialization_error" | null;
  redacted: boolean;
}

export interface SpanCapture {
  input: CaptureInfo;
  output: CaptureInfo;
}

export interface SpanError {
  type: string | null;
  message: string | null;
}

export interface AgentDetails {
  kind: "agent";
  agent_name: string | null;
  agent_version: string | null;
}

export interface LLMUsage {
  input_tokens: ExactInteger | null;
  output_tokens: ExactInteger | null;
  reasoning_output_tokens: ExactInteger | null;
  cache_read_input_tokens: ExactInteger | null;
  cache_creation_input_tokens: ExactInteger | null;
}

export interface EstimatedCost {
  amount: CanonicalJsonNumber;
  currency: string;
  estimated: true;
}

export interface LLMDetails {
  kind: "llm";
  provider: string | null;
  request_model: string | null;
  response_model: string | null;
  response_id: string | null;
  usage: LLMUsage;
  finish_reasons: string[];
  request_parameters: CanonicalJsonObject | null;
  estimated_cost: EstimatedCost | null;
}

export interface ToolDetails {
  kind: "tool";
  tool_name: string;
  tool_call_id: string | null;
}

export interface HandoffDetails {
  kind: "handoff";
  from_agent: string | null;
  to_agent: string | null;
}

export interface RetrievalDetails {
  kind: "retrieval";
}

export interface CustomDetails {
  kind: "custom";
  source_type: string | null;
}

export type SpanDetails =
  | AgentDetails
  | LLMDetails
  | ToolDetails
  | HandoffDetails
  | RetrievalDetails
  | CustomDetails;

export interface SpanDetail extends SpanRecord {
  schema_version: "0.1";
  error: SpanError | null;
  input: CanonicalJsonValue;
  output: CanonicalJsonValue;
  capture: SpanCapture;
  source: Record<string, unknown>;
  metadata: CanonicalJsonObject;
  attributes: CanonicalJsonObject;
  details: SpanDetails;
}

export interface SpanDetailResponse {
  span: SpanDetail;
  latency_ms: number | null;
}
