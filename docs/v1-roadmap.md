# TraceMotive v1.0 roadmap

Status: proposal. This document is planning material. It is not a Frozen
Specification, does not amend `spec/v0.1-frozen-spec.md` or `docs/v0.4/`, and
does not authorize any public-contract, schema, ingest, storage, or API change.
Items marked **needs approval** stay unimplemented until a maintainer approves
them explicitly.

Package version `1.0` is a distribution version only. It does not imply
Canonical schema `1.0`, ingest protocol `2`, or a new API namespace. Canonical
schema remains `0.1`, ingest protocol remains `1`, and `/api/v1` through
`/api/v4` remain unchanged unless a versioned migration is separately approved.

TraceMotive reports observed divergence. Nothing in this roadmap adds causal
inference, RCA, confidence scores, or a claim that a divergence caused a
failure.

## 1. Goal

Turn TraceMotive `0.6.0` into a reliable, approachable, local-first `1.0`
developer tool:

- a new user reaches a first useful comparison without reading source;
- the tool explains its own failures (server not running, wrong port, missing
  extra, unreadable database) in terms a user can act on;
- every platform or framework claim is backed by a CI job that actually ran;
- local data can be inspected and removed deliberately, with conservative
  privacy defaults unchanged.

## 2. Repository audit (evidence as of `b32a001`)

### 2.1 Architecture

The documented pipeline holds in source:

| Layer | Source | Observation |
|---|---|---|
| Framework → Adapter | `tracemotive/integrations/openai_agents.py` | Converts framework objects to Canonical before emit; framework objects do not cross. |
| Canonical | `tracemotive/canonical/models.py` | `source.framework` is a free string (`models.py:604`), so a new adapter needs no schema change. |
| Privacy | `tracemotive/privacy.py` | Redaction/sanitization precedes queue ownership. |
| Transport | `tracemotive/transport.py` | Bounded queue; `validate_loopback_endpoint` (`transport.py:53`) rejects non-loopback hosts without DNS. |
| Collector | `tracemotive/collector.py` | `create_app` refuses any bind host except `127.0.0.1` (`collector.py:761`). |
| SQLite | `tracemotive/storage/` | One connection guarded by an `RLock`; forward-only migrations (version `1`). |
| Query API | `tracemotive/query.py`, `api_v3.py`, `api_v4.py` | `/api/v1`–`/api/v4`. |
| UI | `frontend/src/` | Consumes the Query API only; renders captured values as text. |
| CLI | `tracemotive/cli.py`, `local_client.py`, `demo.py` | `serve`, `demo`, `compare`, `last`. |

The boundaries are sound. The main risks are in validation evidence,
first-run ergonomics, and data lifecycle, not in the core pipeline.

### 2.2 Baseline test results (Linux, Python 3.11, this audit)

| Suite | Result |
|---|---|
| `python -m unittest discover -s tests` from the documented contributor venv | 430 run, 3 skipped (release-only), **1 error**: `BuiltArtifactPackagingTests.setUpClass` |
| `cd frontend && npm test` | 80 passed (8 files) |

The error is reproducible on any machine that follows the README/CONTRIBUTING
contributor setup (see R2).

### 2.3 Release blockers and known failures

**R1 — Windows `WinError 32` during demo test cleanup (known, open).**
`docs/release-readiness.md` records two `test_demo` errors on local Windows:
`TemporaryDirectory` cleanup fails because the SQLite file is still open. Both
failing sites (`tests/test_demo.py` class teardown and the fresh-process loop)
start `tracemotive serve` via `sys.executable`, call `terminate()`/`wait()`,
and then delete the database directory immediately.

Likely root cause (not reproducible on Linux, which allows unlinking open
files): on Windows, a virtual environment's `Scripts\python.exe` is a
launcher that runs the base interpreter as a child. `Popen.wait()` observes
the launcher's exit. The child that actually holds the SQLite handle and the
listening socket is torn down asynchronously afterwards, so an immediate
directory delete can hit a sharing violation. The same stop-then-delete
pattern exists in `tests/test_packaging.py` and
`tests/test_v02_p0_fullstack.py`.

**R2 — Installed-wheel gate cannot run from the documented contributor
setup.** `tests/test_packaging.py` creates its validation venv with
`--system-site-packages` and installs the wheel with `--no-deps`. That reuses
the *base* interpreter's site-packages, not the contributor's venv, so
`fastapi`/`uvicorn` are missing whenever tests run from a venv, which is what
README and CONTRIBUTING tell contributors to do. CI passes only because it
installs dependencies into the runner's base interpreter.

**R3 — No CI evidence for Windows or macOS.** `.github/workflows/ci.yml` runs
on `ubuntu-latest` only. `docs/compatibility.md` correctly says Windows and
macOS are not formally validated, but there is no job that could produce that
evidence.

