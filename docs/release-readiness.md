# TraceMotive v0.5.0 release readiness

This checklist is local-only. These commands build and inspect artifacts; they
do not upload, publish, create a release, push a tag, or create a GitHub
Release.

The Python distribution and import package are both `tracemotive`, with
package version `0.5.0`. The Canonical schema remains `0.1`, the ingest
protocol remains `1`, `/api/v1`, `/api/v2`, and `/api/v3` are preserved, and
the additive structured-diff comparison contract is `/api/v4`. LangGraph
is not currently supported and is not part of the v0.5.0 support claim.

## v0.5.0 release notes

v0.5.0 is an adoptability release. It does not add a new API namespace, change
Canonical schema, or change ingest protocol.

Find where two AI agent runs first differ in observed behavior, then jump
directly into the supporting evidence — locally, without pretending to know
the cause.

### Added / Improved

- Identified investigations can surface a bounded set of additional supported
  observations from existing evidence. These are not downstream consequences,
  causal chains, or later-in-time events.
- Installed-user onboarding uses packaged `tracemotive serve` and
  `tracemotive demo` without a checkout, Node.js, or npm at runtime.
- Release-consistency validation keeps live current-version claims aligned
  with `pyproject.toml`.
- CI validates Python 3.10 and 3.12, frontend tests and production build,
  Ruff, and release consistency.
- Security-maintenance baseline: weekly Dependabot, isolated `pip-audit`,
  `npm audit --audit-level=high`, and conservative threat-model documentation.
- Compatibility, limits, and storage documentation match source-backed
  current behavior.
- V03-10 evidence-conservative regression remains 30 scenarios with
  false-confident meaningful = 0 and false-confident starting point = 0.
- Installed wheel/sdist dogfood and the V05-08 final release gate completed
  with GO for v0.5.0 release preparation.

### Important limitations

- An observed divergence is evidence for investigation, not causal proof.
- TraceMotive does not provide RCA, confidence scoring, reconvergence,
  recovery detection, or harmlessness detection.
- Loopback is not authentication. Local SQLite is not encrypted at rest.
- There is no automatic retention policy.
- LangGraph is not currently supported.
- No formal security audit has been completed. Point-in-time dependency
  audits being clean is not a permanent security guarantee.

### V05-08 pre-release evidence

Recorded against commit `7c47b9f5f22972a80ab9233489db363ef7f1f004` before this
version-preparation bump. Local Windows environmental limitations are not
converted to PASS.

| Check | Result |
|---|---|
| Wheel install outside checkout | PASS |
| Sdist install outside checkout | PASS |
| Packaged UI from installed artifact | PASS |
| Identified demo | PASS |
| Uncertain demo | PASS |
| OpenAI Agents resolved version | `0.17.8` in `>=0.17,<0.18` |
| Isolated Python `pip-audit --strict` | PASS |
| `npm audit --audit-level=high` | PASS |
| V03-10 oracle | 30 scenarios; meaningful 15/6/9; starting 14/7/9 |
| False-confident meaningful | 0 |
| False-confident starting point | 0 |
| GitHub Actions Python 3.10 / 3.12 | PASS |
| Frontend tests / production build | PASS |
| Private Vulnerability Reporting setting | MANUAL VERIFICATION |
| Local Windows esbuild packaging bootstrap | known local-only limitation; Ubuntu CI is authoritative |

The historical v0.4.1 and v0.4.0 notes below are preserved as historical
release evidence. They are not current package-version claims.

## v0.4.1 release notes

### Fixed

- Corrected structured-diff truncation semantics.
- `max_depth` now skips only the bounded subtree so later in-bound siblings
  remain visible.
- Global node, record, and value budgets stop the remaining walk
  deterministically.

### Clarify

- No API contract changes.
- No new features.
- No Canonical or ingest changes.

## v0.4.0 release notes

### Added / Improved

- Fresh-checkout bootstrap and installed packaging end-to-end validation.
- A minimal investigation cockpit with stable left/right span navigation.
- A conservative structured JSON diff and additive `/api/v4` comparison
  contract.
