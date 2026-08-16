import { useMemo } from "react";
import type {
  CanonicalJsonValue,
  InvestigationCoordinate,
  InvestigationFinding,
  InvestigationFindingType,
  InvestigationSummaryView,
  InvestigationUncertainty,
  InsightTraceIdentity,
  LastReliablyMatchedPoint,
  TraceInsightResponse,
} from "./types";

const FINDING_LABELS: Record<InvestigationFindingType, string> = {
  new_error: "New error observed",
  resolved_error: "Error no longer observed",
  tool_input_changed: "Tool input changed",
  tool_output_changed: "Tool output changed",
  tool_added: "Tool appeared",
  tool_removed: "Tool disappeared",
  execution_subtree_added: "Execution subtree appeared",
  execution_subtree_removed: "Execution subtree disappeared",
  tool_repetition_changed: "Tool repetition changed",
  model_changed: "Model changed",
  request_parameters_changed: "Request parameters changed",
  trace_status_changed: "Trace status changed",
};

const RELATION_LABELS: Record<string, string> = {
  same_coordinate: "Same structural region",
  descendant: "Descendant observation",
  structurally_later_independent: "Structurally later independent observation",
  unrelated_branch: "Unrelated branch observation",
  additional_observation: "Additional observation",
};

const BARRIER_LABELS: Record<string, string> = {
  repeated_sibling_ambiguity: "Repeated members are ambiguous",
  capture_unavailable: "Captured content is unavailable on one side",
  redacted_observation: "The observation was redacted",
  missing_parent: "A parent observation is missing",
  invalid_structure: "The trace structure could not be resolved safely",
  incomplete_trace: "One trace ended before the observed execution was complete",
  ambiguous_parent: "The parent location is ambiguous",
  cycle: "The trace contains a structural cycle",
  unsupported_observation: "This observation type is not supported for a safe comparison",
};

const MAX_INLINE_VALUE_LENGTH = 900;
const UNKNOWN_RELATION_LABEL = "Structural relationship not specified";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatValue(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "bigint") {
    return value.toString();
  }
  try {
    return JSON.stringify(value, (_key, nested) => typeof nested === "bigint" ? nested.toString() : nested, 2) ?? "Unavailable";
  } catch {
    return "Unavailable";
  }
}

function stateCopy(state: TraceInsightResponse["investigation"]["state"]): { title: string; description: string } {
  switch (state) {
    case "identified":
      return {
        title: "Investigation starting point",
        description: "TraceMotive found an evidence-supported place to inspect first.",
      };
    case "uncertain":
      return {
        title: "No safe first investigation point",
        description: "TraceMotive observed a limitation that prevents it from safely selecting the first investigation point.",
      };
    case "none":
      return {
        title: "No supported behavioral divergence found",
        description: "TraceMotive did not find a supported behavioral difference in the available observations.",
      };
  }
}

function findingLabel(type: InvestigationFindingType): string {
  return FINDING_LABELS[type];
}

function barrierLabel(reasonCode: string): string {
  return BARRIER_LABELS[reasonCode] ?? "TraceMotive could not safely resolve this observation";
}

function pathLabel(coordinate: InvestigationCoordinate): string {
  const segments = coordinate.semantic_path.map((segment) => `${segment.type} / ${segment.name}`);
  if (coordinate.kind === "sibling_group" && coordinate.group_signature !== null) {
    segments.push(`Repeated group / ${coordinate.group_signature.name}`);
  }
  return segments.join(" / ") || "Trace root";
}

function technicalPath(coordinate: InvestigationCoordinate): string {
  return coordinate.semantic_path.map((segment) => `${segment.type}:${segment.name}[${segment.ordinal}]`).join(" / ") || "Trace root";
}

function sideObserved(finding: InvestigationFinding, side: "left" | "right"): { state: string; value: CanonicalJsonValue | null } | null {
  const value = finding.observed[side];
  if (!isRecord(value) || typeof value.state !== "string") {
    return null;
  }
  return { state: value.state, value: ("value" in value ? value.value : null) as CanonicalJsonValue | null };
}

