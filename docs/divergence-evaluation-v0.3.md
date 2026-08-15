# TraceMotive v0.3 divergence evaluation

This document defines the private V03-10 adversarial evaluation boundary. The
corpus is an independent oracle for the future V03-11 divergence engine. It is
not production comparison code and does not change the public SDK, Canonical
schema, ingest protocol, Collector, Query API, UI, or package version.

## Three observations that must stay separate

Every scenario records three different concepts:

1. **Earliest observed difference**: the first concrete difference, missing
   observation, or unavailable structure that the pair exposes.
2. **First meaningful divergence**: product-facing terminology for the first
   behavioral divergence, according to deterministic structural triage order,
   that TraceMotive can support from available observations.
3. **Investigation starting point**: the path or group that the product selects
   as the first supported place to investigate, or an explicit uncertain state.

Here, “meaningful” means **evidence-supported behavioral divergence**. It does
not mean semantically important, task-relevant, causal, failure origin, or root
cause. TraceMotive does not determine whether a difference matters to the
user’s task or whether it produced a later failure.

The UI contract for a supported selection is:

> TraceMotive observed this difference and selected it as the first supported
> place to investigate. TraceMotive does not know whether the difference
> caused the later failure.

The UI must present unavailable or redacted evidence as uncertainty, not as a
confident behavioral result.

## Deterministic triage boundary

The corpus expects structural comparison to use the v0.2 alignment identity:
semantic parent path, type/operation/name, and same-parent ordinal. It then
uses a fixed structural order for supported candidates. Wall-clock chronology
does not override that order. Repeated same-signature insertion, removal, or
reordering may support group-level cardinality observations while prohibiting
confident identity for individual ordinal members.

Context-only changes do not nominate a behavioral starting point when the
behavioral observations are unchanged. This includes timing, duration, token
usage, model metadata, request parameters, trace status without a span-level
change, and framework metadata.

The following barriers require an uncertain outcome or no supported candidate:

- `capture_unavailable` when one side lacks the relevant content observation;
- `redacted_observation` when sanitization prevents equality or inequality of
  the original value;
- `repeated_sibling_ambiguity` when ordinal identity is not supported;
- `incomplete_trace` when an absent tail may simply be unobserved;
- `missing_parent` when a subtree cannot be attached to a known parent;
- `invalid_structure` when duplicate or otherwise invalid identity prevents
  reliable alignment.

## Closed oracle outcomes

The future engine may be graded only against these outcome classes:

- `ALLOWED_CONFIDENT`: a confident result is allowed for the scenario and
  candidate path.
- `ALLOWED_UNCERTAIN`: the engine must be allowed to surface uncertainty.
- `FORBIDDEN_CONFIDENT`: a confident result is forbidden for the listed
  candidate, even if the engine also reports other information.

V03-10 counts two failure measures separately:

- false confident meaningful divergence;
- false confident investigation starting point.

The target for both measures is zero across the mandatory corpus.

The candidate contract is intentionally strict. `allowed_candidate_paths` names
the smallest supported meaningful candidate paths for each scenario. When the
starting point is supported, `expected_starting_point_path` must equal the
single selected candidate. A later candidate may be supported as evidence while
an earlier barrier still withholds the first starting point. The
`downstream_observations` field records observed evidence only; it does not
imply a parent/child relationship or a causal chain.

The oracle also requires useful answer coverage. The current corpus has 15
scenarios with a required confident behavioral answer and 14 with a required
confident investigation starting point. A conforming release must answer all
of those cases confidently at the exact oracle candidate. It has 6 scenarios
where the meaningful-divergence result and 7 where the starting-point result
must remain safely withheld as uncertain. Zero false confidence with all 30
results uncertain is therefore insufficient.

## Mandatory scenario inventory

The private corpus contains exactly 30 scenarios. The state columns describe
the independent oracle, not an implementation result.