- First-run onboarding with deterministic local instructions.
- Deterministic identified and uncertain demo scenarios.
- README and evaluation visibility improvements.
- Trusted Publishing release provenance through GitHub Actions and PyPI's
  account-level publisher configuration.

### Important limitations

- An observed divergence is evidence for investigation, not causal proof.
- TraceMotive does not provide RCA.
- Arrays do not infer identity or moves.
- Redacted or unavailable capture can prevent a structured diff.
- The validated framework integration remains the OpenAI Agents SDK.
- LangGraph is deferred and is not claimed as supported in v0.4.0.

The declared runtime and optional dependencies are:

- core: `fastapi>=0.110,<1`
- `server`: `uvicorn>=0.30,<1`
- `openai-agents`: `openai-agents>=0.17,<0.18`

## Build and package

Run from a clean checkout. Node.js/npm are development and release-build
dependencies only; they are not runtime dependencies of an installed wheel.

```text
python scripts/bootstrap.py
cd frontend
npm test
cd ..
python -m pip install build
python -m build --sdist --wheel --no-isolation
```

Inspect both artifacts in `dist/`. The wheel must contain the package-owned
`tracemotive/ui/index.html` and current JavaScript/CSS assets, the CLI, Query
API, comparison, storage, and UI resource modules. It must not require
`frontend/`, `frontend/dist/`, `node_modules/`, or repository paths at runtime.

## Fresh installed-user smoke

Create the isolated environment outside the repository and install the wheel
with the server capability:

```text
python -m venv <temporary-venv>
python -m pip install "dist/<wheel-file>.whl[server]"
python -c "import tracemotive; print(tracemotive.__file__)"
```

From a temporary working directory, start:

```text
tracemotive serve
```

The installed command must bind only to `127.0.0.1:8765` by default, serve the
packaged UI and the existing Query/ingest API from the same origin, and use the
persistent database path resolver. It must not require Node.js, npm, Vite, the
frontend source tree, or the repository checkout.

Verify `/`, `/api/v1/health`, trace list/detail/query behavior, `/api/v2`,
`/api/v3`, and `/api/v4` comparison behavior, packaged static assets, and a
restart with the same database path.
Create the traces through public SDK paths and confirm that committed traces
remain available after restart. The comparison smoke must preserve localized
`exact_match`, `left_only`, `right_only`, `ambiguous_group`, and `unavailable`
semantics and must not report repeated same-signature ordinal pairs as exact.

The release-only full-stack test is available with:

```powershell
$env:TRACEMOTIVE_RUN_V02_22 = "1"
python -m unittest tests.test_v02_p0_fullstack -v
```

## Security and privacy gate

Confirm that `tracemotive serve` has no `--host` option and never binds to
`0.0.0.0`, a LAN address, or a remote hostname. Confirm static traversal,
directory listing, permissive CORS, analytics, and external TraceMotive
telemetry are absent.

Confirm `capture_content=False` remains the default, redaction occurs before
the transport queue, and hostile captured content remains inert. Inspect the
SQLite main database and any created journal/WAL/SHM sidecars for prohibited
secrets. TraceMotive does not provide or claim local database encryption.

## Compatibility and regression gate

Run the V02-19 alignment evaluation and production comparison regressions. The
insertion/removal/reorder corpus must keep the historical 4/2/4 incorrect
ordinal matches at zero in production comparison output.

Run the full Python unittest discovery, the full frontend test suite and
production build, divergence evaluation, demo/API/structured-diff/bootstrap
tests, packaging and fresh-checkout E2E, documentation, release-workflow, and
privacy/security tests, plus `git diff --check`.

The release candidate must retain Canonical schema `0.1`, ingest protocol `1`,
and the existing `/api/v1`, `/api/v2`, and `/api/v3` response contracts. `/api/v4`
is additive only. OpenAI Agents SDK compatibility remains evidence-based and
must match the tested dependency range.

The direct Uvicorn Collector factory remains available as a development and
v0.1 compatibility path. Programmatic `Repository()` and bare `create_app()`
still default to `:memory:`. The `tracemotive serve` command defaults to
persistent storage and accepts explicit `:memory:` when ephemeral behavior is
intentional. Serving remains loopback-only.