function observedStateLabel(state: string): string {
  return state.replaceAll("_", " ");
}

function EvidenceValue({ label, value }: { label: string; value: unknown }) {
  const formatted = useMemo(() => formatValue(value), [value]);
  const large = formatted.length > MAX_INLINE_VALUE_LENGTH;
  if (large) {
    return (
      <details className="insight-value disclosure-value">
        <summary>{label}: large value, expand to inspect</summary>
        <pre>{formatted}</pre>
      </details>
    );
  }
  return (
    <div className="insight-value">
      <span className="insight-value-label">{label}</span>
      <pre>{formatted}</pre>
    </div>
  );
}

function FindingObservation({ finding }: { finding: InvestigationFinding }) {
  const left = sideObserved(finding, "left");
  const right = sideObserved(finding, "right");
  if (left === null && right === null) {
    return <p className="insight-muted">The API supplied a group-level structural observation without an individual member pair.</p>;
  }
  const repetition = finding.type === "tool_repetition_changed";
  return (
    <div className="insight-observation-grid" aria-label="Observed values">
      <div className="insight-observation-side">
        <span className="comparison-side-label">Left observation</span>
        {left === null ? <p className="insight-muted">Not observed</p> : (
          <>
            <span className="insight-observation-state">{observedStateLabel(left.state)}{repetition ? " calls" : ""}</span>
            <EvidenceValue label="Value" value={left.value} />
          </>
        )}
      </div>
      <div className="insight-observation-side">
        <span className="comparison-side-label">Right observation</span>
        {right === null ? <p className="insight-muted">Not observed</p> : (
          <>
            <span className="insight-observation-state">{observedStateLabel(right.state)}{repetition ? " calls" : ""}</span>
            <EvidenceValue label="Value" value={right.value} />
          </>
        )}
      </div>
    </div>
  );
}

function FindingDetails({ finding }: { finding: InvestigationFinding }) {
  const formattedEvidence = useMemo(() => finding.evidence.map((item) => formatValue(item)), [finding.evidence]);
  return (
    <details className="insight-technical-details">
      <summary>Evidence and structural details</summary>
      <dl className="insight-detail-list">
        <div><dt>Finding ID</dt><dd><code>{finding.finding_id}</code></dd></div>
        <div><dt>Field</dt><dd><code>{finding.field_path ?? "Structural observation"}</code></dd></div>
        <div><dt>Reason</dt><dd><code>{finding.reason_code}</code></dd></div>
        <div><dt>Structural path</dt><dd><code>{technicalPath(finding.coordinate)}</code></dd></div>
      </dl>
      {finding.evidence.length > 0 && (
        <div className="insight-evidence-list">
          <span className="comparison-side-label">Exact evidence observations</span>
          {formattedEvidence.map((item, index) => (
            <pre key={`${finding.finding_id}-evidence-${index}`}>{item}</pre>
          ))}
        </div>
      )}
    </details>
  );
}

function FindingCard({ finding, relation, primary = false }: { finding: InvestigationFinding; relation?: string; primary?: boolean }) {
  return (
    <article className={`insight-finding-card ${primary ? "insight-finding-primary" : ""}`}>
      <div className="insight-finding-heading">
        <div>
          <span className="insight-observation-badge">{finding.observation_state === "confirmed_observation" ? "Confirmed observation" : "Observation limited"}</span>
          {relation !== undefined && <span className="insight-relation-badge">{RELATION_LABELS[relation] ?? UNKNOWN_RELATION_LABEL}</span>}
          <h3>{findingLabel(finding.type)}</h3>
          <p className="insight-path">{pathLabel(finding.coordinate)}</p>
        </div>
        {finding.scope === "context_only" && <span className="insight-context-badge">Observed context</span>}
      </div>
      <FindingObservation finding={finding} />
      <FindingDetails finding={finding} />
    </article>
  );
}

