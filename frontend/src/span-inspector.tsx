import { isLosslessNumber } from "lossless-json";
import { formatDuration } from "./span-tree";
import type {
  CanonicalJsonValue,
  LLMDetails,
  SpanDetail,
  SpanIdentity,
  SpanDetails,
  SpanType,
} from "./types";

export type SpanInspectorState =
  | { kind: "no-selection" }
  | { kind: "loading"; identity: SpanIdentity }
  | { kind: "loaded"; identity: SpanIdentity; span: SpanDetail }
  | { kind: "not-found"; identity: SpanIdentity }
  | { kind: "error"; identity: SpanIdentity };

type JsonFrame =
  | { kind: "value"; value: unknown; ancestors: Set<object> }
  | { kind: "text"; value: string }
  | { kind: "leave"; value: object; ancestors: Set<object> };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) && !isLosslessNumber(value);
}

function scalarText(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (isLosslessNumber(value)) {
    return value.toString();
  }
  if (typeof value === "number") {
    if (Object.is(value, -0)) {
      return "-0";
    }
    return Number.isFinite(value) ? String(value) : String(value);
  }
  return "[Unsupported JSON value]";
}

/** Formats decoded Canonical JSON iteratively, preserving lossless numbers and received object order. */
export function formatJsonValue(value: CanonicalJsonValue): string {
  const output: string[] = [];
  const rootAncestors = new Set<object>();
  const stack: JsonFrame[] = [{ kind: "value", value, ancestors: rootAncestors }];

  while (stack.length > 0) {
    const frame = stack.pop();
    if (frame === undefined) {
      continue;
    }
    if (frame.kind === "text") {
      output.push(frame.value);
      continue;
    }
    if (frame.kind === "leave") {
      frame.ancestors.delete(frame.value);
      continue;
    }

    const current = frame.value;
    if (Array.isArray(current)) {
      if (frame.ancestors.has(current)) {
        output.push("[Circular]");
        continue;
      }
      frame.ancestors.add(current);
      stack.push({ kind: "leave", value: current, ancestors: frame.ancestors });
      stack.push({ kind: "text", value: "]" });
      for (let index = current.length - 1; index >= 0; index -= 1) {
        if (index < current.length - 1) {
          stack.push({ kind: "text", value: ", " });
        }
        stack.push({ kind: "value", value: current[index], ancestors: frame.ancestors });
      }
      stack.push({ kind: "text", value: "[" });
      continue;
    }
    if (isRecord(current)) {
      if (frame.ancestors.has(current)) {
        output.push("[Circular]");
        continue;
      }
      const keys = Object.keys(current);
      frame.ancestors.add(current);
      stack.push({ kind: "leave", value: current, ancestors: frame.ancestors });
      stack.push({ kind: "text", value: "}" });
      for (let index = keys.length - 1; index >= 0; index -= 1) {
        if (index < keys.length - 1) {
          stack.push({ kind: "text", value: ", " });
        }
        stack.push({ kind: "value", value: current[keys[index]], ancestors: frame.ancestors });
        stack.push({ kind: "text", value: ": " });
        stack.push({ kind: "text", value: JSON.stringify(keys[index]) });
      }
      stack.push({ kind: "text", value: "{" });
      continue;
    }
    output.push(scalarText(current));
  }

  return output.join("");
}

function identityText(identity: SpanIdentity): string {
  return `${identity.trace_id} / ${identity.span_id}`;
}

function statusLabel(status: SpanDetail["status"]): string {
  return { unset: "Unset", ok: "OK", error: "Error" }[status];
}

function nullableText(value: string | null): string {
  return value === null ? "Unknown" : value;
}

function captureReasonLabel(reason: NonNullable<SpanDetail["capture"]["input"]["reason"]>): string {
  return reason.replaceAll("_", " ");
}