**R4 — The OpenAI Agents compatibility claim is not re-verified by CI.** Every
adapter test (`tests/test_openai_agents.py`,
`tests/test_openai_agents_install.py`, `tests/test_issue14_example.py`) uses
local fixtures or a fake `agents` module. CI never installs
`openai-agents`. The documented checks at `0.17.0`, `0.17.4`, and `0.17.8`
were maintainer runs that no workflow reproduces. This is the most important
evidence gap against the rule "never claim framework support without real
tests".

**R5 — Test-harness defect.** `tests/test_documentation.py` has its
`if __name__ == "__main__": unittest.main()` block *before*
`CompatibilityLimitsStorageTests`. Running the file directly skips that class
without warning. Discovery and CI are unaffected.

### 2.4 Technical debt

| ID | Debt | Evidence | Impact |
|---|---|---|---|
| D1 | Trace list is a full table scan filtered in Python | `repository.py:572-592` loads every trace row, then applies status/name filters and slices | List, UI selection, and `last` get slower as the database grows. `last` pages 100 at a time and rescans the table for every page. |
| D2 | No database lifecycle tooling | `docs/storage.md`: only per-trace `DELETE /api/v1/traces/{id}`; no UI delete, bulk delete, size report, or vacuum | Users cannot see how much they have captured or remove it deliberately. |
| D3 | CLI failures lack diagnosis | `cli.py` prints `could not bind`, `local TraceMotive server request failed`, `startup or server failure` without the port, cause, or next step; no `--version` | First-run failures are hard to act on. |
| D4 | Health endpoint carries no identity | `/api/v1/health` returns only `{"status":"ok"}` (Frozen) | A client cannot tell which TraceMotive version or database it reached. Changing this needs a new contract (needs approval). |
| D5 | Lint scope is minimal | `pyproject.toml` Ruff selects only `E9`, `F`, with per-file ignores | Acceptable today; widen gradually rather than in one pass. |
| D6 | Duplicated subprocess-server helpers | Separate start/stop/health loops in `test_demo.py`, `test_packaging.py`, `test_v02_p0_fullstack.py` | Cross-platform fixes must be repeated; this caused R1 to exist in three places. |
| D7 | Python 3.11 and 3.13 are not in the matrix | `ci.yml` matrix is `3.10`, `3.12` | `requires-python = ">=3.10"` is broader than the evidence. |
| D8 | Onboarding assumes manual trace selection | `frontend/src/onboarding.tsx` asks the user to reload and select two traces | The CLI already prints a deep link; the UI empty state does not use `last`-style selection. |

### 2.5 Scope conflicts that need a decision

These priorities conflict with the current authority chain. They are recorded
here, not resolved by this roadmap.

| Priority | Conflict | Decision needed |
|---|---|---|
| LangGraph integration | Frozen spec §2.2 excludes LangGraph for v0.1; `AGENTS.md` excludes "Additional framework adapters"; `docs/v0.4/v0.4-scope.md` made it conditional on a full GO gate. | Maintainer approval of an `AGENTS.md` scope amendment and a named version range before any adapter code lands. |
| Retention | Frozen spec deferred list includes "retention policies" and "trace archival". | User-initiated deletion through the existing `DELETE` semantics is in contract. Any *automatic* or scheduled retention needs approval. |
| New CLI/API surfaces | `AGENTS.md`: "Do not invent a public contract that is absent from the specification." | New CLI subcommands and flags are new public surface; each milestone lists them for approval. No new HTTP endpoints are proposed. |

## 3. Milestones

Each milestone is small enough to review in one pull request (or a short
series), keeps all Frozen contracts, and has explicit exit criteria.

### M1 — Reliability baseline (implemented on this branch)

Scope: test-harness and CI reliability only. No product behavior, public API,
schema, ingest, or storage change.

| Task | Priority |
|---|---|
| M1.1 Shared subprocess-server stop helper: terminate → bounded wait → kill, then wait until the loopback port stops accepting connections. | P0 |
| M1.2 Shared bounded-retry directory removal for Windows sharing violations (`PermissionError`), raising after a deadline and never ignoring errors silently. | P0 |
| M1.3 Apply M1.1/M1.2 to `test_demo.py` (R1), and cleanup in `test_packaging.py` and `test_v02_p0_fullstack.py`. | P0 |
| M1.4 Make the installed-wheel gate run from a contributor venv by exposing that venv's dependency directories to the validation venv. The wheel stays `--no-deps`, and the existing assertion that `tracemotive` imports from the validation venv's own site-packages is unchanged (R2). | P0 |
| M1.5 Add a non-blocking Windows/macOS Python test job to CI (R3). | P1 |
| M1.6 Fix the misplaced `__main__` block (R5). | P2 |
| M1.7 Consistency test: while the cross-platform job is non-blocking, live docs must not claim Windows/macOS validation. | P1 |

