# Compatibility

This page documents current TraceMotive support as declared by package
metadata and as validated by repository CI. It is not a promise that every
unlisted environment works.

## Python

- Declared minimum: Python `>=3.10` (`pyproject.toml`).
- CI-validated versions: Python `3.10` and `3.12`.
- Python `3.11` is accepted by metadata. It is not a current CI matrix version.

Do not treat every interpreter that satisfies `requires-python` as
CI-validated.

## Framework integrations

The only validated native framework integration is the public OpenAI Agents
SDK adapter.

- Validated extra range: `openai-agents>=0.17,<0.18`.
- Compatibility checks in this checkout cover `0.17.0`, `0.17.4`, and
  `0.17.8` for adapter callbacks, span-data fields, processor registration,
  model settings, and the example construction surface.
- A broader Dependabot proposal is not validated compatibility.

Generic Python tracing is available through the public manual SDK:

`configure`, `trace`, `span`, and `flush`.

That path is manual instrumentation. It is not a native adapter for an
arbitrary framework.

LangGraph is not currently supported.

No other framework is documented as supported merely because Canonical can
represent a trace.

## Node.js and frontend

Installed users do not need Node.js or npm. `tracemotive serve` serves the
packaged UI.

Node.js is a contributor and frontend-build concern. CI uses Node.js
`22.12.0`. The frontend package engines field is `^20.19.0 || >=22.12.0`.

## Operating systems

Blocking GitHub Actions validation runs on `ubuntu-latest`.

CI also runs the Python test suite with Python `3.12` on `windows-latest` and
`macos-latest` as a non-blocking evidence job. On Windows, that job runs the
suite a second time from a contributor virtual environment created with
`python -m venv`. A failure there does not fail CI, and a passing run is not a
support claim. Local Windows development has
occurred in this checkout, but Windows and macOS are not claimed as
CI-validated platforms until that job has a recorded green history on `main`
and is made blocking in the same change that updates this page.

## Current protocol and APIs

- Canonical schema: `0.1`
- Ingest protocol: `1`
- Query and comparison APIs present in this checkout: `/api/v1`, `/api/v2`,
  `/api/v3`, and `/api/v4/compare/{left}/{right}`

TraceMotive currently exposes API namespaces `/api/v1` through `/api/v4`.
`/api/v3` provides the investigation comparison surface, and `/api/v4`
provides the structured-diff projection. `/api/v1` and `/api/v2` remain
present. This page does not invent deprecation or a recommendation hierarchy.

## Local CLI

The installed console entry point supports:

```text
tracemotive compare LEFT_TRACE_ID RIGHT_TRACE_ID [--endpoint URL] [--json] [--open]
tracemotive last TRACE_NAME [--endpoint URL] [--json] [--open]
```

`last` requires an exact trace-name match, selects the two newest matches
observed in one query sequence, assigns older/left and newer/right, and does
not provide an atomic latest guarantee. If the two matches have the same
`started_at`, it refuses to infer order from `trace_id` and fails closed.
`--endpoint` accepts only an existing HTTP loopback TraceMotive server.
`--json` passes through the existing
`/api/v3` response without a new schema.
Exit codes are documented in the README.

## Not validated

The following are not current compatibility claims:

- LangGraph
- OpenTelemetry / OpenInference
- remote collectors
- authentication
- cloud or multi-user isolation
- Python versions outside the CI matrix, including untested `>=3.10`
  interpreters
- OpenAI Agents SDK `0.18` or newer
- macOS or Windows as formally validated platforms
