"""Database-path resolution and safe preparation for local SQLite storage."""

from __future__ import annotations

from collections.abc import Mapping
import ntpath
import os
from pathlib import Path
import posixpath
import sys


class DatabasePathError(RuntimeError):
    """Raised when local SQLite path configuration cannot be used safely."""


def resolve_database_path(
    explicit_path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | os.PathLike[str] | None = None,
    local_app_data: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve the v0.2 Collector database path without opening SQLite.

    ``Repository()`` deliberately does not call this function: its v0.1
    programmatic default remains ``:memory:``.  A future server command owns
    resolving its persistent default and passes the result to the Collector.
    Optional inputs make platform-specific resolution testable on every host.
    """

    if explicit_path is not None:
        return _validate_configured_path(explicit_path)

    environment = os.environ if environ is None else environ
    configured_path = environment.get("TRACEMOTIVE_DB")
    if configured_path is not None and configured_path != "":
        return _validate_configured_path(configured_path)

    current_platform = sys.platform if platform is None else platform
    home_path = _coerce_path_text(Path.home() if home is None else home)

    if current_platform.startswith("win"):
        base = environment.get("LOCALAPPDATA")
        if not base or base.isspace():
            base = _coerce_path_text(
                local_app_data
                if local_app_data is not None
                else ntpath.join(home_path, "AppData", "Local")
            )
        return ntpath.join(base, "TraceMotive", "tracemotive.sqlite3")

    if current_platform == "darwin":
        return posixpath.join(
            home_path,
            "Library",
            "Application Support",
            "TraceMotive",
            "tracemotive.sqlite3",
        )

    data_home = environment.get("XDG_DATA_HOME")
    if data_home and not data_home.isspace():
        return posixpath.join(data_home, "tracemotive", "tracemotive.sqlite3")
    return posixpath.join(home_path, ".local", "share", "tracemotive", "tracemotive.sqlite3")


def prepare_database_path(path: str | os.PathLike[str]) -> str:
    """Validate and prepare a Repository path before SQLite opens it.

    File-backed paths create missing parents.  On POSIX, newly created
    directories and database files request user-only permissions.  All path
    and filesystem failures become safe configuration errors; this function
    never falls back to an in-memory database.
    """

    configured_path = _validate_configured_path(path)
    if configured_path == ":memory:":
        return configured_path

    database_path = Path(configured_path)
    try:
        if database_path.exists() and database_path.is_dir():
            raise DatabasePathError("database path must name a file")
        _create_missing_private_directories(database_path.parent)
        _create_database_file_if_missing(database_path)
    except DatabasePathError:
        raise
    except OSError as exc:
        raise DatabasePathError("could not prepare database path") from exc

    return str(database_path)


def _validate_configured_path(path: str | os.PathLike[str]) -> str:
    try:
        value = _coerce_path_text(path)
    except TypeError as exc:
        raise DatabasePathError("database path must be text") from exc
    if value == "" or value.isspace():
        raise DatabasePathError("database path must not be empty")
    if "\x00" in value:
        raise DatabasePathError("database path is invalid")
    return value


def _coerce_path_text(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise TypeError("database path must be text")
    return value


def _create_missing_private_directories(directory: Path) -> None:
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise DatabasePathError("could not create database directory") from exc
        if os.name == "posix":
            try:
                os.chmod(path, 0o700)
            except OSError as exc:
                raise DatabasePathError("could not secure database directory") from exc


def _create_database_file_if_missing(path: Path) -> None:
    if path.exists():
        return

    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        raise DatabasePathError("could not create database file") from exc
    else:
        os.close(descriptor)

    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise DatabasePathError("could not secure database file") from exc


__all__ = ["DatabasePathError", "prepare_database_path", "resolve_database_path"]