Acceptance criteria:

- Full Python discovery passes from the documented contributor venv on Linux
  (no packaging `setUpClass` error).
- Helper unit tests prove retry-then-success, bounded failure, and immediate
  propagation of non-sharing errors.
- No production module under `tracemotive/` changes.
- The cross-platform job is `continue-on-error: true`, and
  `docs/compatibility.md` still says Windows and macOS are not formally
  validated.

Local verification on this branch (Linux, Python 3.11 contributor venv):
full discovery 452 run, 3 release-only skips, 0 failures/errors; the
installed-wheel packaging gate passes (18/18); the release-only full-stack
test passes with `TRACEMOTIVE_RUN_V02_22=1` (3/3); running
`tests/test_documentation.py` directly now executes 18 tests instead of 14.
The Windows fix has not been executed on Windows yet. The M1.5 job is the
first place it will be.

Exit to "platform validated" (separate, later decision): the cross-platform
job is green on `main` for 10 consecutive runs. Then make it blocking and
update `docs/compatibility.md` in the same change.

### M2 — First run and CLI diagnostics

| Task | Priority | Contract impact |
|---|---|---|
| M2.1 `tracemotive --version` | P0 | New CLI flag (needs approval, low risk) |
| M2.2 `tracemotive doctor`: offline, read-only checks for Python version, package version, `server` extra, packaged UI, resolved database path (location, exists, writable, migration version), and an optional loopback health probe of `--endpoint`. Never prints environment values or database content. | P0 | New CLI subcommand (needs approval) |
| M2.3 Actionable CLI errors: include port and next step for bind failure (port in use → `--port`), server unreachable (→ `tracemotive serve`), missing extra (→ exact install command). Keep existing exit codes 0–6. | P0 | Message text only; exit codes unchanged |
| M2.4 UI empty state: when two or more traces share an exact name, offer "Compare latest two" using the existing `/api/v1/traces` list with the same exact-name and equal-`started_at` fail-closed rules as `last`. | P1 | UI only; no API change |
| M2.5 Comparison UI usability: persistent left/right labels with trace name and start time, keyboard navigation between findings, copyable CLI reproduction command (`tracemotive compare L R`), and clearer "unknown" and "capture unavailable" copy. | P1 | UI only |

Acceptance criteria: each diagnostic path has a CLI test that asserts exit
code and message; `doctor` output contains no environment variable values or
captured content (asserted with sentinel secrets); frontend tests cover the
new UI states; the non-causality wording is preserved verbatim where it
already exists.

### M3 — Local data lifecycle

All operations are explicit and user-initiated. Defaults stay unchanged:
tracing off, capture off, no automatic deletion.

| Task | Priority | Contract impact |
|---|---|---|
| M3.1 `tracemotive traces list [--name] [--limit]` over the existing Query API | P0 | New CLI subcommand (needs approval) |
| M3.2 `tracemotive traces delete TRACE_ID...` through existing `DELETE /api/v1/traces/{id}` | P0 | New CLI; existing API semantics, including recreate-on-late-ingest (§50.1), documented |
| M3.3 `tracemotive prune --older-than DURATION` with `--dry-run` as the default and `--yes` to act; loops over the existing DELETE | P1 | New CLI; not an automatic retention policy |
| M3.4 `tracemotive db info` (path, size, trace count) and `tracemotive db vacuum` (offline, refuses while a server holds the file) | P1 | New CLI; storage-internal |
| M3.5 UI delete control with confirmation on trace detail | P2 | UI only; existing DELETE |
| M3.6 D1 fix: push status/name filters and paging into SQL with an index on `(started_at_us DESC, trace_id)`. Needs a forward-only migration `2`, adding an index only, with byte-identical responses. | P1 | Storage migration (needs approval) |

Acceptance criteria: deletion tests prove spans, span I/O, and ingest events
are removed and that `--dry-run` deletes nothing; prune refuses non-loopback
endpoints; the migration test proves that a version-1 database upgrades, that
results are unchanged, and that a newer database is still refused; docs state
that deletion is not secure erasure.

Explicitly not in M3 without approval: scheduled or automatic retention,
archival, encryption at rest, tombstones.

### M4 — Optional LangGraph integration (gated on the §2.5 decision)

Design, pending approval:

- Optional extra `tracemotive[langgraph]` with a narrow, named version range
  chosen from the versions actually tested.
- Adapter `tracemotive.integrations.langgraph` built on LangChain callback
  handlers (chain, chat-model/LLM, and tool start/end/error). It maps `run_id`
  and `parent_run_id` into in-process native-ID maps and emits Canonical
  spans with fresh Canonical IDs, following §3.1 and §7. The framework
  string is `langgraph`; unmapped concepts become `custom` with
  `source_type`, so no Canonical change is needed.
