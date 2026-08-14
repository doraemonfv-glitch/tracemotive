# TraceMotive v0.2.0 release readiness

This checklist is local-only. These commands build and inspect artifacts; they
do not upload, publish, create a release, push a tag, or create a GitHub
Release.

The Python distribution and import package are both `tracemotive`, with
package version `0.2.0`. The Canonical schema remains `0.1`, the ingest
protocol remains `1`, and the comparison response uses version `0.2` under
`/api/v2`.

The declared runtime and optional dependencies are:

- core: `fastapi>=0.110,<1`
- `server`: `uvicorn>=0.30,<1`
- `openai-agents`: `openai-agents>=0.17,<0.18`

## Build and package

Run from a clean checkout. Node.js/npm are development and release-build
dependencies only; they are not runtime dependencies of an installed wheel.

```text
cd frontend
npm ci
npm test
npm run build:package
cd ..
python -m pip install build
python -m build --sdist --wheel
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

Verify `/`, `/api/v1/health`, trace list/detail/query behavior, `/api/v2`
comparison, packaged static assets, and a restart with the same database path.
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

Run all Python and frontend tests, packaging tests, and `git diff --check`.
Normal CI may keep the release-only installed-user smoke opt-in so regular
validation remains fast; V02-90 is required before declaring a v0.2 release
candidate ready.

The direct Uvicorn Collector factory remains available as a development and
v0.1 compatibility path. Programmatic `Repository()` and bare `create_app()`
still default to `:memory:`. The `tracemotive serve` command defaults to
persistent storage and accepts explicit `:memory:` when ephemeral behavior is
intentional.
