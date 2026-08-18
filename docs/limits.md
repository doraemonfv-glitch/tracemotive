# Limits

TraceMotive is intended for local individual debugging. Per-comparison
operations have explicit hard bounds. These numbers are safety and honesty
limits, not a supported-scale rating.

Large long-lived databases and fleet-scale deployments have not been
validated. This page does not invent a maximum database size, total trace
count, concurrent-user limit, or throughput claim.

## Comparison bounds

Source: `tracemotive/comparison.py`, reused by `/api/v2`, `/api/v3`, and
`/api/v4`.

| Bound | Value | When exceeded |
|---|---|---|
| Spans per side | `10,000` | comparison fails |
| Difference records | `4,096` | comparison fails |
| Comparison response | `4 MiB` | comparison fails |

These bounds raise `ComparisonTooLargeError`. HTTP comparison routes return
`413` with `comparison_too_large`. The result is not truncated into a partial
comparison.

`MAX_DIFFERENCE_RECORDS` counts emitted non-same trace-field records plus
span differences plus span uncertainties. `/api/v3` also applies the same
record ceiling to composed finding and uncertainty items.

## Structured-diff bounds

Source: `tracemotive/structured_diff.py`. These bounds produce a bounded
projection, not a comparison failure.

| Bound | Value | Scope | When reached |
|---|---|---|---|
| `max_depth` | `32` | per subtree | later in-bound siblings may still be emitted |
| `max_nodes` | `4,096` | global visited-node budget | remaining unvisited siblings are omitted |
| `max_change_records` | `256` | global output budget | no further change records are emitted |
| `max_value_bytes` | `64 KiB` | per candidate record | that record is omitted and remaining walk stops |

A truncated projection sets `truncated=true` and a reason of `max_depth`,
`max_nodes`, `max_change_records`, or `max_value_bytes`. Records collected
before the bound are kept.

## Related content bound

Captured span input/output is independently limited to `262,144` UTF-8
Canonical JSON bytes (`tracemotive/privacy.py`). Oversized captured content is
not stored as captured; the capture state becomes `not_captured` with
`size_limit`. That is a capture bound, not a comparison-scale rating.

## What these limits are not

They are not:

- a promise that every comparison below the bound is fast
- a database-capacity rating
- a multi-user or fleet-scale validation
- permission to infer identity, moves, or cause from a truncated diff