| Scenario | Observed | Divergence | Starting point | Main boundary |
|---|---|---|---|---|
| `identical_runs` | none | none | none | regenerated execution-local IDs are not behavior |
| `timing_only_variation` | present | none | none | timestamps and durations are context |
| `token_only_variation` | present | none | none | token usage is context |
| `model_only_change` | present | none | none | model metadata is context |
| `request_parameter_only_change` | present | none | none | request parameters are context |
| `aligned_tool_output_change` | present | supported | supported | both sanitized outputs at the unique alerts tool are captured |
| `aligned_tool_input_change` | present | supported | supported | both sanitized inputs at the unique alerts tool are captured |
| `capture_disabled_one_side` | unknown | uncertain | uncertain | `capture_unavailable` |
| `redacted_content` | present | uncertain | uncertain | `redacted_observation` |
| `new_error_exact_span` | present | supported | supported | exact aligned span status |
| `resolved_error_exact_span` | present | supported | supported | exact aligned span status |
| `trace_status_only_change` | present | none | none | trace-level status alone is context |
| `unique_tool_added` | present | supported | supported | unique structure in a complete run |
| `unique_tool_removed` | present | supported | supported | unique structure in a complete run |
| `execution_subtree_added` | present | supported | supported | observed subtree presence |
| `execution_subtree_removed` | present | supported | supported | observed subtree absence |
| `repeated_tool_insertion` | present | supported | supported | group cardinality, not member identity |
| `repeated_tool_removal` | present | supported | supported | group cardinality, not member identity |
| `repeated_tool_reordering` | present | uncertain | uncertain | `repeated_sibling_ambiguity` |
| `nested_repeated_groups` | present | uncertain | uncertain | repeated grandchildren under a unique parent; ambiguity is localized |
| `early_termination_partial_trace` | present | uncertain | uncertain | `incomplete_trace` |
| `missing_parent` | unavailable | supported | uncertain | later candidate supported, but `missing_parent` blocks the first selection |
| `duplicate_structural_id_invalid_structure` | unavailable | uncertain | uncertain | `invalid_structure` |
| `both_runs_fail_identically` | none | none | none | identical observed error evidence |
| `both_runs_fail_differently` | present | supported | supported | separately observed plan and alerts error locations |
| `error_before_later_structural_difference` | present | supported | supported | fixed structural order selects the earlier supported path |
| `multiple_independent_divergences` | present | supported | supported | plan status is selected; alerts output remains independent evidence |
| `chronological_vs_lexicographic_order` | present | supported | supported | plan structural order wins even when alerts occurred earlier |
| `framework_metadata_only_difference` | present | none | none | framework metadata is context |
| `large_irrelevant_metadata_difference` | present | none | none | irrelevant metadata must not dominate |

## Evidence construction

The baseline is captured through the existing public path used by the v0.2
alignment evaluation: public SDK configuration and spans, the loopback HTTP
Collector, a temporary SQLite repository, and a read-back of sanitized
Canonical traces. Content capture is disabled for that baseline, so the
content-enabled scenarios create deterministic sanitized Canonical variants
from that real baseline. Invalid structures and lifecycle edge cases are
direct fixtures because they are not valid inputs to construct through the
normal public path; each such case records its barrier explicitly.

The corpus and oracle live under private evaluation paths:

- `tracemotive/_evaluation/divergence.py`
- `tests/divergence_evaluation.py`
- `tests/test_divergence_evaluation.py`

No raw content or execution-local IDs are emitted by the serialized oracle.

## Validation and handoff

Run the focused evaluation with:

```text
python -m unittest tests.test_divergence_evaluation -v
python -m tests.divergence_evaluation
```

V03-10 is ready for V03-11 review only when the focused tests pass, the report
contains all 30 unique scenario names, both false-confidence counts remain
zero for a conforming outcome set, and the existing v0.2 alignment and
comparison validations remain green. The full Python suite, `git diff
--check`, and an unchanged `spec/v0.1-frozen-spec.md` are release evidence.

V03-11 must consume this oracle without treating the scenario labels as a
semantic classifier. It must preserve the explicit uncertainty barriers,
avoid confident individual identities inside ambiguous repeated groups, answer
all evidence-supported cases at their exact selected candidate, and keep the
selected starting point framed as an investigation aid.