- Capture stays off by default; values pass through the shared privacy
  boundary before enqueue; handler exceptions are swallowed and never raised
  into graph execution.
- Importing `tracemotive` must not import `langgraph`.

Real compatibility tests (required before any support wording):

- A CI job installs each named LangGraph version and runs a real `StateGraph`
  with a deterministic in-process chat model (no network, no API key). The
  job asserts the exact Canonical span tree, capture-off absence, redaction
  of seeded credentials, hostile/oversized values, and that an adapter fault
  does not fail the graph.
- An installed-wheel smoke with the extra.
- The same job pattern is added for OpenAI Agents to close R4, using a real
  `Runner` with an in-process fake model.

Acceptance criteria: until the GO gate passes on CI, live docs keep
"LangGraph is not currently supported." The release-consistency test already
enforces that.

### M5 — Cross-platform CI, security regressions, installed-wheel E2E

| Task | Priority |
|---|---|
| M5.1 Promote the M1.5 job to blocking after its green-run threshold; add Python 3.11 and 3.13 to the Linux matrix (D7) and update `docs/compatibility.md` in the same change. | P0 |
| M5.2 Installed-wheel E2E job: build once, install the wheel into a clean venv with real dependency resolution on Linux, Windows, and macOS, then run `serve` → SDK ingest → `last --json` → UI asset fetch → restart persistence. | P0 |
| M5.3 Security regression suite: non-loopback endpoints (`0.0.0.0`, IPv6 any, DNS names, credentials in URL), static traversal and `.map` denial, no permissive CORS, sentinel-secret redaction across SDK → SQLite (main DB plus `-wal`/`-journal`), inert hostile content in UI tests, and no secrets in CLI stderr. | P0 |
| M5.4 Framework compatibility jobs from M4 (OpenAI Agents now, LangGraph when approved). | P0 |

### M6 — v1.0 release gate

Package `1.0.0` is proposed only when M1–M3 and M5 are complete, M4 is either
GO or explicitly recorded as not supported, and the gate below passes.

## 4. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Windows fix addresses the wrong root cause | Medium | M1 adds both a port-release wait and bounded retry; the Windows CI job turns the hypothesis into evidence. |
| New CLI surface becomes an unplanned public contract | Medium | Every new subcommand is listed for approval; `--json` output reuses existing API responses instead of new schemas. |
| LangGraph API churn breaks the adapter | High | Narrow version range, real-framework CI per named version, fail-closed handler, no support claim before GO. |
| Bulk deletion races with delayed ingest | Certain (by spec §50.1) | Document that traces may reappear; no tombstones without approval. |
| Storage index migration alters ordering | Low | Byte-identical response tests on a fixed corpus before and after migration. |
| Divergence wording drifts toward causality | Medium | Keep the existing non-causality assertions; add a lint-style test for causal vocabulary in UI strings. |
| CI minutes and flakiness grow with the matrix | Medium | Non-blocking evidence phase first; no retries that hide real failures. |

## 5. Test strategy

- **Specification-derived tests remain authoritative.** No existing assertion
  is weakened. The V03-10 corpus must keep false-confident meaningful = 0 and
  false-confident starting point = 0.
- **Layered evidence.** Unit tests (pure logic), in-process API tests (FastAPI
  test client), subprocess tests (real `serve`), installed-wheel tests (clean
  venv), and real-framework tests (named versions, offline models).
- **Platform claims come from CI only.** A platform or framework is named as
  validated only after a job for it exists and is green on `main`.
- **Security regressions are tests.** Every invariant in `AGENTS.md`
  (loopback-only, off-by-default, redaction before queue, inert content, no
  secrets in logs) has at least one direct test, and new surfaces (M2 doctor,
  M3 prune) add their own.
- **Deterministic and offline.** No test needs an API key or external network.
  Real-framework jobs use in-process models.

## 6. v1.0 acceptance criteria (summary)

1. Canonical `0.1`, ingest `1`, and `/api/v1`–`/api/v4` responses are
   unchanged or changed only through an approved versioned migration.
2. Python suites pass on Linux, Windows, and macOS in blocking CI.
3. The installed-wheel E2E passes on all three platforms.
4. Every named framework has a real-framework CI job; unnamed frameworks stay
   unsupported in live docs.
5. `tracemotive doctor` diagnoses the top first-run failures offline.
6. Users can list, delete, prune (dry-run by default), and size their local
   data without automatic deletion.
7. Security regression suite is green; no new network listener beyond
   `127.0.0.1`.
8. No UI, CLI, or documentation text states or implies that a divergence
   caused a failure.