function FieldList({ fields }: { fields: Array<[string, string]> }) {
  return (
    <dl className="inspector-fields">
      {fields.map(([label, value]) => (
        <div className="inspector-field" key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function JsonData({ label, value }: { label: string; value: CanonicalJsonValue }) {
  return (
    <pre className="inspector-json" aria-label={`${label} JSON value`}>
      {formatJsonValue(value)}
    </pre>
  );
}

function CaptureSection({ label, value, capture }: { label: string; value: CanonicalJsonValue; capture: SpanDetail["capture"]["input"] }) {
  const captured = capture.state === "captured";
  return (
    <section className="inspector-section" aria-labelledby={`inspector-${label.toLowerCase()}-heading`}>
      <h3 id={`inspector-${label.toLowerCase()}-heading`}>{label}</h3>
      <p className="inspector-capture-state">
        {captured
          ? value === null
            ? "Captured JSON null"
            : `Captured${capture.redacted ? " (redacted)" : ""}`
          : `Not captured: ${captureReasonLabel(capture.reason!)}`}
      </p>
      {captured && <JsonData label={label} value={value} />}
    </section>
  );
}

function LLMSection({ details }: { details: LLMDetails }) {
  const usage = details.usage;
  return (
    <section className="inspector-section" aria-labelledby="inspector-llm-heading">
      <h3 id="inspector-llm-heading">LLM details</h3>
      <FieldList
        fields={[
          ["Provider", nullableText(details.provider)],
          ["Request model", nullableText(details.request_model)],
          ["Response model", nullableText(details.response_model)],
          ["Response ID", nullableText(details.response_id)],
          ["Input tokens", usage.input_tokens === null ? "Unknown" : usage.input_tokens.toLocaleString("en-US")],
          ["Output tokens", usage.output_tokens === null ? "Unknown" : usage.output_tokens.toLocaleString("en-US")],
          ["Reasoning output tokens", usage.reasoning_output_tokens === null ? "Unknown" : usage.reasoning_output_tokens.toLocaleString("en-US")],
          ["Cache read input tokens", usage.cache_read_input_tokens === null ? "Unknown" : usage.cache_read_input_tokens.toLocaleString("en-US")],
          ["Cache creation input tokens", usage.cache_creation_input_tokens === null ? "Unknown" : usage.cache_creation_input_tokens.toLocaleString("en-US")],
        ]}
      />
      <div className="inspector-subsection">
        <h4>Finish reasons</h4>
        <JsonData label="Finish reasons" value={details.finish_reasons} />
      </div>
      <div className="inspector-subsection">
        <h4>Request parameters</h4>
        {details.request_parameters === null ? <p className="inspector-muted">Unknown</p> : <JsonData label="Request parameters" value={details.request_parameters} />}
      </div>
      {details.estimated_cost !== null && (
        <div className="inspector-subsection">
          <h4>Estimated cost</h4>
          <FieldList fields={[["Amount", formatJsonValue(details.estimated_cost.amount)], ["Currency", details.estimated_cost.currency], ["Estimated", "true"]]} />
        </div>
      )}
    </section>
  );
}

function DetailsSection({ details }: { details: SpanDetails }) {
  switch (details.kind) {
    case "agent":
      return <section className="inspector-section" aria-labelledby="inspector-agent-heading"><h3 id="inspector-agent-heading">Agent details</h3><FieldList fields={[["Agent name", nullableText(details.agent_name)], ["Agent version", nullableText(details.agent_version)]]} /></section>;
    case "llm":
      return <LLMSection details={details} />;
    case "tool":
      return <section className="inspector-section" aria-labelledby="inspector-tool-heading"><h3 id="inspector-tool-heading">Tool details</h3><FieldList fields={[["Tool name", details.tool_name], ["Tool call ID", nullableText(details.tool_call_id)]]} /></section>;
    case "handoff":
      return <section className="inspector-section" aria-labelledby="inspector-handoff-heading"><h3 id="inspector-handoff-heading">Handoff details</h3><FieldList fields={[["From agent", nullableText(details.from_agent)], ["To agent", nullableText(details.to_agent)]]} /></section>;
    case "retrieval":
      return <section className="inspector-section" aria-labelledby="inspector-retrieval-heading"><h3 id="inspector-retrieval-heading">Retrieval details</h3><p className="inspector-muted">No additional Frozen v0.1 retrieval fields.</p></section>;
    case "custom":
      return <section className="inspector-section" aria-labelledby="inspector-custom-heading"><h3 id="inspector-custom-heading">Custom details</h3><FieldList fields={[["Source type", nullableText(details.source_type)]]} /></section>;
  }
}

function LoadedInspector({ span }: { span: SpanDetail }) {
  const error = span.error;
  return (
    <>
      <section className="inspector-section" aria-labelledby="inspector-identity-heading">
        <h3 id="inspector-identity-heading">Identity</h3>
        <FieldList fields={[["Trace ID", span.trace_id], ["Span ID", span.span_id], ["Parent", span.parent_span_id === null ? "Root span" : span.parent_span_id]]} />
      </section>
      <section className="inspector-section" aria-labelledby="inspector-common-heading">
        <h3 id="inspector-common-heading">Span</h3>
        <FieldList fields={[["Name", span.name], ["Type", span.type], ["Operation", span.operation], ["Status", statusLabel(span.status)], ["Start", span.started_at], ["End", span.ended_at === null ? "Not ended" : span.ended_at], ["Duration", formatDuration(span.latency_ms)]]} />
        {span.status === "ok" && error !== null && <p className="inspector-warning" role="note">Observed status is OK while Error evidence is present.</p>}
      </section>
      <CaptureSection label="Input" value={span.input} capture={span.capture.input} />
      <CaptureSection label="Output" value={span.output} capture={span.capture.output} />
      <DetailsSection details={span.details} />
      {(error !== null || span.status === "error") && (
        <section className="inspector-section" aria-labelledby="inspector-error-heading">
          <h3 id="inspector-error-heading">Error</h3>
          {error === null ? <p className="inspector-muted">No canonical error details were recorded.</p> : <FieldList fields={[["Error type", nullableText(error.type)], ["Error message", nullableText(error.message)]]} />}
        </section>
      )}
    </>
  );
}

export function SpanInspector({ state }: { state: SpanInspectorState }) {
  return (
    <aside className="span-inspector" aria-labelledby="span-inspector-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Selected canonical span</p>
          <h2 id="span-inspector-heading">Span inspector</h2>
        </div>
        {state.kind === "loaded" && <span className="section-count">{state.span.type}</span>}
      </div>

      {state.kind === "no-selection" && <section className="state-message empty-state" aria-live="polite"><p>Select a span to inspect its canonical details.</p></section>}
      {state.kind === "loading" && <section className="state-message" aria-live="polite" aria-busy="true"><p>Loading selected span...</p><code>{identityText(state.identity)}</code></section>}
      {state.kind === "not-found" && <section className="state-message empty-state" aria-live="polite"><p>Span unavailable or not found.</p><code>{identityText(state.identity)}</code></section>}
      {state.kind === "error" && <section className="state-message state-error" role="alert"><p>Unable to load selected span.</p></section>}
      {state.kind === "loaded" && <div className="inspector-content"><LoadedInspector span={state.span} /></div>}
    </aside>
  );
}
