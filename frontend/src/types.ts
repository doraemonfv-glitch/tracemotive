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

export type ComparisonAlignment = "exact_match" | "left_only" | "right_only";
export type ComparisonFieldState = "same" | "different" | "left_only" | "right_only" | "unknown";

export interface ComparisonSpanRef {
  trace_id: string;
  span_id: string;
}

export interface ComparisonPathSegment {
  type: string;
  operation: string;
  name: string;
  ordinal: number;
}

export interface ComparisonFieldRecord {
  path: string;
  state: ComparisonFieldState;
  left: CanonicalJsonValue | null;
  right: CanonicalJsonValue | null;
  reason: string | null;
}

export interface ComparisonSpanRecord {
  alignment: ComparisonAlignment;
  semantic_path: ComparisonPathSegment[];
  left: ComparisonSpanRef | null;
  right: ComparisonSpanRef | null;
  differences: ComparisonFieldRecord[];
  uncertainties: ComparisonFieldRecord[];
}

export interface ComparisonGroupSignature {
  type: string;
  operation: string;
  name: string;
}

export interface ComparisonAmbiguousGroup {
  alignment: "ambiguous_group";
  parent_path: ComparisonPathSegment[];
  group_signature: ComparisonGroupSignature;
  left_count: bigint;
  right_count: bigint;
  resolved_members: ComparisonSpanRef[];
  ambiguous_members: {
    left: ComparisonSpanRef[];
    right: ComparisonSpanRef[];
  };
  left_only_count: bigint | null;
  right_only_count: bigint | null;
  reason: string;
}

export interface ComparisonUnavailableSpan {
  alignment: "unavailable";
  side: "left" | "right";
  span: ComparisonSpanRef;
  reason: string;
}

export interface ComparisonTraceView {
  trace: TraceHeader;
  stats: TraceStats;
}

export interface ComparisonTraceField {
  path: string;
  state: "same" | "different" | "unknown";
  left: CanonicalJsonValue | null;
  right: CanonicalJsonValue | null;
  reason: string | null;
}

export interface ComparisonAlignmentSummary {
  matched_spans: bigint;
  left_only_spans: bigint;
  right_only_spans: bigint;
  ambiguous_groups: bigint;
  unavailable_spans: bigint;
}

export interface ComparisonSummary {
  trace_fields: ComparisonTraceField[];
  alignment: ComparisonAlignmentSummary;
  difference_count: bigint;
  uncertainty_count: bigint;
}

export interface TraceComparisonResponse {
  comparison_version: "0.2";
  left_trace: ComparisonTraceView;
  right_trace: ComparisonTraceView;
  summary: ComparisonSummary;
  spans: ComparisonSpanRecord[];
  ambiguous_groups: ComparisonAmbiguousGroup[];
  unavailable_spans: ComparisonUnavailableSpan[];
}

export type InvestigationState = "identified" | "uncertain" | "none";
export type InvestigationFindingType =
  | "new_error"
  | "resolved_error"
  | "tool_input_changed"
  | "tool_output_changed"
  | "tool_added"
  | "tool_removed"
  | "execution_subtree_added"
  | "execution_subtree_removed"
  | "tool_repetition_changed"
  | "model_changed"
  | "request_parameters_changed"
  | "trace_status_changed";
export type InvestigationFindingScope = "behavioral" | "context_only";
export type InvestigationObservationState = "confirmed_observation" | "observation_limited";

export interface InvestigationCoordinate {
  kind: "span" | "sibling_group" | "trace_summary";
  semantic_path: ComparisonPathSegment[];
  group_signature: ComparisonGroupSignature | null;
}

export interface InvestigationFinding {
  finding_id: string;
  type: InvestigationFindingType;
  coordinate: InvestigationCoordinate;
  left: ComparisonSpanRef | null;
  right: ComparisonSpanRef | null;
  field_path: string | null;
  scope: InvestigationFindingScope;
  observation_state: InvestigationObservationState;
  reason_code: string;
  observed: CanonicalJsonObject;
  evidence: CanonicalJsonObject[];
  relationships: Array<{ relation: string; structural_relation?: string }>;
}

export interface InvestigationUncertainty {
  uncertainty_id: string;
  coordinate: InvestigationCoordinate | null;
  reason_code: string;
  side: "left" | "right" | "both";
  blocks_earlier_claim: boolean;
  evidence: CanonicalJsonObject[];
}

export interface InvestigationEvidenceReference {
  finding_id: string;
  relation: string;
  structural_relation: string;
}

export interface LastReliablyMatchedPoint {
  semantic_path: ComparisonPathSegment[];
  left: ComparisonSpanRef | null;
  right: ComparisonSpanRef | null;
  state: "none" | "matched";
  reason: string;
}

export interface InvestigationStartingPoint {
  kind: InvestigationCoordinate["kind"];
  semantic_path: ComparisonPathSegment[];
  group_signature: ComparisonGroupSignature | null;
  left: ComparisonSpanRef | null;
  right: ComparisonSpanRef | null;
  finding_id: string;
  label: string;
}

export interface InvestigationSummaryView {
  state: InvestigationState;
  ordering_basis: "structural_triage_order";
  starting_point: InvestigationStartingPoint | null;
  first_meaningful_divergence: {
    state: InvestigationState;
    ordering_basis: "structural_triage_order";
    finding_id: string | null;
    reason_code: string | null;
  };
  last_reliably_matched_point: LastReliablyMatchedPoint;
  evidence_summary: InvestigationEvidenceReference[];
  context_finding_ids: string[];
  blocking_uncertainty_ids: string[];
  limitations: Array<{
    uncertainty_id: string;
    reason_code: string;
    side: "left" | "right" | "both";
    coordinate: InvestigationCoordinate | null;
    blocks_earlier_claim: boolean;
  }>;
}

export interface InsightTraceIdentity {
  trace_id: string;
  name: string;
  status: TraceStatus;
}

export interface InsightDetailEndpoint {
  method: "GET";
  path: string;
  comparison_version: "0.2";
}

export interface TraceInsightResponse {
  comparison_version: "0.3";
  left_trace: InsightTraceIdentity;
  right_trace: InsightTraceIdentity;
  summary: {
    alignment: ComparisonAlignmentSummary;
    finding_count: bigint;
    uncertainty_count: bigint;
    trace_fields: ComparisonTraceField[];
  };
  investigation: InvestigationSummaryView;
  findings: InvestigationFinding[];
  uncertainties: InvestigationUncertainty[];
  detail_endpoint: InsightDetailEndpoint;
}
