"""Forward-only SQLite migrations for TraceMotive v0.1."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Callable


CURRENT_MIGRATION_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class MigrationError(RuntimeError):
    """Raised when the TraceMotive database cannot be migrated safely."""


class NewerDatabaseError(MigrationError):
    """Raised when a database was created by a newer TraceMotive version."""


Migration = Callable[[sqlite3.Connection], None]


def _utc_now_us() -> int:
    delta = datetime.now(timezone.utc) - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at_us INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            name TEXT NOT NULL,
            started_at_us INTEGER NOT NULL,
            ended_at_us INTEGER NULL,
            lifecycle_stage INTEGER NOT NULL CHECK (lifecycle_stage IN (1, 2)),
            status TEXT NOT NULL CHECK (status IN ('unset', 'ok', 'error')),
            source_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            attributes_json TEXT NOT NULL,
            created_at_us INTEGER NOT NULL,
            updated_at_us INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS spans (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            parent_span_id TEXT NULL,
            type TEXT NOT NULL,
            operation TEXT NOT NULL,
            name TEXT NOT NULL,
            started_at_us INTEGER NOT NULL,
            ended_at_us INTEGER NULL,
            lifecycle_stage INTEGER NOT NULL CHECK (lifecycle_stage IN (1, 2)),
            status TEXT NOT NULL CHECK (status IN ('unset', 'ok', 'error')),
            error_json TEXT NULL,
            source_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            attributes_json TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at_us INTEGER NOT NULL,
            updated_at_us INTEGER NOT NULL,
            PRIMARY KEY (trace_id, span_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS span_io (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            input_json TEXT NULL,
            output_json TEXT NULL,
            input_capture_json TEXT NOT NULL,
            output_capture_json TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_events (
            event_id TEXT PRIMARY KEY,
            event_content_sha256 TEXT NOT NULL,
            event_type TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            span_id TEXT NULL,
            received_at_us INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_spans_trace_started_at "
        "ON spans (trace_id, started_at_us)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_spans_trace_parent "
        "ON spans (trace_id, parent_span_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_spans_trace_type "
        "ON spans (trace_id, type)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_spans_trace_status "
        "ON spans (trace_id, status)"
    )


MIGRATIONS: dict[int, Migration] = {1: _migration_1}


def _current_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise MigrationError("could not inspect schema_migrations") from exc
    version = row[0]
    if type(version) is not int or version < 0:
        raise MigrationError("schema_migrations contains an invalid version")
    return version


def run_migrations(
    connection: sqlite3.Connection,
    *,
    now_us: int | None = None,
) -> int:
    """Apply all known migrations and return the resulting version.

    Each migration has its own transaction.  No migration is attempted after
    a newer version is observed, so the newer database remains untouched.
    """

    version = _current_version(connection)
    if version > CURRENT_MIGRATION_VERSION:
        raise NewerDatabaseError(
            "a newer TraceMotive version created the database; "
            "this TraceMotive version cannot open it"
        )

    applied_at_us = _utc_now_us() if now_us is None else now_us
    for target_version in range(version + 1, CURRENT_MIGRATION_VERSION + 1):
        migration = MIGRATIONS.get(target_version)
        if migration is None:
            raise MigrationError(f"missing migration {target_version}")
        try:
            connection.execute("BEGIN")
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at_us) VALUES (?, ?)",
                (target_version, applied_at_us),
            )
            connection.commit()
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            raise MigrationError(
                f"migration {target_version} failed; normal startup is refused"
            ) from exc
    return CURRENT_MIGRATION_VERSION


__all__ = [
    "CURRENT_MIGRATION_VERSION",
    "MIGRATIONS",
    "MigrationError",
    "NewerDatabaseError",
    "run_migrations",
]
