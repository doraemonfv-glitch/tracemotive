"""V03-10 public-path baseline and deterministic oracle report helpers.

The baseline is captured through the existing public SDK and loopback
Collector/SQLite path.  The scenario variants and oracle remain private and
independent of any future V03-11 implementation.
"""

from __future__ import annotations

from tests.alignment_evaluation import capture_realistic_run
from tracemotive._evaluation.divergence import (
    DivergenceScenario,
    EvaluationRun,
    build_corpus,
    render_report,
    serialize_corpus,
)


def capture_public_baseline() -> EvaluationRun:
    """Capture one sanitized agent-like run through SDK -> HTTP -> SQLite."""

    captured = capture_realistic_run()
    return EvaluationRun(
        trace=captured.trace,
        spans=captured.spans,
        labels=dict(captured.labels),
        order=captured.order,
    )


def build_evaluation_corpus() -> tuple[DivergenceScenario, ...]:
    return build_corpus(capture_public_baseline())


def main() -> None:
    scenarios = build_evaluation_corpus()
    print(render_report(scenarios), end="")


if __name__ == "__main__":
    main()


__all__ = ["build_evaluation_corpus", "capture_public_baseline", "main", "serialize_corpus"]
