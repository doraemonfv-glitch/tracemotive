# TraceMotive v0.2 Simple Structural Alignment v1 Evaluation

## Scope and decision

This is the V02-19 validation artifact. It does not add a production comparison API, `/api/v2` route, UI, heuristic matcher, or public alignment API. It evaluates the proposed Simple Structural Alignment v1 before V02-20.

Decision: **B. Suitable with small deterministic corrections**.

The exact structural key is useful for near-identical reruns, timing variation, distinct fan-out-like branches, and unaffected branches beside a missing parent. It does not preserve human identity for repeated same-signature siblings when one call is inserted, removed, or reordered: later ordinal positions can shift into incorrect pairs. Duplicate exact sort keys are localized as ambiguity rather than guessed. These are practical alignment limitations, not causal findings.

V02-20 may proceed with the exact structural contract, provided it preserves local `ambiguous`, `left_only`, `right_only`, and `unavailable` records and surfaces repeated-sibling ordinal limitations. No heuristic matching is introduced by V02-19. Any future suggested pairing needs a separate specification and confidence layer.

## Methodology and evidence boundary

The baseline corpus was captured through the public `tracemotive.configure`, `trace`, `span`, and `flush` paths with `capture_content=False`. Events crossed a real loopback HTTP Collector and were read back from a temporary SQLite Repository. The captured agent-like execution contains an Agent root, a planning LLM, three repeated weather-tool siblings with nested normalization branches, an alert tool with a normalization child, and a synthesis LLM.

Scenario pairs are deterministic sanitized Canonical variants of that captured run. They replace execution-local and framework/native IDs independently for each run; native IDs are not supplied to the evaluator. The evaluation therefore checks the framework-independent key:

```
(semantic parent path, span type, operation, name, same-parent deterministic ordinal)
```

The ordinal is assigned within one parent and exact `(started_at in microseconds, span_id)` order. No timestamp tolerance, similarity, nearest-neighbor, input/output matching, native identity, or causal inference is used. The harness and evaluator remain private under `tracemotive._evaluation`; production comparison behavior is deferred to V02-20.

## Metrics

`matched`, `left_only`, `right_only`, `ambiguous_groups`, and `unavailable` are structural result counts. `match_coverage` is:

```
matched / max(left_total, right_total)
```

and is `1.0` when both sides contain zero spans. It is descriptive structural coverage, not causal correctness, semantic identity, or debugging success probability.

`expected/correct/incorrect/missed` in the table are evaluation-only measures. They compare matched span labels against human-expected logical occurrence labels kept outside the alignment input. They do not claim root cause, responsibility, or causal ordering.

## Scenario results

| Scenario | L/R | Matched | Left-only | Right-only | Ambiguous groups | Unavailable | Coverage | Expected / correct / incorrect / missed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `near_identical_rerun` | 11/11 | 11 | 0 | 0 | 0 | 0 | 1.000 | 11 / 11 / 0 / 0 |
| `repeated_tool_calls` | 11/11 | 11 | 0 | 0 | 0 | 0 | 1.000 | 11 / 11 / 0 / 0 |
| `added_tool_call` | 11/13 | 11 | 0 | 2 | 0 | 0 | 0.846 | 11 / 7 / 4 / 4 |
| `removed_tool_call` | 11/9 | 9 | 2 | 0 | 0 | 0 | 0.818 | 9 / 7 / 2 / 2 |
| `reordered_siblings` | 11/11 | 11 | 0 | 0 | 0 | 0 | 1.000 | 11 / 7 / 4 / 4 |
| `timing_variation` | 11/11 | 11 | 0 | 0 | 0 | 0 | 1.000 | 11 / 11 / 0 / 0 |
| `partial_early_error` | 11/6 | 6 | 5 | 0 | 0 | 0 | 0.545 | 6 / 6 / 0 / 0 |
| `missing_parent` | 11/11 | 10 | 1 | 0 | 0 | 1 | 0.909 | 11 / 10 / 0 / 1 |
| `fan_out_fan_in_like` | 11/11 | 11 | 0 | 0 | 0 | 0 | 1.000 | 11 / 11 / 0 / 0 |
| `duplicate_structural_sibling` | 11/12 | 7 | 3 | 2 | 1 | 1 | 0.583 | 11 / 7 / 0 / 4 |

## Human correspondence observations

### Near-identical rerun, repeated calls, timing, and fan-out-like flow

All 11 expected structural pairs were matched correctly in each of these scenarios. A later wall-clock offset and longer span duration did not change structural alignment. Fan-out-like sibling branches and the later synthesis span remained alignable when their structure was unchanged.

### Insertion, removal, and reorder

The inserted repeated tool produced two `right_only` spans, while the later repeated siblings shifted into four structurally matched but human-incorrect pairs. Removing the middle repeated tool produced two `left_only` spans and shifted one later repeated sibling. Reordering same-signature siblings produced four human-incorrect ordinal pairs. The evaluator records these as measured ordinal limitations; it does not repair them with heuristics.

### Partial/error run

The shorter early-error run matched the six spans that were structurally present and reported five `left_only` spans. The report contains no first-divergence, root-cause, causal-score, responsibility, or causal-order result.

### Missing parent

One nested span with a missing parent was reported as `unavailable`; the unrelated branches still produced 10 matches, including all 10 selected branch-local expected pairs. The whole comparison was not made inconclusive.

### Duplicate structural sibling

A deliberately duplicated exact structural sibling produced one local `ambiguous_groups` entry. Its affected descendants became `unavailable` because their parent identity was structurally invalid, while seven unaffected matches remained available. No arbitrary pairing was chosen.

## Security and privacy checks

- The real capture uses `capture_content=False`; the persisted evaluation spans have no captured input or output.
- The evidence path is adapter -> sanitized Canonical data -> loopback transport -> Collector -> temporary SQLite Repository.
- The evaluator consumes Canonical fields only and never receives framework objects or framework/native identity as an alignment key.
- No secret, credential, remote collector, cloud service, or external telemetry is used.
- The evaluation code is not imported by the public SDK, Collector, Query API, CLI, or frontend.

## Files and tests

- `tracemotive/_evaluation/alignment.py`: private exact-key evaluation implementation.
- `tests/alignment_evaluation.py`: realistic public-path corpus capture, deterministic scenario construction, metric collection, and report rendering.
- `tests/test_alignment_evaluation.py`: focused regression tests for the corpus, ordinal shifts, localized ambiguity/unavailability, zero-span coverage, native-ID independence, and deterministic result order.

Focused command:

```text
python -m unittest tests.test_alignment_evaluation -v
```

The full Python suite remains the release regression gate. There is no frontend change in V02-19; production UI and frontend packaging remain outside this issue.
