import { useMemo, useState } from "react";
import type {
  CanonicalJsonValue,
  ComparisonSpanRef,
  InvestigationFinding,
  InvestigationFindingType,
  InvestigationSummaryView,
  InvestigationUncertainty,
  StructuredDiffObservation,
  StructuredDiffRecord,
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

const UNCERTAINTY_LABELS: Record<string, string> = {
  repeated_sibling_ambiguity: "Repeated members are ambiguous",
  capture_unavailable: "Captured content is unavailable",
  redacted_observation: "The observation was redacted",
  missing_parent: "A parent observation is missing",
  invalid_structure: "The trace structure could not be resolved safely",
  incomplete_trace: "One trace ended before the observed execution was complete",
  ambiguous_parent: "The parent location is ambiguous",
  cycle: "The trace contains a structural cycle",
  unsupported_observation: "This observation type is not supported for a safe comparison",
};

const FIELD_LABELS: Record<string, string> = {
  input: "tool or span input",
  output: "tool or span output",
  error: "error evidence",
  status: "status",
  latency_ms: "duration",
  "details.request_model": "request model",
  "details.response_model": "response model",
  "details.request_parameters": "request parameters",
};

const MAX_INLINE_VALUE_LENGTH = 900;

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

function findingLabel(type: InvestigationFindingType): string {
  return FINDING_LABELS[type];
}

function uncertaintyLabel(reasonCode: string): string {
  return UNCERTAINTY_LABELS[reasonCode] ?? "TraceMotive could not safely resolve this observation";
}

function stateLabel(state: InvestigationSummaryView["state"]): string {
  return state[0].toUpperCase() + state.slice(1);
}

function pathLabel(finding: InvestigationFinding): string {
  const segments = finding.coordinate.semantic_path.map((segment) => `${segment.type} / ${segment.name}`);
  if (finding.coordinate.kind === "sibling_group" && finding.coordinate.group_signature !== null) {
    segments.push(`Repeated group / ${finding.coordinate.group_signature.name}`);
  }
  return segments.join(" / ") || "Trace root";
}

function fieldLabel(fieldPath: string | null): string | null {
  if (fieldPath === null) {
    return null;
  }
  const normalized = fieldPath.replace(/^\/+/, "");
  return FIELD_LABELS[normalized] ?? null;
}

function changedDescription(finding: InvestigationFinding): string {
  const field = fieldLabel(finding.field_path);
  return field === null
    ? `${findingLabel(finding.type)} was observed at this structural location.`
    : `Observed ${field} differs between the left and right trace.`;
}

function structuredDiffValue(observation: StructuredDiffObservation): string {
  if (observation.state !== "present") {
    return observation.state === "absent" ? "Absent" : observation.state.replaceAll("_", " ");
  }
  return formatValue(observation.value ?? null);
}

function StructuredDiffView({ finding }: { finding: InvestigationFinding }) {
  if (finding.structured_diff_available === false) {
    const reason = finding.structured_diff_reason?.replaceAll("_", " ") ?? "not available";
    return <p className="cockpit-muted">Detailed field comparison unavailable: {reason}.</p>;
  }
  if (finding.structured_diff_available !== true || finding.structured_diff === undefined) {
    return null;
  }
  return (
    <div className="cockpit-structured-diff" aria-label="Structured field differences">
      <p className="comparison-side-label">Structured field differences</p>
      {finding.structured_diff.length === 0 && <p className="cockpit-muted">No bounded field records were emitted.</p>}
      {finding.structured_diff.map((record: StructuredDiffRecord) => (
        <div className="cockpit-structured-diff-record" key={`${record.op}:${record.path}`}>
          <code>{record.path || "(root)"}</code>
          <strong>{record.op}</strong>
          <span><b>Left</b> {structuredDiffValue(record.left)}</span>
          <span><b>Right</b> {structuredDiffValue(record.right)}</span>
          {record.reason !== null && <small>{record.reason.replaceAll("_", " ")}</small>}
        </div>
      ))}
      {finding.structured_diff_truncated === true && (
        <p className="cockpit-muted">The structured field view was truncated: {finding.structured_diff_reason?.replaceAll("_", " ") ?? "bounded limit reached"}.</p>
      )}
    </div>
  );
}

function sideObserved(finding: InvestigationFinding, side: "left" | "right"): { state: string; value: CanonicalJsonValue | null } | null {
  const value = finding.observed[side];
  if (!isRecord(value) || typeof value.state !== "string") {
    return null;
  }
  return { state: value.state, value: ("value" in value ? value.value : null) as CanonicalJsonValue | null };
}

function observationLabel(observation: { state: string; value: CanonicalJsonValue | null } | null): string {
  if (observation === null) {
    return "Not observed";
  }
  switch (observation.state) {
    case "captured":
      return "Captured";
    case "redacted":
      return "Redacted; source value not shown";
    case "not_captured":
    case "unavailable":
      return "Unavailable; source value was not captured";
    default:
      return observation.state.replaceAll("_", " ");
  }
}

function targetText(target: ComparisonSpanRef): string {
  return `${target.trace_id} / ${target.span_id}`;
}

function referenceHash(response: TraceInsightResponse): string {
  return `#/compare/${encodeURIComponent(response.left_trace.trace_id)}/${encodeURIComponent(response.right_trace.trace_id)}`;
}

function limitationText(uncertainty: InvestigationUncertainty): string {
  const side = uncertainty.side === "both" ? "both traces" : `the ${uncertainty.side} trace`;
  return `${uncertaintyLabel(uncertainty.reason_code)} on ${side}.`;
}

function EvidenceValue({ label, value }: { label: string; value: CanonicalJsonValue | null }) {
  const formatted = useMemo(() => formatValue(value), [value]);
  if (formatted.length > MAX_INLINE_VALUE_LENGTH) {
    return (
      <details className="cockpit-value-disclosure">
        <summary>{label}: large value, expand to inspect</summary>
        <pre>{formatted}</pre>
      </details>
    );
  }
  return <pre aria-label={`${label} evidence`}>{formatted}</pre>;
}

function ObservationEvidence({ finding }: { finding: InvestigationFinding }) {
  const left = sideObserved(finding, "left");
  const right = sideObserved(finding, "right");
  return (
    <div className="cockpit-observation-grid" aria-label="Observed evidence by side">
      <div className="cockpit-observation-side">
        <span className="comparison-side-label">Left observation</span>
        <strong>{observationLabel(left)}</strong>
        {left?.state === "captured" && <EvidenceValue label="Left" value={left.value} />}
      </div>
      <div className="cockpit-observation-side">
        <span className="comparison-side-label">Right observation</span>
        <strong>{observationLabel(right)}</strong>
        {right?.state === "captured" && <EvidenceValue label="Right" value={right.value} />}
      </div>
    </div>
  );
}

function TargetLocation({ label, target }: { label: string; target: ComparisonSpanRef | null }) {
  return (
    <div className="cockpit-target-location">
      <span className="comparison-side-label">{label}</span>
      {target === null ? <span>Not observed at an individual span</span> : <code>{targetText(target)}</code>}
    </div>
  );
}

function LastMatchedEvidence({ point }: { point: InvestigationSummaryView["last_reliably_matched_point"] }) {
  if (point.state !== "matched") {
    return <p className="cockpit-muted">No prior reliable structural match was available.</p>;
  }
  const path = point.semantic_path.map((segment) => `${segment.type} / ${segment.name}`).join(" / ") || "Trace root";
  return (
    <div className="cockpit-last-match">
      <span className="comparison-side-label">Last reliably matched point</span>
      <strong>{path}</strong>
      <p>Structural evidence only; this does not describe runtime chronology.</p>
    </div>
  );
}

function FindingEvidenceDetails({ finding }: { finding: InvestigationFinding }) {
  if (finding.evidence.length === 0) {
    return null;
  }
  const observations = [sideObserved(finding, "left"), sideObserved(finding, "right")];
  if (observations.some((observation) => observation?.state === "redacted" || observation?.state === "not_captured" || observation?.state === "unavailable")) {
    return <p className="cockpit-muted">Detailed evidence records are not shown because captured content is redacted or unavailable.</p>;
  }
  return (
    <details className="cockpit-evidence-details">
      <summary>Observed evidence records</summary>
      <div className="cockpit-evidence-records">
        {finding.evidence.map((item, index) => <pre key={`${finding.finding_id}-evidence-${index}`}>{formatValue(item)}</pre>)}
      </div>
    </details>
  );
}

function CopyActions({ response, evidenceText }: { response: TraceInsightResponse; evidenceText: string }) {
  const [copyState, setCopyState] = useState<"idle" | "evidence-copied" | "reference-copied" | "failed">("idle");
  const [fallbackText, setFallbackText] = useState("");

  const copy = async (text: string, success: "evidence-copied" | "reference-copied") => {
    try {
      if (navigator.clipboard === undefined || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(text);
      setCopyState(success);
    } catch {
      setFallbackText(text);
      setCopyState("failed");
    }
  };

  return (
    <div className="cockpit-secondary-actions">
      <button type="button" className="secondary-button" onClick={() => void copy(evidenceText, "evidence-copied")}>Copy evidence</button>
      <button type="button" className="secondary-button" onClick={() => void copy(referenceHash(response), "reference-copied")}>Copy local reference</button>
      {copyState === "evidence-copied" && <span role="status">Evidence copied.</span>}
      {copyState === "reference-copied" && <span role="status">Local reference copied.</span>}
      {copyState === "failed" && (
        <div className="cockpit-copy-fallback" role="status">
          <span>Clipboard unavailable. Select the text below.</span>
          <pre>{fallbackText}</pre>
        </div>
      )}
    </div>
  );
}

export function ComparisonInsight({
  response,
  onOpenDetails,
  onOpenSpan,
  detailsState,
}: {
  response: TraceInsightResponse;
  onOpenDetails: () => void;
  onOpenSpan?: (traceId: string, spanId: string) => void;
  detailsState: "closed" | "loading" | "loaded" | "not-found" | "invalid" | "too-large" | "error";
}) {
  const { investigation } = response;
  const findingsById = new Map(response.findings.map((finding) => [finding.finding_id, finding]));
  const primary = investigation.starting_point === null ? null : findingsById.get(investigation.starting_point.finding_id) ?? null;
  const evidenceFinding = primary ?? investigation.evidence_summary
    .map((reference) => findingsById.get(reference.finding_id))
    .find((finding): finding is InvestigationFinding => finding !== undefined) ?? null;
  const uncertaintyById = new Map(response.uncertainties.map((uncertainty) => [uncertainty.uncertainty_id, uncertainty]));
  const limitationIds = [...new Set([
    ...investigation.limitations.map((limitation) => limitation.uncertainty_id),
    ...investigation.blocking_uncertainty_ids,
  ])];
  const limitations = limitationIds
    .map((uncertaintyId) => uncertaintyById.get(uncertaintyId))
    .filter((uncertainty): uncertainty is InvestigationUncertainty => uncertainty !== undefined);
  const evidenceText = [
    `State: ${stateLabel(investigation.state)}`,
    evidenceFinding === null ? "What changed: No supported behavioral divergence was selected." : `What changed: ${changedDescription(evidenceFinding)}`,
    evidenceFinding === null ? "" : `Left: ${observationLabel(sideObserved(evidenceFinding, "left"))}`,
    evidenceFinding === null ? "" : `Right: ${observationLabel(sideObserved(evidenceFinding, "right"))}`,
  ].filter((line) => line.length > 0).join("\n");

  const openTarget = (target: ComparisonSpanRef | null) => {
    if (target !== null) {
      onOpenSpan?.(target.trace_id, target.span_id);
    }
  };

  return (
    <article className={`comparison-insight cockpit-state-${investigation.state}`} aria-label="Investigation cockpit">
      <section className="cockpit-section" aria-labelledby="cockpit-look-here-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Investigation</p>
            <h2 id="cockpit-look-here-heading">Look here</h2>
          </div>
          <span className={`cockpit-state-badge cockpit-state-badge-${investigation.state}`}>{stateLabel(investigation.state)}</span>
        </div>
        {investigation.state === "identified" && primary !== null && investigation.starting_point !== null ? (
          <div className="cockpit-primary-card">
            <h3>{findingLabel(primary.type)}</h3>
            <p className="cockpit-path">{pathLabel(primary)}</p>
            <p>{investigation.starting_point.kind === "sibling_group" ? "A repeated group changed; no individual member identity was inferred." : "This is the supported structural place to inspect first."}</p>
            <div className="cockpit-target-grid">
              <TargetLocation label="Left span" target={investigation.starting_point.left} />
              <TargetLocation label="Right span" target={investigation.starting_point.right} />
            </div>
          </div>
        ) : investigation.state === "uncertain" ? (
          <div className="cockpit-primary-card">
            <h3>No supported starting point</h3>
            <p>TraceMotive observed a limitation that prevents it from safely selecting the first place to inspect.</p>
            {limitations.length > 0 && <ul className="cockpit-limitation-list">{limitations.map((uncertainty) => <li key={uncertainty.uncertainty_id}>{limitationText(uncertainty)}</li>)}</ul>}
          </div>
        ) : (
          <div className="cockpit-primary-card">
            <h3>No supported starting point</h3>
            <p>No supported behavioral divergence was found in the available observations.</p>
          </div>
        )}
        <p className="cockpit-order-note">The starting point follows deterministic structural triage order, not runtime chronology.</p>
      </section>

      <section className="cockpit-section" aria-labelledby="cockpit-what-changed-heading">
        <div className="section-heading"><div><h2 id="cockpit-what-changed-heading">What changed</h2></div></div>
        {primary !== null && investigation.state === "identified" ? (
          <div className="cockpit-change-summary">
            <strong>{findingLabel(primary.type)}</strong>
            <p>{changedDescription(primary)}</p>
            <StructuredDiffView finding={primary} />
          </div>
        ) : investigation.state === "uncertain" && evidenceFinding !== null ? (
          <div className="cockpit-change-summary"><p>Other observed difference: <strong>{findingLabel(evidenceFinding.type)}</strong>. It was not promoted to a starting point.</p></div>
        ) : (
          <div className="cockpit-change-summary"><p>No supported behavioral divergence was found in the available observations.</p></div>
        )}
      </section>

      <section className="cockpit-section" aria-labelledby="cockpit-evidence-heading">
        <div className="section-heading"><div><h2 id="cockpit-evidence-heading">Evidence</h2></div></div>
        {evidenceFinding !== null && <ObservationEvidence finding={evidenceFinding} />}
        {evidenceFinding !== null && <FindingEvidenceDetails finding={evidenceFinding} />}
        {limitations.length > 0 && (
          <ul className="cockpit-evidence-limitations">
            {limitations.map((uncertainty) => <li key={uncertainty.uncertainty_id}>{limitationText(uncertainty)}</li>)}
          </ul>
        )}
        {evidenceFinding === null && limitations.length === 0 && <p className="cockpit-muted">No additional evidence was available for this result.</p>}
        <LastMatchedEvidence point={investigation.last_reliably_matched_point} />
        <CopyActions response={response} evidenceText={evidenceText} />
      </section>

      <section className="cockpit-section" aria-labelledby="cockpit-next-heading">
        <div className="section-heading"><div><h2 id="cockpit-next-heading">Next</h2></div></div>
        <div className="cockpit-primary-actions">
          {investigation.state === "identified" && investigation.starting_point?.left !== null && investigation.starting_point?.left !== undefined && (
            <button type="button" className="primary-button" onClick={() => openTarget(investigation.starting_point!.left)}>Open left span</button>
          )}
          {investigation.state === "identified" && investigation.starting_point?.right !== null && investigation.starting_point?.right !== undefined && (
            <button type="button" className="primary-button" onClick={() => openTarget(investigation.starting_point!.right)}>Open right span</button>
          )}
          <button type="button" className="primary-button" onClick={onOpenDetails} disabled={detailsState === "loading"} aria-busy={detailsState === "loading"}>
            Full comparison
          </button>
        </div>
        {detailsState === "loaded" && <p className="cockpit-muted" role="status">Full comparison is shown below.</p>}
      </section>

      <section className="cockpit-section" aria-labelledby="cockpit-unknown-heading">
        <div className="section-heading"><div><h2 id="cockpit-unknown-heading">What TraceMotive does not know</h2></div></div>
        <p>This observed divergence is not proof of cause.</p>
        {investigation.state === "identified" && <p>TraceMotive does not know why the traces differ or whether this observation affected later behavior.</p>}
        {investigation.state === "uncertain" && <p>TraceMotive does not know a safe first investigation point because the available evidence is limited.</p>}
        {investigation.state === "none" && <p>TraceMotive does not know of a supported behavioral divergence in the available observations.</p>}
      </section>
    </article>
  );
}