function UncertaintyCard({ uncertainty, blocking }: { uncertainty: InvestigationUncertainty; blocking: boolean }) {
  return (
    <article className={`insight-uncertainty-card ${blocking ? "insight-uncertainty-blocking" : ""}`}>
      <div className="insight-uncertainty-heading">
        <span className="insight-uncertainty-badge">{blocking ? "Blocks first-point selection" : "Observed limitation"}</span>
        <h3>{barrierLabel(uncertainty.reason_code)}</h3>
      </div>
      <p>{uncertainty.side === "both" ? "Both traces have" : `The ${uncertainty.side} trace has`} an observation limitation here.</p>
      {uncertainty.coordinate !== null && <p className="insight-path">{pathLabel(uncertainty.coordinate)}</p>}
      <details className="insight-technical-details">
        <summary>Limitation details</summary>
        <dl className="insight-detail-list">
          <div><dt>Reason</dt><dd><code>{uncertainty.reason_code}</code></dd></div>
          <div><dt>Uncertainty ID</dt><dd><code>{uncertainty.uncertainty_id}</code></dd></div>
        </dl>
        {uncertainty.evidence.length > 0 && <pre>{formatValue(uncertainty.evidence[0])}</pre>}
      </details>
    </article>
  );
}

function LastMatched({ point }: { point: LastReliablyMatchedPoint }) {
  if (point.state !== "matched") {
    return <p className="insight-muted">No prior reliable structural match was available.</p>;
  }
  const path = point.semantic_path.map((segment) => `${segment.type} / ${segment.name}`).join(" / ");
  return (
    <div className="insight-last-matched">
      <span className="comparison-side-label">Last reliably matched point</span>
      <strong>{path || "Trace root"}</strong>
      <p>Structural evidence only; this does not describe runtime chronology.</p>
    </div>
  );
}

function TracePair({ left, right }: { left: InsightTraceIdentity; right: InsightTraceIdentity }) {
  return (
    <div className="insight-trace-pair">
      <div><span className="comparison-side-label">Left</span><strong>{left.name}</strong><code>{left.trace_id}</code></div>
      <div><span className="comparison-side-label">Right</span><strong>{right.name}</strong><code>{right.trace_id}</code></div>
    </div>
  );
}

