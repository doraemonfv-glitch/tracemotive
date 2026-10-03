"""Read-only version reporting and first-run diagnostics for the local CLI.

Nothing in this module imports FastAPI, Starlette, or uvicorn at module load,
creates files or directories, runs migrations, reads stored trace content, or
prints environment variable values.  Network access is one loopback request
to the existing health route of an explicitly validated local endpoint.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform as platform_module
import sqlite3
import sys
import unicodedata
from urllib.parse import urlsplit

from tracemotive.canonical.models import AGENTLENS_SCHEMA_VERSION
from tracemotive.collector import DEFAULT_BIND_HOST, PROTOCOL_VERSION
from tracemotive.storage.migrations import CURRENT_MIGRATION_VERSION
from tracemotive.storage.paths import DatabasePathError, resolve_database_path


SERVER_INSTALL_COMMAND = 'python -m pip install "tracemotive[server]"'
DOCTOR_ENDPOINT_RULE = (
    "expected http://127.0.0.1:PORT or http://localhost:PORT without "
    "credentials, a path, a query, or a fragment (the supplied value is not shown)"
)

_DOCTOR_HOSTS = frozenset({"localhost", DEFAULT_BIND_HOST})
_HEALTH_PATH = "/api/v1/health"
_PROBE_TIMEOUT_SECONDS = 2.0
_MAX_HEALTH_BYTES = 1024
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_HEADER_BYTES = 100
_WAL_FORMAT = 2
_MINIMUM_PYTHON = (3, 10)
# Matches tracemotive.cli.DEFAULT_PORT; the CLI imports this module.
_SERVE_DEFAULT_PORT = 8765

OK = "ok"
INFO = "info"
FAIL = "fail"

PROBE_OK = "ok"
PROBE_UNREACHABLE = "unreachable"
PROBE_UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class LoopbackTarget:
    """A validated local server address for the doctor health probe."""

    host: str
    port: int

    @property
    def display(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class Check:
    label: str
    status: str
    detail: str
    hints: tuple[str, ...] = ()


def installed_package_version() -> str | None:
    """Return the installed distribution version, or ``None`` when absent."""

    try:
        return importlib.metadata.version("tracemotive")
    except importlib.metadata.PackageNotFoundError:
        return None


def version_text() -> str:
    """Return ``--version`` output with each versioned contract labelled."""

    version = installed_package_version()
    first = (
        f"tracemotive {version}"
        if version is not None
        else "tracemotive (package version unknown: installed metadata not found)"
    )
    return "\n".join(
        (
            first,
            f"Canonical schema version: {AGENTLENS_SCHEMA_VERSION}",
            f"Ingest protocol version: {PROTOCOL_VERSION}",
            f"Database migration version: {CURRENT_MIGRATION_VERSION}",
            "The package version is not the Canonical schema, ingest protocol, "
            "or database migration version; each is versioned separately.",
        )
    )


def parse_doctor_endpoint(value: str) -> LoopbackTarget:
    """Accept only ``localhost`` or ``127.0.0.1`` HTTP endpoints.

    The error never contains the supplied value, which may hold credentials.
    """

    if not isinstance(value, str):
        raise ValueError(DOCTOR_ENDPOINT_RULE)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(DOCTOR_ENDPOINT_RULE) from None
    if (
        parsed.scheme.casefold() != "http"
        or hostname is None
        or hostname.casefold() not in _DOCTOR_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError(DOCTOR_ENDPOINT_RULE)
    return LoopbackTarget(hostname.casefold(), 80 if port is None else port)


def probe_health(port: int) -> str:
    """Request the existing health route on 127.0.0.1 and classify the reply.

    ``localhost`` is probed at 127.0.0.1, the only address ``serve`` binds, so
    no name resolution happens.  The response body is never returned.
    """

    connection: HTTPConnection | None = None
    try:
        connection = HTTPConnection(DEFAULT_BIND_HOST, port, timeout=_PROBE_TIMEOUT_SECONDS)
        connection.request(
            "GET",
            _HEALTH_PATH,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        status = response.status
        body = response.read(_MAX_HEALTH_BYTES + 1)
    except (OSError, HTTPException, ValueError):
        return PROBE_UNREACHABLE
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    if status != 200 or len(body) > _MAX_HEALTH_BYTES:
        return PROBE_UNEXPECTED
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        return PROBE_UNEXPECTED
    return PROBE_OK if payload == {"status": "ok"} else PROBE_UNEXPECTED


def run_doctor(
    *,
    db: str | None,
    target: LoopbackTarget,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | os.PathLike[str] | None = None,
    probe: Callable[[int], str] | None = None,
) -> tuple[int, str]:
    """Run every read-only check and return ``(exit_code, report_text)``."""

    environment = os.environ if environ is None else environ
    probe = probe_health if probe is None else probe
    checks = [
        _python_check(),
        _package_check(),
        _dependency_check("FastAPI (required dependency)", "fastapi", "fastapi"),
        _dependency_check("Server extra (uvicorn)", "uvicorn", "uvicorn"),
        _ui_check(),
        *_database_checks(db, environment, platform, home),
        _server_check(target, probe),
    ]
    failures = sum(1 for check in checks if check.status == FAIL)
    return (1 if failures else 0), _format_report(checks, failures)


def safe_text(text: str) -> str:
    """Escape control, format, and line-separator characters for a terminal."""

    return "".join(_escape(character) for character in text)


def _escape(character: str) -> str:
    category = unicodedata.category(character)
    if not (category.startswith("C") or category in ("Zl", "Zp")):
        return character
    code = ord(character)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _format_report(checks: list[Check], failures: int) -> str:
    lines = ["TraceMotive doctor: read-only checks; nothing is created or changed."]
    for check in checks:
        marker = f"[{check.status}]".ljust(7)
        lines.append(safe_text(f"  {marker}{check.label}: {check.detail}"))
        for hint in check.hints:
            lines.append(safe_text(f"         next step: {hint}"))
    if failures:
        noun = "check" if failures == 1 else "checks"
        lines.append(f"Result: {failures} {noun} failed.")
    else:
        lines.append("Result: all checks passed.")
    return "\n".join(lines)


def _python_check() -> Check:
    detail = f"{platform_module.python_version()} ({platform_module.python_implementation()})"
    if sys.version_info[:2] < _MINIMUM_PYTHON:
        return Check("Python", FAIL, detail, ("TraceMotive requires Python 3.10 or newer",))
    return Check("Python", OK, detail)


def _package_check() -> Check:
    version = installed_package_version()
    if version is None:
        return Check(
            "TraceMotive package",
            FAIL,
            "installed package metadata not found",
            (f"install it with: {SERVER_INSTALL_COMMAND}",),
        )
    return Check("TraceMotive package", OK, version)


def _module_available(module: str) -> bool:
    """Locate a top-level module without importing or executing it."""

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _dependency_check(label: str, module: str, distribution: str) -> Check:
    if not _module_available(module):
        return Check(label, FAIL, "not installed", (f"install it with: {SERVER_INSTALL_COMMAND}",))
    try:
        return Check(label, OK, f"available ({importlib.metadata.version(distribution)})")
    except importlib.metadata.PackageNotFoundError:
        return Check(label, OK, "available")


def _ui_check() -> Check:
    label = "Packaged UI"
    try:
        from tracemotive.ui import get_ui_root
        from tracemotive.ui.server import PackagedUIError, validate_ui_root
    except ImportError:
        return Check(
            label,
            FAIL,
            "cannot be checked because FastAPI/Starlette is unavailable",
            (f"install it with: {SERVER_INSTALL_COMMAND}",),
        )
    reinstall = (
        'reinstall TraceMotive: python -m pip install --force-reinstall "tracemotive[server]"'
        "; a source checkout needs: python scripts/bootstrap.py",
    )
    try:
        validate_ui_root(get_ui_root())
    except PackagedUIError as exc:
        return Check(label, FAIL, str(exc), reinstall)
    except Exception:
        return Check(label, FAIL, "packaged production UI could not be inspected", reinstall)
    return Check(label, OK, "available")


def _database_checks(
    explicit: str | None,
    environ: Mapping[str, str],
    platform: str | None,
    home: str | os.PathLike[str] | None,
) -> list[Check]:
    label = "Database path"
    from_environment = False
    if explicit is not None:
        source = "--db option"
    elif environ.get("TRACEMOTIVE_DB") not in (None, ""):
        source = "TRACEMOTIVE_DB environment variable"
        from_environment = True
    else:
        source = "platform default"

    choose = ("choose a usable file path with --db PATH or TRACEMOTIVE_DB",)
    try:
        path = resolve_database_path(explicit, environ=environ, platform=platform, home=home)
    except DatabasePathError:
        return [Check(label, FAIL, f"the {source} value is not a usable path (value not shown)", choose)]

    if from_environment:
        shown = "value of TRACEMOTIVE_DB (not shown)"
    elif explicit is not None:
        shown = path
    else:
        shown = _default_path_display(environ, platform)
    checks = [Check(label, OK, f"{shown} (source: {source})")]

    if path == ":memory:":
        checks.append(
            Check("Database file", INFO, "in-memory database requested; nothing is persisted")
        )
        return checks

    database = Path(path)
    try:
        exists = database.exists()
        is_directory = exists and database.is_dir()
        is_special = exists and not is_directory and not database.is_file()
    except OSError:
        checks.append(Check("Database file", FAIL, "could not be inspected", choose))
        return checks
    if is_directory:
        checks.append(Check("Database file", FAIL, "the path names a directory, not a file", choose))
        return checks
    if is_special:
        checks.append(Check("Database file", FAIL, "the path is not a regular file", choose))
        return checks

    if exists:
        checks.append(Check("Database file", OK, "exists"))
    else:
        checks.append(
            Check(
                "Database file",
                INFO,
                "not created yet; tracemotive serve creates it on first start",
            )
        )
    checks.append(_writable_check(database, exists, choose))
    checks.append(_migration_check(database) if exists else Check(
        "Database migration",
        INFO,
        "not applicable until the database file exists",
    ))
    return checks


def _default_path_display(environ: Mapping[str, str], platform: str | None) -> str:
    """Resolve the default path with placeholders instead of environment values."""

    current = sys.platform if platform is None else platform
    placeholders: dict[str, str] = {}
    if current.startswith("win"):
        value = environ.get("LOCALAPPDATA")
        if value and not value.isspace():
            placeholders["LOCALAPPDATA"] = "%LOCALAPPDATA%"
    elif current != "darwin":
        value = environ.get("XDG_DATA_HOME")
        if value and not value.isspace():
            placeholders["XDG_DATA_HOME"] = "$XDG_DATA_HOME"
    return resolve_database_path(environ=placeholders, platform=current, home="~")


def _writable_check(database: Path, exists: bool, choose: tuple[str, ...]) -> Check:
    label = "Database location"
    directory_access = os.W_OK | os.X_OK
    try:
        if exists:
            writable = os.access(database, os.W_OK) and os.access(
                database.parent, directory_access
            )
        else:
            ancestor = database.parent
            while not ancestor.exists() and ancestor.parent != ancestor:
                ancestor = ancestor.parent
            if not ancestor.is_dir():
                return Check(label, FAIL, "a parent of the database path is not a directory", choose)
            writable = os.access(ancestor, directory_access)
    except OSError:
        return Check(label, FAIL, "could not be inspected", choose)
    if not writable:
        return Check(label, FAIL, "does not appear writable", choose)
    return Check(label, OK, "appears writable (permission check only; nothing was written)")


def _migration_check(database: Path) -> Check:
    label = "Database migration"
    try:
        with database.open("rb") as stream:
            header = stream.read(_SQLITE_HEADER_BYTES)
    except OSError:
        return Check(label, FAIL, "the database file could not be read")

    not_initialized = Check(
        label,
        INFO,
        f"not initialized (version 0); tracemotive serve applies migration "
        f"{CURRENT_MIGRATION_VERSION} on start",
    )
    if not header:
        return not_initialized
    if len(header) < _SQLITE_HEADER_BYTES or not header.startswith(_SQLITE_MAGIC):
        return Check(
            label,
            FAIL,
            "the file is not a SQLite database",
            ("choose a different path with --db PATH or TRACEMOTIVE_DB",),
        )
    if _WAL_FORMAT in (header[18], header[19]):
        return Check(
            label,
            INFO,
            "not read: the database uses WAL mode, and opening it could create files",
        )

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            row = (None,)
    except sqlite3.Error:
        return Check(
            label,
            FAIL,
            "could not be read in read-only mode (the file may be locked or damaged)",
        )
    finally:
        if connection is not None:
            connection.close()

    version = 0 if row is None or row[0] is None else row[0]
    if type(version) is not int or version < 0:
        return Check(label, FAIL, "schema_migrations contains an invalid version")
    if version > CURRENT_MIGRATION_VERSION:
        return Check(
            label,
            FAIL,
            f"version {version} was created by a newer TraceMotive; this version "
            f"supports up to {CURRENT_MIGRATION_VERSION}",
            ("upgrade TraceMotive, or choose a different path with --db PATH",),
        )
    if version == 0:
        return not_initialized
    if version < CURRENT_MIGRATION_VERSION:
        return Check(
            label,
            INFO,
            f"version {version}; tracemotive serve migrates it to {CURRENT_MIGRATION_VERSION}",
        )
    return Check(label, OK, f"version {version} (current)")


def _server_check(target: LoopbackTarget, probe: Callable[[int], str]) -> Check:
    label = "Local server"
    where = target.display
    if target.host != DEFAULT_BIND_HOST:
        where += f" (probed at {DEFAULT_BIND_HOST})"
    result = probe(target.port)
    if result == PROBE_OK:
        return Check(label, OK, f"TraceMotive is responding at {where}")
    if result == PROBE_UNREACHABLE:
        start = "tracemotive serve"
        if target.port != _SERVE_DEFAULT_PORT:
            start += f" --port {target.port}"
        return Check(
            label,
            FAIL,
            f"no TraceMotive server is reachable at {where}",
            (f"start the local server with: {start}",),
        )
    return Check(
        label,
        FAIL,
        f"port {target.port} answered, but not as a TraceMotive server at {where}",
        (
            f"stop the program using port {target.port}, or start TraceMotive on "
            "another port with: tracemotive serve --port PORT",
        ),
    )


__all__ = [
    "DOCTOR_ENDPOINT_RULE",
    "LoopbackTarget",
    "SERVER_INSTALL_COMMAND",
    "installed_package_version",
    "parse_doctor_endpoint",
    "probe_health",
    "run_doctor",
    "safe_text",
    "version_text",
]
