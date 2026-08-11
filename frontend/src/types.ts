export type TraceStatus = "unset" | "ok" | "error";
export type ExactInteger = bigint;
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

export interface SpanListResponse {
  items: SpanRecord[];
}
