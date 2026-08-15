"""Private V03-10 divergence corpus and oracle.

This module is an evaluation artifact, not a production comparison engine.  It
contains deterministic Canonical fixtures, explicit safe-oracle expectations,
and report helpers for V03-10.  It is not imported by the public SDK,
Collector, Query API, CLI, or frontend.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Callable, Iterable, Literal, Mapping, Sequence

from tracemotive.canonical import (
    Capture,
    CaptureInfo,
    Error,
    LLMDetails,
    Span,
    Trace,
    ToolDetails,
)
from tracemotive.storage.repository import timestamp_to_us, us_to_timestamp


ObservedState = Literal["none", "present", "unknown", "unavailable"]
DivergenceState = Literal["none", "supported", "uncertain"]
StartingPointState = Literal["none", "supported", "uncertain"]
OutcomeClass = Literal["ALLOWED_CONFIDENT", "ALLOWED_UNCERTAIN"]

OBSERVED_STATES = frozenset({"none", "present", "unknown", "unavailable"})
DIVERGENCE_STATES = frozenset({"none", "supported", "uncertain"})
STARTING_POINT_STATES = frozenset({"none", "supported", "uncertain"})
OUTCOME_CLASSES = frozenset({"ALLOWED_CONFIDENT", "ALLOWED_UNCERTAIN", "FORBIDDEN_CONFIDENT"})

BASELINE_EPOCH_US = timestamp_to_us("2026-08-15T00:00:00.000000Z")


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """A sanitized trace pair member used only by the evaluation corpus."""

    trace: Trace
    spans: tuple[Span, ...]
    labels: dict[str, str]
    order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DivergenceScenario:
    """One pair and its independent, safe expected outcome."""

    name: str
    left_description: str
    right_description: str
    left: EvaluationRun
    right: EvaluationRun
    observed_difference: ObservedState
    meaningful_divergence: DivergenceState
    investigation_starting_point: StartingPointState
    expected_starting_point_path: str | None
    allowed_candidate_paths: tuple[str, ...]
    forbidden_confident_candidates: tuple[str, ...]
    uncertainty_barrier: str | None
    downstream_observations: tuple[str, ...]
    content_evidence: str
    left_complete: bool
    right_complete: bool
    notes: str
    allowed_outcomes: tuple[OutcomeClass, ...]

    def __post_init__(self) -> None:
        if self.observed_difference not in OBSERVED_STATES:
            raise ValueError(f"unknown observed state: {self.observed_difference}")
        if self.meaningful_divergence not in DIVERGENCE_STATES:
            raise ValueError(f"unknown divergence state: {self.meaningful_divergence}")
        if self.investigation_starting_point not in STARTING_POINT_STATES:
            raise ValueError(f"unknown starting-point state: {self.investigation_starting_point}")
        if not self.forbidden_confident_candidates:
            raise ValueError(f"{self.name} must state forbidden confident candidates")
        if self.investigation_starting_point == "supported" and self.expected_starting_point_path is None:
            raise ValueError(f"{self.name} needs a supported starting-point path")
        if (
            self.investigation_starting_point == "supported"
            and self.expected_starting_point_path not in self.allowed_candidate_paths
        ):
            raise ValueError(f"{self.name} starting-point path is not an allowed candidate")
        if self.meaningful_divergence != "supported" and self.allowed_candidate_paths:
            raise ValueError(f"{self.name} cannot allow candidates without a supported divergence")
        for run, complete, side in (
            (self.left, self.left_complete, "left"),
            (self.right, self.right_complete, "right"),
        ):
            observed_complete = run.trace.ended_at is not None and run.trace.status != "unset"
            if complete != observed_complete:
                raise ValueError(f"{self.name} {side}_complete disagrees with observable Trace lifecycle")
        for outcome in self.allowed_outcomes:
            if outcome not in {"ALLOWED_CONFIDENT", "ALLOWED_UNCERTAIN"}:
                raise ValueError(f"{self.name} has a forbidden allowed outcome: {outcome}")

    @property
    def left_span_count(self) -> int:
        return len(self.left.spans)

    @property
    def right_span_count(self) -> int:
        return len(self.right.spans)

    def to_dict(self) -> dict[str, object]:
        """Serialize oracle metadata without raw content or execution IDs."""

        return {
            "name": self.name,
            "left_description": self.left_description,
            "right_description": self.right_description,
            "left_span_count": self.left_span_count,
            "right_span_count": self.right_span_count,
            "left_complete": self.left_complete,
            "right_complete": self.right_complete,
            "observed_difference": self.observed_difference,
            "meaningful_divergence": self.meaningful_divergence,
            "investigation_starting_point": self.investigation_starting_point,
            "expected_starting_point_path": self.expected_starting_point_path,
            "allowed_candidate_paths": list(self.allowed_candidate_paths),
            "forbidden_confident_candidates": list(self.forbidden_confident_candidates),
            "uncertainty_barrier": self.uncertainty_barrier,
            "downstream_observations": list(self.downstream_observations),
            "content_evidence": self.content_evidence,
            "notes": self.notes,
            "allowed_outcomes": list(self.allowed_outcomes),
        }


@dataclass(frozen=True, slots=True)
class ProductionOutcome:
    """Minimal future-engine result shape used to count false confidence."""

    scenario_name: str
    meaningful_confident: bool
    starting_point_confident: bool
    candidate_path: str | None


@dataclass(frozen=True, slots=True)
class FalseConfidenceCounts:
    meaningful_divergence: int
    investigation_starting_point: int
    expected_confident_meaningful: int
    correctly_confident_meaningful: int
    expected_confident_starting_point: int
    correctly_confident_starting_point: int
    expected_uncertain_meaningful: int
    safely_withheld_meaningful: int
    expected_uncertain_starting_point: int
    safely_withheld_starting_point: int
    missing_outcomes: int


SpanMutator = Callable[[Span], Span]
TraceMutator = Callable[[Trace], Trace]


def _stable_id(prefix: str, value: str, length: int) -> str:
    import hashlib

    return hashlib.sha256(f"{prefix}:{value}".encode("utf-8")).hexdigest()[:length]


def _base_maps(run: EvaluationRun) -> tuple[dict[str, Span], dict[str, str | None]]:
    by_label = {run.labels[span.span_id]: span for span in run.spans}
    by_span_id = {span.span_id: label for label, span in by_label.items()}
    parent_by_label: dict[str, str | None] = {}
    for label, span in by_label.items():
        parent_by_label[label] = None if span.parent_span_id is None else by_span_id.get(span.parent_span_id)
    return by_label, parent_by_label


def clone_run(
    base: EvaluationRun,
    run_name: str,
    *,
    order: Iterable[str] | None = None,
    drop: Iterable[str] = (),
    extras: tuple[tuple[str, str, str | None], ...] = (),
    timing_offset_us: int = 0,
    duration_us: int = 50_000,
    status: str = "ok",
    error_labels: Iterable[str] = (),
    incomplete: bool = False,
    missing_parent_labels: Iterable[str] = (),
    duplicate_label: str | None = None,
    timing_overrides_us: Mapping[str, int] | None = None,
    span_mutators: Mapping[str, SpanMutator] | None = None,
    trace_mutator: TraceMutator | None = None,
) -> EvaluationRun:
    """Create a deterministic sanitized Canonical variant.

    This is fixture construction only.  It does not call a production
    comparison path and deliberately permits invalid persisted shapes when a
    scenario needs to exercise invalid-structure handling.
    """

    by_label, parent_by_label = _base_maps(base)
    dropped = set(drop)
    selected = [label for label in (base.order if order is None else order) if label not in dropped]
    extra_by_label = {label: (template, parent) for label, template, parent in extras}
    selected.extend(label for label, _, _ in extras if label not in selected and label not in dropped)

    specs: list[tuple[str, str, str | None]] = []
    for label in selected:
        if label in extra_by_label:
            template, parent_override = extra_by_label[label]
            specs.append((label, template, parent_override))
        else:
            specs.append((label, label, parent_by_label.get(label)))

    trace_id = _stable_id("trace", run_name, 32)
    span_ids = {label: _stable_id("span", f"{run_name}:{label}", 16) for label, _, _ in specs}
    missing = set(missing_parent_labels)
    rank = {label: index for index, (label, _, _) in enumerate(specs)}
    timing_overrides = dict(timing_overrides_us or {})
    errors = set(error_labels)
    mutators = dict(span_mutators or {})
    spans: list[Span] = []

    for label, template_label, parent_label in specs:
        template = by_label[template_label]
        if label in missing:
            parent_id = "deadbeefdeadbeef"
        else:
            parent_id = None if parent_label is None else span_ids.get(parent_label)
        started_us = timing_overrides.get(
            label,
            BASELINE_EPOCH_US + timing_offset_us + rank[label] * 100_000,
        )
        cloned = replace(
            template,
            trace_id=trace_id,
            span_id=span_ids[label],
            parent_span_id=parent_id,
            started_at=us_to_timestamp(started_us),
            ended_at=us_to_timestamp(started_us + duration_us),
        )
        if label in errors:
            cloned = replace(
                cloned,
                status="error",
                error=Error("EvaluationError", "observed evaluation error"),
            )
        if label in mutators:
            cloned = mutators[label](cloned)
        spans.append(cloned)

    if duplicate_label is not None:
        duplicate = next(span for span in spans if span_ids.get(duplicate_label) == span.span_id)
        spans.append(duplicate)

    trace = replace(
        base.trace,
        trace_id=trace_id,
        started_at=us_to_timestamp(BASELINE_EPOCH_US + timing_offset_us),
        ended_at=None if incomplete else us_to_timestamp(BASELINE_EPOCH_US + timing_offset_us + (len(specs) + 2) * 100_000),
        status="unset" if incomplete else status,
        source=replace(base.trace.source, native_trace_id=f"native-trace-{run_name}"),
    )
    if trace_mutator is not None:
        trace = trace_mutator(trace)
    labels = {span_ids[label]: label for label, _, _ in specs}
    return EvaluationRun(trace, tuple(spans), labels, tuple(selected))


def _captured_input(value: object) -> SpanMutator:
    def mutate(span: Span) -> Span:
        return replace(
            span,
            input=value,
            capture=replace(span.capture, input=CaptureInfo("captured", None, False)),
        )

    return mutate


def _captured_output(value: object) -> SpanMutator:
    def mutate(span: Span) -> Span:
        return replace(
            span,
            output=value,
            capture=replace(span.capture, output=CaptureInfo("captured", None, False)),
        )

    return mutate


def _tool_signature(name: str, tool_name: str) -> SpanMutator:
    def mutate(span: Span) -> Span:
        if not isinstance(span.details, ToolDetails):
            raise ValueError("tool signature scenario target is not a tool Span")
        return replace(
            span,
            name=name,
            details=replace(span.details, tool_name=tool_name),
        )

    return mutate


def _redacted_input(value: str) -> SpanMutator:
    return _captured_input({"api_key": value})


def _model_change(model: str) -> SpanMutator:
    def mutate(span: Span) -> Span:
        if not isinstance(span.details, LLMDetails):
            raise ValueError("model scenario target is not an LLM Span")
        return replace(span, details=replace(span.details, request_model=model))

    return mutate


def _parameter_change(parameters: dict[str, object]) -> SpanMutator:
    def mutate(span: Span) -> Span:
        if not isinstance(span.details, LLMDetails):
            raise ValueError("parameter scenario target is not an LLM Span")
        return replace(span, details=replace(span.details, request_parameters=parameters))

    return mutate


def _token_change(input_tokens: int, output_tokens: int) -> SpanMutator:
    def mutate(span: Span) -> Span:
        if not isinstance(span.details, LLMDetails):
            raise ValueError("token scenario target is not an LLM Span")
        usage = replace(
            span.details.usage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return replace(span, details=replace(span.details, usage=usage))

    return mutate


def _metadata_change(value: object) -> TraceMutator:
    def mutate(trace: Trace) -> Trace:
        return replace(trace, metadata={"framework_metadata": value})

    return mutate


def _large_metadata_change(trace: Trace) -> Trace:
    return replace(trace, metadata={"irrelevant_noise": "x" * 4096})


def _scenario(
    name: str,
    left_description: str,
    right_description: str,
    left: EvaluationRun,
    right: EvaluationRun,
    *,
    observed_difference: ObservedState,
    meaningful_divergence: DivergenceState,
    investigation_starting_point: StartingPointState,
    expected_starting_point_path: str | None = None,
    allowed_candidate_paths: tuple[str, ...] = (),
    forbidden_confident_candidates: tuple[str, ...],
    uncertainty_barrier: str | None = None,
    downstream_observations: tuple[str, ...] = (),
    content_evidence: str = "not_applicable",
    left_complete: bool = True,
    right_complete: bool = True,
    notes: str,
    allowed_outcomes: tuple[OutcomeClass, ...] = ("ALLOWED_CONFIDENT", "ALLOWED_UNCERTAIN"),
) -> DivergenceScenario:
    return DivergenceScenario(
        name=name,
        left_description=left_description,
        right_description=right_description,
        left=left,
        right=right,
        observed_difference=observed_difference,
        meaningful_divergence=meaningful_divergence,
        investigation_starting_point=investigation_starting_point,
        expected_starting_point_path=expected_starting_point_path,
        allowed_candidate_paths=allowed_candidate_paths,
        forbidden_confident_candidates=forbidden_confident_candidates,
        uncertainty_barrier=uncertainty_barrier,
        downstream_observations=downstream_observations,
        content_evidence=content_evidence,
        left_complete=left_complete,
        right_complete=right_complete,
        notes=notes,
        allowed_outcomes=allowed_outcomes,
    )


def build_corpus(base: EvaluationRun) -> tuple[DivergenceScenario, ...]:
    """Build the 30 mandatory V03-10 scenarios from one public-path baseline."""

    alerts_output = "span:Lookup alerts/output"
    alerts_input = "span:Lookup alerts/input"
    alerts_status = "span:Lookup alerts/status"
    plan_status = "span:Plan/status"
    plan_model = "span:Plan/model"
    plan_parameters = "span:Plan/request_parameters"
    repeat_group = "group:tool.call/Lookup weather"
    route_subtree = "subtree:Customer route"

    same_left = clone_run(base, "identical-left")
    same_right = clone_run(base, "identical-right", timing_offset_us=700_000)

    scenarios: list[DivergenceScenario] = [
        _scenario(
            "identical_runs",
            "Same observed agent-like execution.",
            "Same structure with regenerated execution-local IDs.",
            same_left,
            same_right,
            observed_difference="none",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=("candidate:any",),
            notes="No admitted behavioral observation differs.",
        ),
        _scenario(
            "timing_only_variation",
            "Baseline execution.",
            "Same structure with shifted timestamps and durations.",
            base,
            clone_run(base, "timing-only", timing_offset_us=2_000_000, duration_us=80_000),
            observed_difference="present",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=("field:timestamps",),
            notes="Timing variation is observed but is not an admitted behavioral divergence.",
        ),
        _scenario(
            "token_only_variation",
            "Baseline LLM usage observations.",
            "Only provider/accounting token counts differ.",
            base,
            clone_run(base, "token-only", span_mutators={"plan": _token_change(42, 17)}),
            observed_difference="present",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=("field:token_usage",),
            notes="Exact metrics remain context detail and do not nominate a behavioral location.",
        ),
        _scenario(
            "model_only_change",
            "Baseline model observation.",
            "Only the known request model changes.",
            base,
            clone_run(base, "model-only", span_mutators={"plan": _model_change("offline-model-b")}),
            observed_difference="present",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=(plan_model,),
            notes="Model metadata is context-only when execution behavior is unchanged.",
        ),
        _scenario(
            "request_parameter_only_change",
            "Baseline request parameters.",
            "Only the known request-parameter object changes.",
            base,
            clone_run(base, "parameter-only", span_mutators={"plan": _parameter_change({"temperature": 0.7})}),
            observed_difference="present",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=(plan_parameters,),
            notes="Request parameters are context-only when no behavioral observation changes.",
        ),
        _scenario(
            "aligned_tool_output_change",
            "Captured output at one uniquely aligned tool.",
            "The same tool has a different captured output.",
            clone_run(base, "output-left", span_mutators={"alerts": _captured_output({"alerts": []})}),
            clone_run(base, "output-right", span_mutators={"alerts": _captured_output({"alerts": ["notice"]})}),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=alerts_output,
            allowed_candidate_paths=(alerts_output,),
            forbidden_confident_candidates=("candidate:unresolved-repeated-member",),
            content_evidence="available_both",
            notes="Both CaptureInfo values are captured and the aligned sanitized values differ.",
        ),
        _scenario(
            "aligned_tool_input_change",
            "Captured input at one uniquely aligned tool.",
            "The same tool has a different captured input.",
            clone_run(base, "input-left", span_mutators={"alerts": _captured_input({"city": "Tokyo"})}),
            clone_run(base, "input-right", span_mutators={"alerts": _captured_input({"city": "Osaka"})}),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=alerts_input,
            allowed_candidate_paths=(alerts_input,),
            forbidden_confident_candidates=("candidate:unresolved-repeated-member",),
            content_evidence="available_both",
            notes="Both CaptureInfo values are captured and the aligned sanitized values differ.",
        ),
        _scenario(
            "capture_disabled_one_side",
            "Captured tool output.",
            "The possible corresponding output is not captured.",
            clone_run(base, "capture-left", span_mutators={"alerts": _captured_output({"alerts": []})}),
            clone_run(base, "capture-right"),
            observed_difference="unknown",
            meaningful_divergence="uncertain",
            investigation_starting_point="uncertain",
            expected_starting_point_path=None,
            forbidden_confident_candidates=(alerts_output,),
            uncertainty_barrier="capture_unavailable",
            content_evidence="disabled_one_side",
            allowed_outcomes=("ALLOWED_UNCERTAIN",),
            notes="The missing content observation cannot establish equality or inequality.",
        ),
        _scenario(
            "redacted_content",
            "A captured input containing a sanitized sensitive-key value.",
            "A different captured input with a non-sensitive shape.",
            clone_run(base, "redacted-left", span_mutators={"alerts": _redacted_input("left-value")}),
            clone_run(base, "redacted-right", span_mutators={"alerts": _captured_input({"result": "safe"})}),
            observed_difference="present",
            meaningful_divergence="uncertain",
            investigation_starting_point="uncertain",
            forbidden_confident_candidates=(alerts_input,),
            uncertainty_barrier="redacted_observation",
            content_evidence="redacted",
            allowed_outcomes=("ALLOWED_UNCERTAIN",),
            notes="The sanitized observation differs, but the original sensitive value is not known.",
        ),
        _scenario(
            "new_error_exact_span",
            "An aligned tool completes successfully.",
            "The same aligned tool has an observed error.",
            base,
            clone_run(base, "new-error", error_labels=("alerts",)),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=alerts_status,
            allowed_candidate_paths=(alerts_status,),
            forbidden_confident_candidates=("candidate:later-subtree",),
            downstream_observations=("right aligned tool status=error",),
            notes="The error status is directly observed at a uniquely resolved Span.",
        ),
        _scenario(
            "resolved_error_exact_span",
            "An aligned tool has an observed error.",
            "The same aligned tool completes successfully.",
            clone_run(base, "resolved-left", error_labels=("alerts",)),
            base,
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=alerts_status,
            allowed_candidate_paths=(alerts_status,),
            forbidden_confident_candidates=("candidate:missing-right-tail",),
            downstream_observations=("left aligned tool status=error",),
            notes="The error resolution is directly observed at a uniquely resolved Span.",
        ),
        _scenario(
            "trace_status_only_change",
            "Complete observed Spans with Trace status ok.",
            "Same observed Spans with Trace status error.",
            base,
            clone_run(base, "trace-status-only", status="error"),
            observed_difference="present",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=("trace:status",),
            notes="Trace status is context-only without a corresponding Span behavior change.",
        ),
        _scenario(
            "unique_tool_added",
            "Complete baseline execution.",
            "One uniquely resolvable customer tool is added under the root.",
            base,
            clone_run(
                base,
                "unique-tool-added",
                order=(*base.order, "customer-tool"),
                extras=(("customer-tool", "alerts", "agent-root"),),
                span_mutators={"customer-tool": _tool_signature("Lookup customer", "lookup_customer")},
            ),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path="span:Lookup customer",
            allowed_candidate_paths=("span:Lookup customer",),
            forbidden_confident_candidates=("candidate:unresolved-parent",),
            notes="The added tool has a unique signature and a resolvable parent.",
        ),
        _scenario(
            "unique_tool_removed",
            "Complete execution containing the alerts tool subtree.",
            "The unique alerts tool subtree is absent from a complete run.",
            base,
            clone_run(base, "unique-tool-removed", drop=("alerts", "normalize-alerts")),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path="span:Lookup alerts",
            allowed_candidate_paths=("span:Lookup alerts",),
            forbidden_confident_candidates=("candidate:incomplete-tail",),
            notes="The absent unique tool is known structure, not a missing incomplete tail.",
        ),
        _scenario(
            "execution_subtree_added",
            "Complete baseline execution.",
            "A non-tool customer route subtree is added under the root.",
            base,
            clone_run(
                base,
                "subtree-added",
                order=(*base.order, "customer-route", "customer-route-leaf"),
                extras=(
                    ("customer-route", "normalize-alerts", "agent-root"),
                    ("customer-route-leaf", "normalize-alerts", "customer-route"),
                ),
            ),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=route_subtree,
            allowed_candidate_paths=(route_subtree,),
            forbidden_confident_candidates=("candidate:semantic-branch",),
            notes="The result describes observed subtree presence and no graph meaning.",
        ),
        _scenario(
            "execution_subtree_removed",
            "Complete execution containing a planning subtree.",
            "The planning subtree is absent from a complete run.",
            base,
            clone_run(
                base,
                "subtree-removed",
                drop=("plan", "lookup-1", "normalize-1", "lookup-2", "normalize-2", "lookup-3", "normalize-3"),
            ),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path="subtree:Plan",
            allowed_candidate_paths=("subtree:Plan",),
            forbidden_confident_candidates=("candidate:semantic-branch",),
            notes="The result describes observed structural absence and no graph meaning.",
        ),
        _scenario(
            "repeated_tool_insertion",
            "Three same-signature weather tool siblings.",
            "A fourth same-signature weather tool sibling is inserted.",
            base,
            clone_run(
                base,
                "repeated-insertion",
                order=("agent-root", "plan", "lookup-1", "normalize-1", "lookup-extra", "lookup-2", "normalize-2", "lookup-3", "normalize-3", "alerts", "normalize-alerts", "synthesis"),
                extras=(("lookup-extra", "lookup-2", "agent-root"),),
            ),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=repeat_group,
            allowed_candidate_paths=(repeat_group,),
            forbidden_confident_candidates=(
                "span:Lookup weather[0]",
                "span:Lookup weather[1]",
                "span:Lookup weather[2]",
                "span:Lookup weather[3]",
            ),
            notes="Group cardinality is supported; individual repeated-member identity is not.",
        ),
        _scenario(
            "repeated_tool_removal",
            "Three same-signature weather tool siblings.",
            "One same-signature weather tool sibling is absent.",
            base,
            clone_run(base, "repeated-removal", drop=("lookup-2", "normalize-2")),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=repeat_group,
            allowed_candidate_paths=(repeat_group,),
            forbidden_confident_candidates=(
                "span:Lookup weather[0]",
                "span:Lookup weather[1]",
                "span:Lookup weather[2]",
            ),
            notes="Group cardinality is supported; individual repeated-member identity is not.",
        ),
        _scenario(
            "repeated_tool_reordering",
            "Repeated weather siblings in their baseline order.",
            "The same repeated siblings are reordered.",
            base,
            clone_run(
                base,
                "repeated-reorder",
                order=("agent-root", "plan", "lookup-2", "normalize-2", "lookup-1", "normalize-1", "lookup-3", "normalize-3", "alerts", "normalize-alerts", "synthesis"),
            ),
            observed_difference="present",
            meaningful_divergence="uncertain",
            investigation_starting_point="uncertain",
            forbidden_confident_candidates=("span:Lookup weather[0]", "span:Lookup weather[1]", "span:Lookup weather[2]"),
            uncertainty_barrier="repeated_sibling_ambiguity",
            allowed_outcomes=("ALLOWED_UNCERTAIN",),
            notes="Ordinal movement does not establish an individual logical identity.",
        ),
        _scenario(
            "nested_repeated_groups",
            "A uniquely aligned route parent has two same-signature normalization children.",
            "The same route parent has an additional same-signature normalization child.",
            clone_run(
                base,
                "nested-repeated-left",
                order=(*base.order, "nested-parent", "nested-child-1", "nested-child-2"),
                extras=(
                    ("nested-parent", "normalize-alerts", "agent-root"),
                    ("nested-child-1", "normalize-2", "nested-parent"),
                    ("nested-child-2", "normalize-2", "nested-parent"),
                ),
            ),
            clone_run(
                base,
                "nested-repeated-right",
                order=(*base.order, "nested-parent", "nested-child-1", "nested-child-2", "nested-child-3"),
                extras=(
                    ("nested-parent", "normalize-alerts", "agent-root"),
                    ("nested-child-1", "normalize-2", "nested-parent"),
                    ("nested-child-2", "normalize-2", "nested-parent"),
                    ("nested-child-3", "normalize-2", "nested-parent"),
                ),
            ),
            observed_difference="present",
            meaningful_divergence="uncertain",
            investigation_starting_point="uncertain",
            forbidden_confident_candidates=("span:nested-ordinal-member",),
            uncertainty_barrier="repeated_sibling_ambiguity",
            allowed_outcomes=("ALLOWED_UNCERTAIN",),
            notes="Ambiguity is confined to the nested group and does not erase unrelated branches.",
        ),
        _scenario(
            "early_termination_partial_trace",
            "Complete baseline execution.",
            "The right run ends before the tail and has no terminal lifecycle observation.",
            base,
            clone_run(base, "partial-trace", order=base.order[:6], incomplete=True),
            observed_difference="present",
            meaningful_divergence="uncertain",
            investigation_starting_point="uncertain",
            forbidden_confident_candidates=("candidate:removed-tail",),
            uncertainty_barrier="incomplete_trace",
            right_complete=False,
            allowed_outcomes=("ALLOWED_UNCERTAIN",),
            notes="Absent tail structure is unknown while the right lifecycle is incomplete.",
        ),
        _scenario(
            "missing_parent",
            "Complete baseline execution.",
            "One child references a missing parent while a later unique customer tool is observed.",
            base,
            clone_run(
                base,
                "missing-parent",
                order=(*base.order, "customer-tool"),
                extras=(("customer-tool", "alerts", "agent-root"),),
                span_mutators={"customer-tool": _tool_signature("Lookup customer", "lookup_customer")},
                missing_parent_labels=("normalize-alerts",),
            ),
            observed_difference="unavailable",
            meaningful_divergence="supported",
            investigation_starting_point="uncertain",
            allowed_candidate_paths=("span:Lookup customer",),
            forbidden_confident_candidates=("subtree:missing-parent",),
            uncertainty_barrier="missing_parent",
            allowed_outcomes=("ALLOWED_CONFIDENT", "ALLOWED_UNCERTAIN"),
            downstream_observations=("later customer tool is structurally observed",),
            notes="The later customer candidate is supported, but the earlier missing-parent barrier withholds the first starting point.",
        ),
        _scenario(
            "duplicate_structural_id_invalid_structure",
            "Complete baseline execution.",
            "The right trace contains a duplicated structural Span ID.",
            base,
            clone_run(base, "duplicate-structure", duplicate_label="lookup-2"),
            observed_difference="unavailable",
            meaningful_divergence="uncertain",
            investigation_starting_point="uncertain",
            forbidden_confident_candidates=("candidate:invalid-structure",),
            uncertainty_barrier="invalid_structure",
            allowed_outcomes=("ALLOWED_UNCERTAIN",),
            notes="Invalid structure prevents a supported identity at the affected location.",
        ),
        _scenario(
            "both_runs_fail_identically",
            "Complete run with an observed lookup error.",
            "Same observed lookup error in an independently identified run.",
            clone_run(base, "identical-failure-left", status="error", error_labels=("alerts",)),
            clone_run(base, "identical-failure-right", status="error", error_labels=("alerts",)),
            observed_difference="none",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=("candidate:any",),
            downstream_observations=("both aligned alerts tools status=error",),
            notes="Identical observed failure evidence does not create a behavioral divergence.",
        ),
        _scenario(
            "both_runs_fail_differently",
            "Complete run with an observed planning error.",
            "Complete run with an observed alerts error.",
            clone_run(base, "different-failure-left", status="error", error_labels=("plan",)),
            clone_run(base, "different-failure-right", status="error", error_labels=("alerts",)),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=plan_status,
            allowed_candidate_paths=(plan_status,),
            forbidden_confident_candidates=("candidate:failure-pair-meaning",),
            downstream_observations=("left plan error", "right alerts error"),
            notes="Both statuses are observed separately; the pair does not establish why they differ.",
        ),
        _scenario(
            "error_before_later_structural_difference",
            "Complete run with no Span error.",
            "An early plan error and a later added customer tool are both observed.",
            base,
            clone_run(
                base,
                "error-before-subtree",
                order=(*base.order, "customer-tool"),
                extras=(("customer-tool", "alerts", "agent-root"),),
                span_mutators={"customer-tool": _tool_signature("Lookup customer", "lookup_customer")},
                error_labels=("plan",),
            ),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=plan_status,
            allowed_candidate_paths=(plan_status,),
            forbidden_confident_candidates=("span:Lookup customer",),
            downstream_observations=("right customer subtree is also present",),
            notes="The earlier supported error is selected before the later structural observation.",
        ),
        _scenario(
            "multiple_independent_divergences",
            "A plan status and an alerts output are both observed.",
            "The plan status and alerts output differ independently.",
            clone_run(
                base,
                "multiple-left",
                span_mutators={
                    "alerts": _captured_output({"alerts": []}),
                },
            ),
            clone_run(
                base,
                "multiple-right",
                span_mutators={
                    "alerts": _captured_output({"alerts": ["notice"]}),
                },
                error_labels=("plan",),
            ),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=plan_status,
            allowed_candidate_paths=(plan_status,),
            forbidden_confident_candidates=("candidate:timestamp-order",),
            downstream_observations=("alerts output differs independently",),
            content_evidence="available_both",
            notes="Both supported observations are retained; the plan status is selected by fixed structural order.",
        ),
        _scenario(
            "chronological_vs_lexicographic_order",
            "The alerts output changes earlier in wall-clock time than the plan status.",
            "Both behavioral observations are otherwise structurally aligned.",
            clone_run(
                base,
                "chronology-left",
                timing_overrides_us={
                    "plan": BASELINE_EPOCH_US + 900_000,
                    "alerts": BASELINE_EPOCH_US + 100_000,
                },
                span_mutators={
                    "alerts": _captured_output({"alerts": []}),
                },
            ),
            clone_run(
                base,
                "chronology-right",
                timing_overrides_us={
                    "plan": BASELINE_EPOCH_US + 900_000,
                    "alerts": BASELINE_EPOCH_US + 100_000,
                },
                span_mutators={
                    "alerts": _captured_output({"alerts": ["notice"]}),
                },
                error_labels=("plan",),
            ),
            observed_difference="present",
            meaningful_divergence="supported",
            investigation_starting_point="supported",
            expected_starting_point_path=plan_status,
            allowed_candidate_paths=(plan_status,),
            forbidden_confident_candidates=("candidate:earliest-wall-clock-event",),
            downstream_observations=("alerts output occurs earlier by timestamp",),
            content_evidence="available_both",
            notes="The plan path is structurally earlier even though the alerts output occurs earlier in time.",
        ),
        _scenario(
            "framework_metadata_only_difference",
            "Baseline Canonical execution with one framework metadata value.",
            "Same Canonical execution behavior with different framework metadata.",
            base,
            clone_run(base, "framework-metadata", trace_mutator=_metadata_change("framework-b")),
            observed_difference="present",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=("trace:framework-metadata",),
            notes="Framework metadata changes without a behavioral Canonical observation.",
        ),
        _scenario(
            "large_irrelevant_metadata_difference",
            "Baseline execution with ordinary metadata.",
            "Same execution with bounded irrelevant metadata noise.",
            base,
            clone_run(base, "large-metadata", trace_mutator=_large_metadata_change),
            observed_difference="present",
            meaningful_divergence="none",
            investigation_starting_point="none",
            forbidden_confident_candidates=("trace:irrelevant-metadata",),
            notes="Large irrelevant metadata must not dominate the behavioral triage result.",
        ),
    ]
    if len(scenarios) != 30:
        raise AssertionError(f"V03-10 corpus must contain 30 scenarios, got {len(scenarios)}")
    return tuple(scenarios)


def validate_corpus(scenarios: Sequence[DivergenceScenario]) -> None:
    """Validate corpus shape and closed oracle vocabulary."""

    names = [scenario.name for scenario in scenarios]
    if len(names) != len(set(names)):
        raise AssertionError("V03-10 scenario names must be unique")
    for scenario in scenarios:
        if scenario.meaningful_divergence == "supported" and not scenario.allowed_candidate_paths:
            raise AssertionError(f"{scenario.name} needs an allowed candidate")
        if scenario.meaningful_divergence != "supported" and scenario.allowed_candidate_paths:
            raise AssertionError(f"{scenario.name} has a candidate without support")
        if not scenario.forbidden_confident_candidates:
            raise AssertionError(f"{scenario.name} needs forbidden confident candidates")


def serialize_corpus(scenarios: Sequence[DivergenceScenario]) -> str:
    validate_corpus(scenarios)
    document = {
        "corpus_version": "v0.3-10",
        "scenario_count": len(scenarios),
        "scenarios": [scenario.to_dict() for scenario in scenarios],
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_report(scenarios: Sequence[DivergenceScenario]) -> str:
    validate_corpus(scenarios)
    rows = [
        "# TraceMotive v0.3 Divergence Adversarial Evaluation",
        "",
        "This is an evaluation oracle, not production V03-11 accuracy.",
        "The product-facing first meaningful divergence means the first supported behavioral divergence in deterministic structural triage order.",
        "",
        "| Scenario | L/R spans | Observed | Meaningful | Starting point | Barrier | Confident allowed | Notes |",
        "|---|---:|---|---|---|---|---|---|",
    ]
    for scenario in scenarios:
        rows.append(
            "| `{}` | {}/{} | {} | {} | {} | {} | {} | {} |".format(
                scenario.name,
                scenario.left_span_count,
                scenario.right_span_count,
                scenario.observed_difference,
                scenario.meaningful_divergence,
                scenario.investigation_starting_point,
                scenario.uncertainty_barrier or "none",
                "yes"
                if scenario.allowed_candidate_paths and "ALLOWED_CONFIDENT" in scenario.allowed_outcomes
                else "no",
                scenario.notes,
            )
        )
    meaningful_counts = {state: sum(s.meaningful_divergence == state for s in scenarios) for state in sorted(DIVERGENCE_STATES)}
    starting_counts = {state: sum(s.investigation_starting_point == state for s in scenarios) for state in sorted(STARTING_POINT_STATES)}
    observed_counts = {state: sum(s.observed_difference == state for s in scenarios) for state in sorted(OBSERVED_STATES)}
    expected_confident_meaningful = sum(s.meaningful_divergence == "supported" for s in scenarios)
    expected_confident_starting = sum(s.investigation_starting_point == "supported" for s in scenarios)
    expected_uncertain_meaningful = sum(s.meaningful_divergence == "uncertain" for s in scenarios)
    expected_uncertain_starting = sum(s.investigation_starting_point == "uncertain" for s in scenarios)
    rows.extend(
        [
            "",
            "## Aggregate counts",
            "",
            f"- scenarios: {len(scenarios)}",
            f"- observed difference states: {json.dumps(observed_counts, sort_keys=True)}",
            f"- meaningful divergence states: {json.dumps(meaningful_counts, sort_keys=True)}",
            f"- investigation starting-point states: {json.dumps(starting_counts, sort_keys=True)}",
            f"- required confident meaningful-divergence answers: {expected_confident_meaningful}",
            f"- required confident starting-point answers: {expected_confident_starting}",
            f"- required uncertain meaningful-divergence answers: {expected_uncertain_meaningful}",
            f"- required uncertain starting-point answers: {expected_uncertain_starting}",
            "- false confident meaningful divergence target: 0",
            "- false confident investigation starting-point target: 0",
        ]
    )
    return "\n".join(rows) + "\n"


def count_false_confidence(
    scenarios: Sequence[DivergenceScenario],
    outcomes: Sequence[ProductionOutcome],
) -> FalseConfidenceCounts:
    """Count safety failures and useful-answer coverage for future results.

    A production result must provide exactly one outcome for every scenario.
    The meaningful-divergence candidate set describes supported findings, but
    the investigation starting point is stricter: it must equal the oracle's
    deterministic selected path.
    """

    by_name = {scenario.name: scenario for scenario in scenarios}
    outcome_names = [outcome.scenario_name for outcome in outcomes]
    if len(outcome_names) != len(by_name) or set(outcome_names) != set(by_name):
        missing = sorted(set(by_name) - set(outcome_names))
        extra = sorted(set(outcome_names) - set(by_name))
        raise ValueError(f"outcomes must cover each scenario exactly once; missing={missing}, extra={extra}")
    if len(outcome_names) != len(set(outcome_names)):
        raise ValueError("outcomes must contain each scenario exactly once")

    meaningful_false = 0
    starting_false = 0
    expected_confident_meaningful = 0
    correctly_confident_meaningful = 0
    expected_confident_starting_point = 0
    correctly_confident_starting_point = 0
    expected_uncertain_meaningful = 0
    safely_withheld_meaningful = 0
    expected_uncertain_starting_point = 0
    safely_withheld_starting_point = 0
    for outcome in outcomes:
        scenario = by_name[outcome.scenario_name]
        meaningful_candidate_allowed = outcome.candidate_path in scenario.allowed_candidate_paths
        starting_candidate_allowed = (
            outcome.candidate_path == scenario.expected_starting_point_path
        )
        if outcome.meaningful_confident and (
            scenario.meaningful_divergence != "supported" or not meaningful_candidate_allowed
        ):
            meaningful_false += 1
        if outcome.starting_point_confident and (
            scenario.investigation_starting_point != "supported" or not starting_candidate_allowed
        ):
            starting_false += 1
        if scenario.meaningful_divergence == "supported":
            expected_confident_meaningful += 1
            if outcome.meaningful_confident and meaningful_candidate_allowed:
                correctly_confident_meaningful += 1
        elif scenario.meaningful_divergence == "uncertain":
            expected_uncertain_meaningful += 1
            if not outcome.meaningful_confident:
                safely_withheld_meaningful += 1
        if scenario.investigation_starting_point == "supported":
            expected_confident_starting_point += 1
            if outcome.starting_point_confident and starting_candidate_allowed:
                correctly_confident_starting_point += 1
        elif scenario.investigation_starting_point == "uncertain":
            expected_uncertain_starting_point += 1
            if not outcome.starting_point_confident:
                safely_withheld_starting_point += 1
    return FalseConfidenceCounts(
        meaningful_false,
        starting_false,
        expected_confident_meaningful,
        correctly_confident_meaningful,
        expected_confident_starting_point,
        correctly_confident_starting_point,
        expected_uncertain_meaningful,
        safely_withheld_meaningful,
        expected_uncertain_starting_point,
        safely_withheld_starting_point,
        0,
    )


__all__ = [
    "DivergenceScenario",
    "EvaluationRun",
    "FalseConfidenceCounts",
    "ProductionOutcome",
    "build_corpus",
    "clone_run",
    "count_false_confidence",
    "render_report",
    "serialize_corpus",
    "validate_corpus",
]