export function ComparisonInsight({
  response,
  onOpenDetails,
  detailsState,
}: {
  response: TraceInsightResponse;
  onOpenDetails: () => void;
  detailsState: "closed" | "loading" | "loaded" | "not-found" | "invalid" | "too-large" | "error";
}) {
  const { investigation } = response;
  const findingsById = new Map(response.findings.map((finding) => [finding.finding_id, finding]));
  const primaryId = investigation.starting_point?.finding_id ?? null;
  const primary = primaryId === null ? null : findingsById.get(primaryId) ?? null;
  const additional = investigation.evidence_summary
    .map((reference) => ({ reference, finding: findingsById.get(reference.finding_id) }))
    .filter((item): item is { reference: typeof item.reference; finding: InvestigationFinding } => item.finding !== undefined);
  const context = investigation.context_finding_ids
    .map((findingId) => findingsById.get(findingId))
    .filter((finding): finding is InvestigationFinding => finding !== undefined);
  const blockingIds = new Set(investigation.blocking_uncertainty_ids);
  const orderedUncertainties = [...response.uncertainties].sort((left, right) => Number(blockingIds.has(right.uncertainty_id)) - Number(blockingIds.has(left.uncertainty_id)));
  const copy = stateCopy(investigation.state);

  return (
    <section className="comparison-insight" aria-label="Investigation insight">
      <div className={`insight-state-panel insight-state-${investigation.state}`}>
        <div>
          <p className="eyebrow">Investigation status</p>
          <span className="insight-state-badge">{investigation.state === "identified" ? "Confirmed observation" : investigation.state === "uncertain" ? "Observed limitation" : "No supported divergence"}</span>
          <h2>{copy.title}</h2>
          <p>{copy.description}</p>
        </div>
        <TracePair left={response.left_trace} right={response.right_trace} />
      </div>

      <p className="insight-order-note">“First” means deterministic structural triage order, not runtime chronology or causality.</p>

      {investigation.state === "identified" && primary !== null && investigation.starting_point !== null && (
        <section className="insight-primary-section" aria-label="Investigation starting point">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Where to begin</p>
              <h2>Investigation starting point</h2>
            </div>
            <span className="insight-structural-label">{investigation.starting_point.kind === "sibling_group" ? "Group-level observation" : "Unique structural location"}</span>
          </div>
          <FindingCard finding={primary} primary />
          <p className="insight-epistemic-disclaimer">TraceMotive observed this difference and selected it as the first supported place to investigate. TraceMotive does not know whether the difference caused later behavior.</p>
          <LastMatched point={investigation.last_reliably_matched_point} />
        </section>
      )}

      {investigation.state === "uncertain" && orderedUncertainties.length > 0 && (
        <section className="insight-uncertainty-section" aria-label="Blocking uncertainty">
          <div className="section-heading">
            <div>
              <p className="eyebrow">What remains uncertain</p>
              <h2>TraceMotive cannot safely choose the first point</h2>
            </div>
          </div>
          <div className="insight-uncertainty-list">
            {orderedUncertainties.map((uncertainty) => <UncertaintyCard key={uncertainty.uncertainty_id} uncertainty={uncertainty} blocking={blockingIds.has(uncertainty.uncertainty_id)} />)}
          </div>
        </section>
      )}

      {investigation.state === "uncertain" && additional.length > 0 && (
        <section className="insight-additional-section" aria-label="Additional observed behavioral findings">
          <div className="section-heading"><div><p className="eyebrow">Observed after the limitation</p><h2>Additional behavioral observations</h2></div></div>
          <p className="insight-section-note">These findings remain visible, but none is promoted to a starting point.</p>
          <div className="insight-finding-list">{additional.map(({ reference, finding }) => <FindingCard key={finding.finding_id} finding={finding} relation={reference.structural_relation} />)}</div>
        </section>
      )}

      {investigation.state === "identified" && additional.length > 0 && (
        <section className="insight-additional-section" aria-label="Additional observed behavioral findings">
          <div className="section-heading"><div><p className="eyebrow">What else was observed</p><h2>Additional behavioral observations</h2></div></div>
          <div className="insight-finding-list">{additional.map(({ reference, finding }) => <FindingCard key={finding.finding_id} finding={finding} relation={reference.structural_relation} />)}</div>
        </section>
      )}

      {investigation.state === "none" && context.length === 0 && response.uncertainties.length === 0 && (
        <LastMatched point={investigation.last_reliably_matched_point} />
      )}

      {context.length > 0 && (
        <section className="insight-context-section" aria-label="Observed context changes">
          <div className="section-heading"><div><p className="eyebrow">Observed context</p><h2>Context changes</h2></div></div>
          <p className="insight-section-note">These observations provide context. They are not behavioral divergence or an investigation starting point.</p>
          <div className="insight-finding-list">{context.map((finding) => <FindingCard key={finding.finding_id} finding={finding} />)}</div>
        </section>
      )}

      {investigation.state !== "uncertain" && orderedUncertainties.length > 0 && (
        <section className="insight-uncertainty-section" aria-label="Observed uncertainty">
          <div className="section-heading"><div><p className="eyebrow">What remains uncertain</p><h2>Limitations in the available observations</h2></div></div>
          <div className="insight-uncertainty-list">{orderedUncertainties.map((uncertainty) => <UncertaintyCard key={uncertainty.uncertainty_id} uncertainty={uncertainty} blocking={blockingIds.has(uncertainty.uncertainty_id)} />)}</div>
        </section>
      )}

      <section className="insight-detail-action" aria-label="Detailed comparison">
        <div><p className="eyebrow">Evidence details</p><h2>Need the full structural comparison?</h2><p>Open the existing v0.2 detail view for all alignment records and Changed only filtering.</p></div>
        {detailsState === "loaded" ? <span className="insight-detail-open">Detailed comparison loaded below</span> : <button type="button" className="primary-button" onClick={onOpenDetails} disabled={detailsState === "loading"}>{detailsState === "loading" ? "Loading detailed comparison…" : "View detailed comparison"}</button>}
      </section>
    </section>
  );
}
