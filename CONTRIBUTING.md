# Contributing to TraceMotive

## Contract and current scope

The [Frozen v0.1 Specification](spec/v0.1-frozen-spec.md) remains the
highest-authority compatibility contract for Canonical schema, ingest,
privacy, transport, and v1 behavior. The current checkout also contains the
additive TraceMotive v0.4 Core implementation through the accepted bootstrap,
packaging, cockpit, structured-diff, onboarding, and identified/uncertain-demo
issues. The frozen implementation design is under [docs/v0.4](docs/v0.4/).

The package metadata is `0.4.1` for the current release candidate. Version
bumps and publication remain explicit release tasks.

`AGENTS.md` contains repository implementation rules.

- Do not add Deferred or explicitly excluded features to the Frozen v0.1
  contract or the current v0.4 Core.
- If the specification is ambiguous, do not invent a public contract; open an
  Issue to clarify it first.
- The long-term vision is non-normative and is not a basis for adding v0.1 or
  v0.4 functionality.

## Development setup

Recommended versions:

- Python `3.10` or newer; Python `3.12` matches the development validation and CI.
- Node.js `^20.19.0 || >=22.12.0`; Node.js `22.12.0` matches CI.

Clone the repository and create a virtual environment:

```text
git clone https://github.com/doraemonfv-glitch/tracemotive.git
cd tracemotive
python -m venv .venv
```

Activate the environment using the command for your shell:

PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

POSIX shells:

```sh
. .venv/bin/activate
```

Install the Python development dependencies:

```text
python -m pip install --upgrade pip
python -m pip install "setuptools>=77,<84" wheel build
python -m pip install -e ".[server]"
```

Prepare the packaged frontend from the repository root using the canonical
bootstrap command:

```text
python scripts/bootstrap.py
```

This runs the locked `npm ci` and `npm run build:package` sequence and writes
only the disposable ignored generated UI assets.

The normal contribution validation does not require an OpenAI API key or the
optional OpenAI Agents SDK dependency. The real OpenAI Agents example is a
separate, API-backed example and is not part of the normal CI path.

## Local validation

From the repository root:

```text
python -m unittest discover -s tests -v
```

Frontend validation:

```text
python scripts/bootstrap.py
cd frontend
npm test
```

## Continuous integration

GitHub Actions runs on pushes to `main` and pull requests targeting `main`.
The workflow has two jobs:

- Python tests on Python 3.12, including the repository dependencies and
  packaging tools.
- Frontend tests and a production build on Node.js 22.12.0.

CI does not require secrets or an OpenAI API key. A passing CI run is a basic
requirement for a pull request.

## Making changes

- Keep changes small and focused; do not mix unrelated refactors into a fix.
- Read the relevant Frozen Specification sections before editing.
- Do not delete or weaken tests to make an implementation pass.
- Keep framework objects within the integration adapter boundary. The Collector
  must remain framework-agnostic.
- The frontend must use the Query API and must not access SQLite directly.
- TraceMotive is disabled by default, and content capture is independently
  disabled by default.
- Redaction must happen before events enter the transport queue.
- The Collector is loopback-only. Never document or configure the server to
  bind to `0.0.0.0`.
- Tracing failures must not fail the user's Agent execution.
- Do not include secrets, personal data, or real captured Agent content in
  fixtures, Issues, pull requests, or logs.

Historical v0.1 exclusions include Replay, RCA, Eval or Benchmark, distributed
tracing, additional framework adapters, cloud or hosted backends,
authentication, and remote collectors. Current v0.4 Core also does not claim
LangGraph support, a full demo catalog, causal diagnosis, ranking, confidence,
or a hosted service; LangGraph is not currently supported.

## When to open an issue first

Open an Issue before implementing changes that may affect a contract or an
architecture boundary, including:

- the public API or canonical schema;
- the ingest protocol or Query API;
- SQLite schema or storage semantics;
- privacy, redaction, or content-capture defaults;
- transport, retry, timeout, or failure-isolation behavior;
- new framework integrations;
- dependency, runtime, or CI policy; or
- a Deferred feature or an interpretation of the Frozen Specification.

Documentation fixes, test additions, and clear small bug fixes within the
existing contract do not necessarily require an Issue first.

## Pull requests

Keep pull requests focused and include at least:

- What changed
- Why it changed
- The relevant specification section or Issue
- Validation performed, including tests and builds

For UI changes, include a screenshot or GIF when useful. For privacy or
security-related changes, describe the impact and the validation performed.

## Security

Do not post suspected security vulnerabilities in a public Issue or pull
request. Do not publish secrets or personal data. Formal security reporting
guidance will be defined separately; do not use public contribution channels
for sensitive reports.
