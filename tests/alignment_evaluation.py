"""V02-19 corpus and report helpers.

The baseline is captured through the public manual SDK path and a real local
Collector/Repository.  Scenario variants are sanitized Canonical copies of
that captured execution with controlled structural perturbations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import tempfile
from typing import Iterable

import tracemotive
from tracemotive import sdk
from tracemotive.canonical import (
    AgentDetails,
    CustomDetails,
    Error,
    LLMDetails,
    LLMUsage,
    Span,
    SpanSource,
    ToolDetails,
    Trace,
)
from tracemotive.storage.repository import timestamp_to_us, us_to_timestamp
from tracemotive._evaluation.alignment import AlignmentReport, align_traces


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    trace: Trace
    spans: tuple[Span, ...]
    labels: dict[str, str]
    order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    name: str
    purpose: str
    report: AlignmentReport
    expected_matches: int
    correct_matches: int
    incorrect_pairs: int
    missed_expected_matches: int
    branch_local_correct: int
    branch_local_expected: int
    repeated_shift: dict[str, int] | None


def _llm_details() -> LLMDetails:
    return LLMDetails(
        "llm",
        "fixture-provider",
        "offline-model",
        "offline-model",
        None,
        LLMUsage(0, 0, 0, 0, 0),
        ["stop"],
        {"temperature": 0},
        None,
    )


def capture_realistic_run() -> EvaluationRun:
    """Capture an agent-like run through SDK -> HTTP -> Collector -> SQLite."""

    from tests.test_issue15_e2e import _LoopbackCollectorServer

    sdk._reset_for_tests()
    with tempfile.TemporaryDirectory(prefix="tracemotive-v02-19-corpus-") as directory:
        server = _LoopbackCollectorServer(Path(directory) / "capture.sqlite3")
        labels: dict[str, str] = {}
        order: list[str] = []
        try:
            tracemotive.configure(
                enabled=True,
                endpoint=server.endpoint,
                capture_content=False,
            )
            with tracemotive.trace("Weather assistant") as trace_value:
                assert trace_value is not None
                with tracemotive.span(
                    "Agent root",
                    type="agent",
                    operation="agent.run",
                    details=AgentDetails("agent", "Evaluation agent", "0.2"),
                ) as root:
                    labels[root.span_id] = "agent-root"
                    order.append("agent-root")
                    with tracemotive.span(
                        "Plan",
                        type="llm",
                        operation="llm.generate",
                        details=_llm_details(),
                        input={"prompt": "Plan a weather answer"},
                    ) as plan:
                        labels[plan.span_id] = "plan"
                        order.append("plan")

                    for index in range(1, 4):
                        label = f"lookup-{index}"
                        with tracemotive.span(
                            "Lookup weather",
                            type="tool",
                            operation="tool.call",
                            details=ToolDetails("tool", "lookup_weather", f"call-{index}"),
                            input={"city": "Tokyo", "call": index},
                        ) as lookup:
                            labels[lookup.span_id] = label
                            order.append(label)
                            normalize_label = f"normalize-{index}"
                            with tracemotive.span(
                                "Normalize result",
                                type="custom",
                                operation="custom.normalize",
                                details=CustomDetails("custom", "normalize"),
                            ) as normalize:
                                labels[normalize.span_id] = normalize_label
                                order.append(normalize_label)
                            lookup.set_output({"temperature": 21 + index})

                    with tracemotive.span(
                        "Lookup alerts",
                        type="tool",
                        operation="tool.call",
                        details=ToolDetails("tool", "lookup_alerts", "alerts-1"),
                        input={"city": "Tokyo"},
                    ) as alerts:
                        labels[alerts.span_id] = "alerts"
                        order.append("alerts")
                        with tracemotive.span(
                            "Normalize result",
                            type="custom",
                            operation="custom.normalize",
                            details=CustomDetails("custom", "normalize"),
                        ) as normalize_alerts:
                            labels[normalize_alerts.span_id] = "normalize-alerts"
                            order.append("normalize-alerts")
                        alerts.set_output({"alerts": []})

                    with tracemotive.span(
                        "Synthesize answer",
                        type="llm",
                        operation="llm.generate",
                        details=_llm_details(),
                        input={"prompt": "Combine the observations"},
                    ) as synthesis:
                        labels[synthesis.span_id] = "synthesis"
                        order.append("synthesis")

            if not tracemotive.flush(5):
                raise AssertionError("public SDK flush did not reach a terminal outcome")
            trace = server.repository.get_trace(trace_value.trace_id)
            spans = server.repository.get_spans_for_trace(trace_value.trace_id)
            if trace is None or spans is None:
                raise AssertionError("realistic public SDK trace was not persisted")
            if len(spans) != len(labels):
                raise AssertionError("captured labels do not cover persisted spans")
            return EvaluationRun(trace, tuple(spans), labels, tuple(order))
        finally:
            sdk._reset_for_tests()
            if not server.close():
                raise AssertionError("capture Collector did not shut down")


def _stable_id(prefix: str, value: str, length: int) -> str:
    return sha256(f"{prefix}:{value}".encode("utf-8")).hexdigest()[:length]


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
    error_label: str | None = None,
    missing_parent_labels: Iterable[str] = (),
    duplicate_label: str | None = None,
) -> EvaluationRun:
    """Create a deterministic Canonical variant from the captured run."""

    by_label, parent_by_label = _base_maps(base)
    dropped = set(drop)
    selected = [label for label in (base.order if order is None else order) if label not in dropped]
    extra_by_label = {label: (template, parent) for label, template, parent in extras}
    selected.extend(label for label, _, _ in extras if label not in selected)

    specs: list[tuple[str, str, str | None]] = []
    for label in selected:
        if label in extra_by_label:
            template, parent_override = extra_by_label[label]
            specs.append((label, template, parent_override))
        else:
            specs.append((label, label, parent_by_label[label]))

    trace_id = _stable_id("trace", run_name, 32)
    span_ids = {label: _stable_id("span", f"{run_name}:{label}", 16) for label, _, _ in specs}
    missing = set(missing_parent_labels)
    start_order = [label for label, _, _ in specs]
    rank = {label: index for index, label in enumerate(start_order)}
    epoch = timestamp_to_us("2026-08-15T00:00:00.000000Z")
    spans: list[Span] = []

    for label, template_label, parent_label in specs:
        template = by_label[template_label]
        if label in missing:
            parent_id = "deadbeefdeadbeef"
        else:
            parent_id = None if parent_label is None else span_ids.get(parent_label)
        started_us = epoch + timing_offset_us + rank[label] * 100_000
        ended_us = started_us + duration_us
        new_id = span_ids[label]
        native_parent_id = None if parent_id is None else f"native-{parent_id}"
        source = replace(
            template.source,
            native_trace_id=f"native-trace-{run_name}",
            native_span_id=f"native-span-{new_id}",
            native_parent_span_id=native_parent_id,
        )
        span = replace(
            template,
            trace_id=trace_id,
            span_id=new_id,
            parent_span_id=parent_id,
            source=source,
            started_at=us_to_timestamp(started_us),
            ended_at=us_to_timestamp(ended_us),
        )
        if label == error_label:
            span = replace(span, status="error", error=Error("EvaluationError", "early execution error"))
        spans.append(span)

    if duplicate_label is not None:
        duplicate = next(span for span in spans if span_ids.get(duplicate_label) == span.span_id)
        spans.append(duplicate)

    trace = replace(
        base.trace,
        trace_id=trace_id,
        started_at=us_to_timestamp(epoch + timing_offset_us),
        ended_at=us_to_timestamp(epoch + timing_offset_us + (len(start_order) + 2) * 100_000),
        status=status,
        source=replace(base.trace.source, native_trace_id=f"native-trace-{run_name}"),
    )
    labels = {span_ids[label]: label for label, _, _ in specs}
    return EvaluationRun(trace, tuple(spans), labels, tuple(selected))


def _scenario_result(
    name: str,
    purpose: str,
    left: EvaluationRun,
    right: EvaluationRun,
    *,
    branch_labels: Iterable[str] = (),
    repeated_labels: Iterable[str] = (),
) -> ScenarioResult:
    report = align_traces(left.trace, left.spans, right.trace, right.spans)
    left_labels = {span_id: left.labels[span_id] for span_id in left.labels}
    right_labels = {span_id: right.labels[span_id] for span_id in right.labels}
    common_labels = set(left.labels.values()) & set(right.labels.values())
    correct = incorrect = 0
    for item in report.spans:
        if item["alignment"] != "matched":
            continue
        left_label = left_labels[item["left"]["span_id"]]
        right_label = right_labels[item["right"]["span_id"]]
        if left_label == right_label:
            correct += 1
        else:
            incorrect += 1

    branch = set(branch_labels)
    branch_local_correct = 0
    for item in report.spans:
        if item["alignment"] != "matched":
            continue
        left_label = left_labels[item["left"]["span_id"]]
        right_label = right_labels[item["right"]["span_id"]]
        if left_label == right_label and left_label in branch:
            branch_local_correct += 1
    repeated = set(repeated_labels)
    repeated_shift = None
    if repeated:
        shifted = unmatched = ambiguous = 0
        for item in report.spans:
            if item["alignment"] == "matched":
                left_label = left_labels[item["left"]["span_id"]]
                right_label = right_labels[item["right"]["span_id"]]
                if left_label in repeated and left_label != right_label:
                    shifted += 1
            elif item["alignment"] == "left_only":
                if left_labels[item["left"]["span_id"]] in repeated:
                    unmatched += 1
            elif item["alignment"] == "right_only":
                if right_labels[item["right"]["span_id"]] in repeated:
                    unmatched += 1
        for group in report.ambiguous_groups:
            labels = {
                left_labels[ref["span_id"]]
                for ref in group["left"]
                if ref["span_id"] in left_labels
            }
            labels.update(
                right_labels[ref["span_id"]]
                for ref in group["right"]
                if ref["span_id"] in right_labels
            )
            if labels & repeated:
                ambiguous += 1
        repeated_shift = {
            "later_repeated_shifted": shifted,
            "later_repeated_unmatched": unmatched,
            "later_repeated_ambiguous_groups": ambiguous,
        }

    return ScenarioResult(
        name,
        purpose,
        report,
        len(common_labels),
        correct,
        incorrect,
        max(0, len(common_labels) - correct),
        branch_local_correct,
        len(branch),
        repeated_shift,
    )


def evaluate_corpus() -> tuple[ScenarioResult, ...]:
    """Capture one realistic run and evaluate all required perturbations."""

    base = capture_realistic_run()
    base_order = list(base.order)
    inserted_order = [
        "agent-root",
        "plan",
        "lookup-1",
        "normalize-1",
        "lookup-extra",
        "normalize-extra",
        "lookup-2",
        "normalize-2",
        "lookup-3",
        "normalize-3",
        "alerts",
        "normalize-alerts",
        "synthesis",
    ]
    inserted = clone_run(
        base,
        "added-tool",
        order=inserted_order,
        extras=(("lookup-extra", "lookup-2", "agent-root"), ("normalize-extra", "normalize-2", "lookup-extra")),
    )
    removed = clone_run(base, "removed-tool", drop=("lookup-2", "normalize-2"))
    reordered_order = [
        "agent-root",
        "plan",
        "lookup-2",
        "normalize-2",
        "lookup-1",
        "normalize-1",
        "lookup-3",
        "normalize-3",
        "alerts",
        "normalize-alerts",
        "synthesis",
    ]
    return (
        _scenario_result(
            "near_identical_rerun",
            "Same captured agent-like structure with new execution-local and native IDs.",
            base,
            clone_run(base, "near-identical", timing_offset_us=700_000),
        ),
        _scenario_result(
            "repeated_tool_calls",
            "Three repeated Lookup weather tool siblings and their nested normalization spans.",
            base,
            clone_run(base, "repeated-tools", timing_offset_us=200_000),
            repeated_labels=("lookup-1", "lookup-2", "lookup-3"),
        ),
        _scenario_result(
            "added_tool_call",
            "One repeated tool call and its child inserted before later repeated siblings.",
            base,
            inserted,
            repeated_labels=("lookup-1", "lookup-2", "lookup-3"),
        ),
        _scenario_result(
            "removed_tool_call",
            "The middle repeated tool call and its child removed.",
            base,
            removed,
            repeated_labels=("lookup-1", "lookup-2", "lookup-3"),
        ),
        _scenario_result(
            "reordered_siblings",
            "Repeated Lookup weather siblings execute in the opposite first/second order.",
            base,
            clone_run(base, "reordered", order=reordered_order),
            repeated_labels=("lookup-1", "lookup-2", "lookup-3"),
        ),
        _scenario_result(
            "timing_variation",
            "Same structure with a later wall-clock offset and longer span duration.",
            base,
            clone_run(base, "timing", timing_offset_us=2_000_000, duration_us=80_000),
        ),
        _scenario_result(
            "partial_early_error",
            "Run terminates after the second repeated tool and marks that branch error.",
            base,
            clone_run(
                base,
                "partial-error",
                order=base_order[: base_order.index("normalize-2") + 1],
                status="error",
                error_label="lookup-2",
            ),
        ),
        _scenario_result(
            "missing_parent",
            "One nested alerts branch loses its parent while unrelated branches remain present.",
            base,
            clone_run(base, "missing-parent", missing_parent_labels=("normalize-alerts",)),
            branch_labels=("agent-root", "plan", "lookup-1", "normalize-1", "lookup-2", "normalize-2", "lookup-3", "normalize-3", "alerts", "synthesis"),
        ),
        _scenario_result(
            "fan_out_fan_in_like",
            "Several sibling tool branches feed a later synthesis span; timing varies only.",
            base,
            clone_run(base, "fanout-fanin", timing_offset_us=1_000_000),
            branch_labels=("lookup-1", "lookup-2", "lookup-3", "alerts", "synthesis"),
        ),
        _scenario_result(
            "duplicate_structural_sibling",
            "A deliberately duplicated exact timestamp/span-id sibling creates a local collision.",
            base,
            clone_run(base, "duplicate-sibling", duplicate_label="lookup-2"),
            branch_labels=("agent-root", "plan", "lookup-1", "normalize-1", "lookup-3", "normalize-3", "alerts", "normalize-alerts", "synthesis"),
            repeated_labels=("lookup-1", "lookup-2", "lookup-3"),
        ),
    )


def render_markdown(results: Iterable[ScenarioResult]) -> str:
    rows = list(results)
    lines = [
        "# TraceMotive v0.2 Simple Structural Alignment v1 Evaluation",
        "",
        "## Scope and methodology",
        "",
        "This is the V02-19 validation artifact. It does not add a production comparison API, `/api/v2` route, UI, heuristic matcher, or public alignment API.",
        "",
        "The baseline corpus is captured through the public `tracemotive.configure`, `trace`, `span`, and `flush` paths with `capture_content=False`. Events cross a real loopback HTTP Collector and are read back from a temporary SQLite Repository. The scenario pairs below are deterministic sanitized Canonical variants of that captured agent-like execution: Agent -> planning LLM -> repeated weather tools -> normalization branches -> alert tool -> synthesis LLM.",
        "",
        "Framework/native IDs are replaced independently for each run and are not supplied to the evaluator. The evaluated key is `(semantic parent path, type, operation, name, same-parent ordinal)`. Ordinals use exact `started_at` timestamp order and `span_id` as the specified tie-breaker. No timestamp tolerance, similarity, nearest-neighbor, input/output matching, or causal inference is used.",
        "",
        "## Metrics",
        "",
        "Match coverage is `matched / max(left_total, right_total)`, with `1.0` when both totals are zero. It is descriptive structural coverage only; it is not causal correctness, semantic identity, or debugging success probability.",
        "",
        "`ambiguous_groups` counts structural-key collision groups. `unavailable` counts individual spans whose parent path cannot be computed. `incorrect_pairs` compares matched span labels against the human-expected logical occurrence labels kept outside the alignment input.",
        "",
        "## Per-scenario results",
        "",
        "| Scenario | L/R | matched | left-only | right-only | ambiguous | unavailable | coverage | expected/correct/incorrect/missed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in rows:
        metrics = result.report.metrics
        lines.append(
            f"| `{result.name}` | {metrics.left_total}/{metrics.right_total} | {metrics.matched} | {metrics.left_only} | {metrics.right_only} | {metrics.ambiguous_groups} | {metrics.unavailable} | {metrics.match_coverage:.3f} | {result.expected_matches}/{result.correct_matches}/{result.incorrect_pairs}/{result.missed_expected_matches} |"
        )
    lines.extend(
        [
            "",
            "## Human correspondence and observed failure patterns",
            "",
        ]
    )
    for result in rows:
        metrics = result.report.metrics
        lines.append(f"### `{result.name}`")
        lines.append("")
        lines.append(result.purpose)
        lines.append("")
        lines.append(
            f"Observed: {result.correct_matches} expected label matches, {result.incorrect_pairs} incorrect structural pair(s), and {result.missed_expected_matches} expected match(es) not confirmed. Branch-local correct/expected: {result.branch_local_correct}/{result.branch_local_expected}."
        )
        if result.repeated_shift is not None:
            shift = result.repeated_shift
            lines.append(
                "Repeated-sibling shift measurement: "
                f"later repeated siblings shifted={shift['later_repeated_shifted']}, "
                f"unmatched={shift['later_repeated_unmatched']}, "
                f"ambiguous_groups={shift['later_repeated_ambiguous_groups']}."
            )
        if metrics.unavailable:
            reasons = sorted(
                str(record["reason"]) for record in result.report.unavailable_spans
            )
            lines.append(f"Unavailable reasons: {', '.join(reasons)}.")
        lines.append("")

    lines.extend(
        [
            "## Conclusion",
            "",
            "Decision: **B. Suitable with small deterministic corrections**.",
            "",
            "The exact structural key is useful for near-identical reruns, timing variation, distinct fan-out-like branches, and unaffected branches beside a missing parent. It does not preserve human identity for repeated same-signature siblings when one call is inserted, removed, or reordered: later ordinal positions can shift into incorrect pairs. Duplicate exact sort keys are localized as ambiguity rather than guessed. These are practical limitations, not causal findings.",
            "",
            "V02-20 may proceed with the exact structural contract, but its review MUST preserve the explicit local ambiguity/left-only/right-only records and surface repeated-sibling ordinal limitations. No heuristic matching is introduced by V02-19. Any future suggested pairing would need a separate confidence layer and specification.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_markdown(evaluate_corpus()))
