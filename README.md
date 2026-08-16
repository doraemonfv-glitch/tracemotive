# TraceMotive

TraceMotive v0.3 is a local-first tracing and debugging tool for AI agent
execution. It records sanitized Canonical traces and spans, keeps them in a
persistent local SQLite Collector, and helps compare two observed runs by
showing the first evidence-supported behavioral divergence and a safe place to
begin investigating.

![TraceMotive demo](https://raw.githubusercontent.com/doraemonfv-glitch/tracemotive/main/docs/assets/tracemotive-demo.gif)

The long-term vision describes TraceMotive as “the causal debugger for AI
agents”. The current v0.3 product provides evidence-supported behavioral
divergence, deterministic findings, an investigation summary, and the
existing v0.2 structural detail view. It does not provide automatic RCA,
causal proof, replay, cloud sync,
authentication, remote collectors, or additional framework adapters. See [the
long-term vision](https://github.com/doraemonfv-glitch/tracemotive/blob/main/docs/long-term-vision.md)
for non-normative future context.

## Release and distribution status

The Python distribution and import package are both `tracemotive`. Releases
are distributed on PyPI as `tracemotive`. This checkout declares package
version `0.3.0`. After publication, normal users can install the released
package from PyPI as described below. The fresh-checkout instructions later in
this README are for contributors and local development.

The OpenAI Agents SDK range supported by this release is `>=0.17,<0.18`; that
same range is declared in `pyproject.toml`.

## Install from PyPI

For a normal installation of the released package:

```text
pip install tracemotive
```

If you will run the local Collector, install the `server` extra:

```text
pip install "tracemotive[server]"
```

The OpenAI Agents SDK integration is optional. To use it, install the
`openai-agents` extra:

```text
pip install "tracemotive[openai-agents]"
```

To use both the Collector and the OpenAI Agents SDK integration, install both
extras together:

```text
pip install "tracemotive[server,openai-agents]"
```

## Requirements

- Python 3.10 or newer. The package metadata declares `Requires-Python >=3.10`;
  this checkout was locally validated with Python 3.12.
- Node.js and npm are needed only for frontend development and release builds;
  normal installed-wheel users do not need them.
- An OpenAI API key is needed only for the real OpenAI Agents example, not for
  the deterministic test suite or core SDK smoke.

## Quick trial — deterministic v0.3 investigation demo

This is a short first-run path for evaluation. It uses the deterministic Python
SDK trace below, so it does not require an OpenAI API key. The released wheel
contains the production UI; Node.js/npm are not required.

1. In a fresh Python environment, install the server extra:

   ```text
   python -m pip install "tracemotive[server]"
   ```

2. Start the single-command local experience:

   ```text
   tracemotive serve
   ```

   It binds only to `127.0.0.1:8765`, serves the packaged UI and APIs from the
   same origin, and stores sanitized traces in the platform-safe persistent
   database path. Use `tracemotive serve --db :memory:` only for an explicit
   ephemeral session.

3. In a second terminal, run the deterministic demo seed command:

   ```text
   tracemotive demo
   ```

   It creates a fresh reference/changed pair through the public SDK and local
   Collector path. No model, API key, or external network request is made.

4. Open the printed comparison URL in a browser. The v0.3 investigation view
   should show the first supported policy-output observation, later observed
   evidence, and uncertainty/context boundaries without claiming causality.
   The detailed v0.2 comparison remains available below the insight.
   Each invocation creates a fresh pair; if a later seed step fails, any
   already-persisted demo trace remains untouched and can be inspected or left
   in place for a subsequent run.
   [OpenAI Agents example](#openai-agents-sdk-integration-and-example) is the
   alternative path if you want to try a real framework integration; it makes
   a real model request and requires its documented extra and API key.

5. Share what worked, what was unclear, and what you expected to see in a
   [GitHub Issue](https://github.com/doraemonfv-glitch/tracemotive/issues).
   Include your OS and Python/Node versions plus sanitized setup output, and
   do not include API keys or other credentials. Use
   [SECURITY.md](https://github.com/doraemonfv-glitch/tracemotive/blob/main/SECURITY.md)
   for security-sensitive reports.

## Install from a fresh checkout

For contributors and local development, create and activate a virtual
environment, then install the package from this repository and the Uvicorn
server extra.

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[server]"
```

POSIX shells:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[server]"
```

The core install contains FastAPI only as its third-party runtime dependency.
The `server` extra adds Uvicorn. The optional `openai-agents` extra is not
installed by the core path:

```text
python -m pip install -e ".[server,openai-agents]"
```

This is a development install from a repository checkout, distinct from the
PyPI installation above.

## Start the local server

For normal use, start both the Collector and packaged production UI together:

```text
tracemotive serve
```

The server is loopback-only and has no configurable host. It always binds to
`127.0.0.1`; remote, LAN, and `0.0.0.0` serving are not supported. Check that
it is ready at `http://127.0.0.1:8765/api/v1/health`.

`tracemotive serve` uses persistent SQLite by default. The path precedence is
explicit `--db`, `TRACEMOTIVE_DB`, then the platform-safe default documented in
the [release readiness checklist](https://github.com/doraemonfv-glitch/tracemotive/blob/main/docs/release-readiness.md).
Startup/path failures are explicit; the server never silently falls back to
`:memory:` or another port.

For development and v0.1 compatibility, the direct Uvicorn Collector factory
remains available:

```text
python -m uvicorn tracemotive.collector:create_app --factory --host 127.0.0.1 --port 8765
```

Programmatic `Repository()` and bare `create_app()` calls still default to
SQLite `:memory:` for v0.1 compatibility.

## Frontend development

The frontend development server is for contributors and frontend work only:

```text
cd frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to the loopback Collector.
The production UI is embedded in the Python wheel and served by
`tracemotive serve`; normal users do not need this development workflow.

## Minimal Python SDK usage

TraceMotive and content capture are independently disabled by default. A minimal
local trace is:

```python
import tracemotive

tracemotive.configure(
    enabled=True,
    endpoint="http://127.0.0.1:8765",
    capture_content=False,
)

with tracemotive.trace("demo"):
    with tracemotive.span("work"):
        pass

tracemotive.flush()
```

The stable Python SDK surface remains `configure`, `trace`, `span`, and `flush`.
Tracing failures, an unavailable Collector, and queue overflow do not fail the
instrumented Agent execution.

## OpenAI Agents SDK integration and example

For a PyPI installation, install the optional integration in the active
environment:

```text
pip install "tracemotive[openai-agents]"
```

If the Collector is also being installed from PyPI, use
`tracemotive[server,openai-agents]` instead.

The supported range is `openai-agents>=0.17,<0.18`. Compatibility probes were
run against versions 0.17.0, 0.17.4, and 0.17.8 for the tracing processor
callbacks, span-data fields, processor registration functions,
`ModelSettings.tool_choice`, and the example's `Agent` settings.

Set `OPENAI_API_KEY` in the shell used to run the example. PowerShell and POSIX
examples are:

```powershell
$env:OPENAI_API_KEY = "<your-key>"
```

```sh
export OPENAI_API_KEY="<your-key>"
```

With the Collector already running, execute:

```text
python -m examples.openai_agents_example
```

The example uses `local_only=True`, which replaces the OpenAI Agents SDK
global tracing processor list with TraceMotive. This controls framework tracing
processors; it does not make model traffic local. OpenAI model requests may
still leave the machine. With `local_only=False`, existing OpenAI or
third-party processors remain active and may export framework traces
remotely. See [the integration notes](https://github.com/doraemonfv-glitch/tracemotive/blob/main/docs/openai-agents.md) and
[the example README](https://github.com/doraemonfv-glitch/tracemotive/blob/main/examples/README.md).

## Privacy and security

- TraceMotive is disabled by default and has no analytics or external TraceMotive
  telemetry. Its supported transport is the configured loopback Collector.
- `capture_content=False` is the default even when TraceMotive is enabled. Turn
  it on only when local content capture is intentional.
- Model-provider traffic is separate from TraceMotive telemetry. For example,
  the OpenAI example sends the model request to OpenAI.
- Tracing and content capture are independent controls: enabling tracing does
  not enable content capture.
- A framework adapter converts framework data into TraceMotive's Canonical
  representation. The shared v0.1 privacy boundary then normalizes and
  sanitizes sensitive values before an event enters the transport queue.
- The in-memory transport queue retains serialized Canonical event bytes only;
  it does not retain raw framework objects or unsanitized source values.
- The Collector persists only sanitized Canonical data in its configured local
  SQLite database. SQLite journal/WAL/SHM sidecars may also exist. TraceMotive
  does not provide local database encryption; protect the local data directory
  with the operating system's access controls.
- Redaction is part of this shared pre-transport boundary, not an independent
  redaction policy that each framework adapter is expected to define. The
  policy covers known/specified sensitive keys and recognizable credential
  patterns; it does not guarantee detection of every possible secret.
- Captured runtime content is untrusted data. The frontend renders it as data
  and does not execute embedded HTML, script, or arbitrary code.

## Trace status and troubleshooting

Trace status describes the observed top-level workflow outcome: `unset`, `ok`,
or `error`. An error in a child Span does not automatically change the Trace
status; the UI also reports Span error counts separately.

If the example or SDK smoke reports that the Collector is unavailable, check
the health URL, confirm the Collector terminal is still running on
`127.0.0.1:8765`, and ensure the frontend is using `127.0.0.1:5173`. The SDK
keeps Agent execution non-fatal when local telemetry cannot be delivered, but
the trace may be absent or incomplete. Restarting the default persistent
Collector keeps committed traces; an explicitly selected `:memory:` Collector
is cleared on restart.

## Local validation

Python tests:

```text
python -m unittest discover -s tests -v
```

Frontend tests and build:

```text
cd frontend
npm ci
npm test
npm run build:package
```

Local wheel/sdist build and installed-package checks are documented in
[release readiness](https://github.com/doraemonfv-glitch/tracemotive/blob/main/docs/release-readiness.md). These are maintainer and
developer validation steps; end users should install TraceMotive from PyPI as
described above.

## Contributing

Contributions are welcome. Before making changes, read
[CONTRIBUTING.md](https://github.com/doraemonfv-glitch/tracemotive/blob/main/CONTRIBUTING.md). New contributors can start with issues
labeled [good first issue](https://github.com/doraemonfv-glitch/tracemotive/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
Keep changes focused and within the Frozen v0.1 contract.

## Security

Do not report security vulnerabilities in public Issues or pull requests.
Read [SECURITY.md](https://github.com/doraemonfv-glitch/tracemotive/blob/main/SECURITY.md) and use GitHub Private Vulnerability Reporting
from the repository Security page.
