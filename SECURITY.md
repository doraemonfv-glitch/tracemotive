# Security Policy

## Reporting a vulnerability

Please do not report suspected security vulnerabilities in a public Issue,
Discussion, or pull request. Use GitHub Private Vulnerability Reporting:
open the repository Security page and submit a private report.

GitHub Private Vulnerability Reporting is a repository setting. This file
cannot enable that setting by itself. If the Security page does not offer
private reporting, treat that as a maintainer follow-up and do not fall back
to a public Issue.

Please include, when available:

- The affected TraceMotive version or commit
- A concise description of the vulnerability
- The potential impact
- Reproduction steps or a minimal proof of concept
- Relevant environment information
- A possible mitigation or fix

Do not include real API keys, passwords, tokens, personal data, or captured
Agent content in a report. Use redacted or synthetic examples whenever
possible.

There is no promised response SLA, patch window, or guaranteed fix period.

## Security-sensitive areas

Security and privacy reports may involve, for example:

- Secret and credential redaction
- Content capture
- The local transport and loopback-only Collector
- SQLite persistence
- The Query API
- Rendering of untrusted captured content
- Framework adapters
- Failure isolation
- Dependency advisories in shipped runtime paths

## Supported versions

The latest public TraceMotive release is supported for security reports.

## Disclosure

Please avoid public disclosure while maintainers review a private report. Do
not move details to a public Issue or pull request before a fix or mitigation
has been coordinated with the maintainers.

## Current security posture

These statements describe current source behavior, not a formal security
audit:

- The Collector is loopback-only. `create_app()` rejects a non-`127.0.0.1`
  bind host, and `tracemotive serve` has no host option.
- Loopback reduces network exposure. Loopback is not authentication.
- TraceMotive is disabled by default.
- Content capture is independently disabled by default.
- Redaction occurs before the transport queue.
- Tracing failures must not fail Agent execution.
- Captured content is treated as untrusted.
- TraceMotive does not intentionally persist provider credentials.
- TraceMotive itself includes no analytics or external telemetry.

The local-first threat model, storage locations, deletion/retention limits,
and redaction limits are documented in [docs/security-model.md](docs/security-model.md).

No formal security audit has been completed. Automated `pip-audit` and
`npm audit` checks detect known dependency advisories; they are not a formal
audit.

## Reproducing the automated checks

From a checkout, using the same commands as `.github/workflows/security.yml`:

```text
rm -rf .audit-runtime
python -m pip install --upgrade pip
python -m pip install --target .audit-runtime ".[server,openai-agents]"
python scripts/prepare_audit_runtime.py .audit-runtime
python -m pip install "pip-audit==2.10.1"
python -m pip_audit --progress-spinner off --strict --path .audit-runtime
```

```text
cd frontend
npm ci
npm audit --audit-level=high
```

The strict Python dependency audit scans the isolated third-party shipped
runtime dependency surface for the server and OpenAI Agents extras. First-party
TraceMotive is excluded from that PyPI lookup surface so an unpublished
package version can still be audited. `--strict` fails the check if dependency
collection is incomplete. The frontend audit inspects the full lockfile,
including development dependencies, and fails on high or critical advisories.
There is no ignore list.
