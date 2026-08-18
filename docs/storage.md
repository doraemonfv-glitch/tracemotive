# Storage

This page describes current local SQLite behavior. Security implications of
same-host access, redaction limits, and untrusted captured content are in
[docs/security-model.md](security-model.md).

## Database selection

`tracemotive serve` resolves a file-backed database unless an explicit path
or `:memory:` is requested, in this order:

1. `--db PATH`
2. `TRACEMOTIVE_DB`
3. platform default:
   - Windows: `%LOCALAPPDATA%\TraceMotive\tracemotive.sqlite3`
   - macOS: `~/Library/Application Support/TraceMotive/tracemotive.sqlite3`
   - Linux: `$XDG_DATA_HOME/tracemotive/tracemotive.sqlite3`, otherwise
     `~/.local/share/tracemotive/tracemotive.sqlite3`

The programmatic Collector / `Repository()` default remains `:memory:`.

## What can be stored

The file-backed database may contain Canonical traces and spans, ingest
events, and captured span input/output when capture is enabled. Captured
values can include prompts, model outputs, tool inputs, tool outputs,
application identifiers, and other application-provided JSON.

Sanitization happens before transport-queue ownership. Sanitized data can
still be sensitive. Treat the SQLite file as sensitive.

## Permissions

On POSIX, newly created database directories request `0700` and newly created
database files request `0600`. Those requests are not made equivalently on
Windows. Filesystem permissions are not encryption.

## Deletion and retention

A single trace can be deleted with `DELETE /api/v1/traces/{trace_id}`. That
removes the trace, its spans, captured I/O, and ingest events for that
trace id. The packaged UI does not currently expose a delete control. There
is no bulk-delete UI.

File-backed databases have no automatic retention, expiry, or vacuum policy.
They persist across process restarts until records are explicitly deleted or
the unused database file is intentionally removed outside normal use. Do not
treat removing a live/in-use SQLite file as the recommended deletion path.
This is not a secure-deletion guarantee.

A programmatic Collector using the default `:memory:` database has no
persistent database file. That in-memory data exists only for that
database/process lifetime and does not persist across process termination.

## Encryption

SQLite is not encrypted at rest.

Loopback is not authentication. See [docs/security-model.md](security-model.md)
and [SECURITY.md](../SECURITY.md).
