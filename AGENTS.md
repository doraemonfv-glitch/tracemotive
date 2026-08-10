# AgentLens Repository Instructions

## Project

AgentLens

## Long-term vision

"The causal debugger for AI agents."

## Current target

AgentLens v0.1 observation Kernel

## Authority precedence

When sources conflict, the higher-ranked source wins:

1. `spec/v0.1-frozen-spec.md` — the highest-authority contract.
2. `AGENTS.md` — repository implementation rules for Codex.
3. Issue Definition of Done within the Frozen Specification.
4. `docs/long-term-vision.md` — NON-NORMATIVE reference material only. It must never justify adding v0.1 functionality.
5. `reviews/adversarial-architecture-review.md` — NON-NORMATIVE advisory review.

## Required implementation rules

- Before implementation, read the relevant Frozen Specification sections.
- Implement Frozen behavior exactly.
- Do not implement Deferred features.
- For an Implementation Choice, choose the smallest maintainable approach.
- Do not invent a public contract that is absent from the specification.
- If the specification is ambiguous, do not implement the ambiguous behavior; report the ambiguity.

## Architecture boundary

```text
Framework
↓
Integration Adapter
↓
Canonical Trace Events
↓
Bounded Local Transport
↓
Collector
↓
SQLite
↓
Query API
↓
UI
```

- Framework objects stop at Adapter.
- SQLite stops at Backend.
- UI consumes Query API only.

## Security and privacy invariants

- AgentLens is disabled by default.
- Content capture is independently disabled by default.
- Redaction occurs before the transport queue.
- Known API keys and credentials must not be persisted.
- AgentLens must not include analytics or external telemetry.
- The HTTP collector is loopback-only.
- Never bind the documented v0.1 server to `0.0.0.0`.
- Captured Agent content is untrusted.
- The frontend never executes captured HTML or script.
- Tracing failures must never fail Agent execution.
- Debug logs must not contain secrets.

## Scope exclusions

- Replay
- RCA
- Eval
- Benchmark
- Causal Evidence Graph
- Execution Forking
- Context Delta Debugging
- Failure Fingerprinting
- Regression generation
- OTLP/OpenTelemetry export
- Distributed tracing
- Span links
- Additional framework adapters
- Cloud or hosted backend
- Authentication
- Remote collector support

## Testing rules

- Tests derive from the specification.
- Do not delete or weaken tests merely to pass them.
- The specification wins over the implementation.
- Security-sensitive changes require adversarial review.

## Change discipline

Before editing:

1. Inspect relevant files.
2. Identify applicable specification sections.
3. State intended changes.
4. Implement.
5. Run focused tests.
6. Run broader relevant tests.
7. Report evidence.

Do not opportunistically refactor unrelated code.
