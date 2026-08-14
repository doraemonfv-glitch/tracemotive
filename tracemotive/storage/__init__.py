"""Internal SQLite persistence for TraceMotive v0.1."""

from .migrations import (
    CURRENT_MIGRATION_VERSION,
    MigrationError,
    NewerDatabaseError,
    run_migrations,
)
from .paths import DatabasePathError, resolve_database_path
from .repository import (
    EntityConflictError,
    Repository,
    SQLiteRepository,
    TraceQueryRecord,
    TraceStats,
    TraceSummaryRecord,
)

__all__ = [
    "CURRENT_MIGRATION_VERSION",
    "DatabasePathError",
    "EntityConflictError",
    "MigrationError",
    "NewerDatabaseError",
    "Repository",
    "SQLiteRepository",
    "TraceQueryRecord",
    "TraceStats",
    "TraceSummaryRecord",
    "run_migrations",
    "resolve_database_path",
]
