# TraceMotive security model

This document describes the current local-first security boundary. It is not a
formal security audit, a guarantee, or a promise that TraceMotive is secure.

## Local-first posture

TraceMotive is a local debugging tool. The documented server binds to loopback,
stores traces in local SQLite, and does not include TraceMotive-owned analytics
or external telemetry. Model providers, frameworks, and third-party
dependencies remain separate and may communicate externally.

These architectural positions are current source behavior:

- tracing is disabled by default;
- content capture is independently disabled by default;
- redaction and sanitization occur before transport-queue ownership;
- tracing and sink failures do not fail Agent execution;
- captured content is treated as untrusted data;
- TraceMotive does not intentionally persist provider credentials.

Loopback reduces network exposure. Loopback is not authentication. A same-host
process or user with sufficient access may still reach the local HTTP endpoint
or read the local database.

## Endpoint and binding

The Collector default bind address is `127.0.0.1`. `create_app()` rejects any
other `bind_host`. `tracemotive serve` has no host option and always starts
Uvicorn on `127.0.0.1`. Transport and SDK configuration accept only HTTP
loopback endpoints.

This is an enforced local-network invariant, not an access-control system.
There is no authentication, authorization, encryption in transit beyond local
HTTP, or remote multi-user isolation.

## Local SQLite storage

`tracemotive serve` resolves a file-backed database unless an explicit path or
`:memory:` is requested:

1. `--db PATH`
2. `TRACEMOTIVE_DB`
3. platform default:
   - Windows: `%LOCALAPPDATA%\TraceMotive\tracemotive.sqlite3`
   - macOS: `~/Library/Application Support/TraceMotive/tracemotive.sqlite3`
   - Linux: `$XDG_DATA_HOME/tracemotive/tracemotive.sqlite3`, otherwise
     `~/.local/share/tracemotive/tracemotive.sqlite3`

The programmatic Collector default remains an in-memory database. Newly created
file-backed paths request user-only POSIX permissions. That is not encrypted
at rest and is not a same-host access-control guarantee.

Persisted data may include sanitized Canonical traces and spans, plus captured
values when capture is enabled: prompts, model outputs, tool inputs, tool
outputs, application identifiers, application-provided data, metadata, and
other captured JSON values. Treat the SQLite file as sensitive.

## Deletion and retention

There is no automatic retention, expiry, or vacuum policy.

A single trace can be deleted through `DELETE /api/v1/traces/{trace_id}`,
which removes that trace, its spans, captured I/O, and ingest events. The
packaged UI does not currently expose a delete control. There is no documented
secure-deletion guarantee and no whole-database wipe command.

## Redaction limits

Adapters convert framework objects into Canonical data first. The shared
privacy boundary then redacts a frozen set of recognizable credential keys and
patterns before the transport queue owns the event. Capture remains off unless
the caller enables it.

Redaction is defense in depth. Pattern-based replacement cannot guarantee
removal of every secret or sensitive value. Users remain responsible for
deciding what content to capture. Secrets can still be stored if they appear
in captured prompts, tool payloads, or application data and are not matched.

## Untrusted captured content

Captured model, tool, and application content is untrusted data. It may
contain misleading text, markup-like strings, instruction-like text, arbitrary
application values, or oversized values up to the current content bound.

The frontend renders captured values as React text or JSON text. Current
source does not use `dangerouslySetInnerHTML` or `innerHTML`. That is an
untrusted-data rendering choice, not a claim of XSS immunity.

Do not execute captured HTML, script, or other Agent content.

## Credentials and telemetry

TraceMotive does not intentionally persist provider credentials. It can still
store credential-like values if they are present in captured content and not
redacted.

TraceMotive itself does not send analytics or TraceMotive-owned external
telemetry. Provider traffic, such as an OpenAI model request, is separate and
may leave the machine.

## In scope / not currently solved

In scope for this local-first design:

- accidental sensitive-data capture
- incomplete redaction
- local endpoint access by another same-host process
- local SQLite exposure
- vulnerable third-party dependencies
- unsafe trust in captured content

Not currently solved:

- a compromised operating system
- a hostile user with filesystem access
- endpoint authentication or authorization
- encrypted-at-rest storage
- remote multi-user isolation
- automatic retention or secure deletion

## Dependency automation

V05-05 adds weekly Dependabot updates and automated `pip-audit` /
`npm audit` checks. The strict Python dependency audit scans the isolated
third-party shipped runtime dependency surface for the server and OpenAI
Agents extras. First-party TraceMotive is excluded from that PyPI lookup
surface; pip-audit does not statically audit TraceMotive source. Those checks
make known third-party dependency advisories visible. They are not a formal
security audit.

No formal security audit has been completed.

CodeQL, GitHub dependency-review, and SBOM generation remain deferred P1 work.
