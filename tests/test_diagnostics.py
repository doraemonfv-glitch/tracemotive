"""M2-A: ``tracemotive --version``, ``tracemotive doctor``, and CLI guidance."""

from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from tracemotive import diagnostics
from tracemotive.canonical.models import AGENTLENS_SCHEMA_VERSION
from tracemotive.cli import DEFAULT_PORT, _parser, main
from tracemotive.collector import PROTOCOL_VERSION, Collector
from tracemotive.diagnostics import (
    LoopbackTarget,
    parse_doctor_endpoint,
    run_doctor,
    safe_text,
    version_text,
)
from tracemotive.local_client import ApiContractError
from tracemotive.storage import CURRENT_MIGRATION_VERSION, Repository
from tests.process_support import RetryingTemporaryDirectory
from tests.test_collector import batch, event, make_trace


ROOT = Path(__file__).resolve().parents[1]
INSTALL_COMMAND = 'python -m pip install "tracemotive[server]"'
LEFT_ID = "a" * 32
RIGHT_ID = "b" * 32
TARGET = LoopbackTarget("127.0.0.1", 8765)
CLI = "from tracemotive.cli import main; raise SystemExit(main())"
BLOCKED_SERVER_STACK = (
    "import sys\n"
    "for name in ('fastapi', 'starlette', 'uvicorn'):\n"
    "    sys.modules[name] = None\n"
    "from tracemotive.cli import main\n"
    "raise SystemExit(main())\n"
)


def _capture(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    script: str = CLI,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy() if env is None else env
    environment["PYTHONPATH"] = str(ROOT)
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


def _write_ui(root: Path) -> Path:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    (root / "assets" / "app.js").write_text("void 0;", encoding="utf-8")
    return root


@contextlib.contextmanager
def _installed(ui_root: Path, *, dependencies: bool = True):
    with patch(
        "tracemotive.diagnostics._module_available",
        return_value=dependencies,
    ), patch("tracemotive.ui.get_ui_root", return_value=ui_root):
        yield


@contextlib.contextmanager
def _health_server(status: int, body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _seed_database(path: Path, name: str) -> None:
    collector = Collector(Repository(str(path)))
    try:
        result = collector.ingest(batch(event("trace.ended", make_trace(ended=True, name=name))))
        assert result == {"accepted": 1, "duplicates": 0, "stale": 0}, result
    finally:
        collector.close()


class VersionTests(unittest.TestCase):
    def test_first_line_is_package_version_and_contracts_are_labelled(self) -> None:
        code, stdout, stderr = _capture(["--version"])
        self.assertEqual(code, 0, stderr)
        lines = stdout.splitlines()
        self.assertEqual(lines[0], f"tracemotive {importlib.metadata.version('tracemotive')}")
        self.assertIn(f"Canonical schema version: {AGENTLENS_SCHEMA_VERSION}", lines)
        self.assertIn(f"Ingest protocol version: {PROTOCOL_VERSION}", lines)
        self.assertIn(f"Database migration version: {CURRENT_MIGRATION_VERSION}", lines)
        for word in ("schema", "protocol", "migration"):
            self.assertNotIn(word, lines[0].lower())
        self.assertIn("versioned separately", stdout)

    def test_reported_contract_versions_are_the_unchanged_frozen_values(self) -> None:
        self.assertEqual(AGENTLENS_SCHEMA_VERSION, "0.1")
        self.assertEqual(PROTOCOL_VERSION, 1)
        self.assertEqual(CURRENT_MIGRATION_VERSION, 1)

    def test_package_1_0_does_not_imply_contract_1_0(self) -> None:
        with patch("tracemotive.diagnostics.installed_package_version", return_value="1.0.0"):
            lines = version_text().splitlines()
        self.assertEqual(lines[0], "tracemotive 1.0.0")
        self.assertIn("Canonical schema version: 0.1", lines)
        self.assertIn("Ingest protocol version: 1", lines)
        self.assertIn("Database migration version: 1", lines)
        self.assertFalse(any("1.0" in line for line in lines[1:]), lines)

    def test_missing_metadata_is_reported_on_the_first_line(self) -> None:
        with patch("tracemotive.diagnostics.installed_package_version", return_value=None):
            first = version_text().splitlines()[0]
        self.assertTrue(first.startswith("tracemotive (package version unknown"), first)

    def test_version_runs_without_server_dependencies(self) -> None:
        result = _run(["--version"], script=BLOCKED_SERVER_STACK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("tracemotive "), result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class DoctorEndpointTests(unittest.TestCase):
    def test_only_localhost_and_127_0_0_1_are_accepted(self) -> None:
        for value, expected in (
            ("http://127.0.0.1:8765", LoopbackTarget("127.0.0.1", 8765)),
            ("http://localhost:9000/", LoopbackTarget("localhost", 9000)),
            ("http://LOCALHOST:1", LoopbackTarget("localhost", 1)),
            ("http://127.0.0.1", LoopbackTarget("127.0.0.1", 80)),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_doctor_endpoint(value), expected)

    def test_non_loopback_and_rich_endpoints_are_rejected_with_usage_exit(self) -> None:
        rejected = (
            "http://example.com:8765",
            "http://0.0.0.0:8765",
            "http://[::]:8765",
            "http://[::1]:8765",
            "http://10.0.0.1:8765",
            "http://127.0.0.2:8765",
            "https://127.0.0.1:8765",
            "ftp://localhost:8765",
            "localhost:8765",
            "http://127.0.0.1:8765@evil.example",
            "http://127.0.0.1:8765/api",
            "http://127.0.0.1:8765?debug=1",
            "http://127.0.0.1:8765#frag",
            "http://127.0.0.1:0",
            "http://127.0.0.1:99999",
            "",
        )
        connection = Mock(side_effect=AssertionError("doctor contacted a host"))
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_doctor_endpoint(value)
                with patch("tracemotive.diagnostics.HTTPConnection", connection):
                    code, stdout, stderr = _capture(["doctor", "--endpoint", value])
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("the supplied value is not shown", stderr)
        connection.assert_not_called()

    def test_rejected_endpoint_credentials_are_never_echoed(self) -> None:
        for value in (
            "http://alice:SENTINEL-PASSWORD-4d1c@example.com:8765/",
            "http://alice:SENTINEL-PASSWORD-4d1c@127.0.0.1:8765",
            "http://SENTINEL-PASSWORD-4d1c@localhost:8765",
        ):
            with self.subTest(value=value):
                result = _run(["doctor", "--endpoint", value])
                self.assertEqual(result.returncode, 2)
                combined = result.stdout + result.stderr
                self.assertNotIn("SENTINEL-PASSWORD-4d1c", combined)
                self.assertNotIn("alice", combined)

    def test_rejected_endpoint_control_characters_are_not_echoed(self) -> None:
        code, stdout, stderr = _capture(
            ["doctor", "--endpoint", "http://127.0.0.1:8765/\x1b[2J\x07"]
        )
        self.assertEqual(code, 2)
        self.assertNotIn("\x1b", stdout + stderr)
        self.assertNotIn("\x07", stdout + stderr)

    def test_localhost_probe_connects_to_127_0_0_1_only(self) -> None:
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ok"}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch(
            "tracemotive.diagnostics.HTTPConnection",
            return_value=connection,
        ) as factory:
            self.assertEqual(diagnostics.probe_health(4321), diagnostics.PROBE_OK)
        self.assertEqual(factory.call_args.args[:2], ("127.0.0.1", 4321))
        self.assertEqual(connection.request.call_args.args[:2], ("GET", "/api/v1/health"))


class DoctorReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = RetryingTemporaryDirectory(prefix="tracemotive doctor ")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.ui = _write_ui(self.root / "ui")
        self.work = self.root / "work"
        self.work.mkdir()

    def _doctor(self, *args: str, probe: str = diagnostics.PROBE_OK) -> tuple[int, str, str]:
        with _installed(self.ui), patch(
            "tracemotive.diagnostics.probe_health",
            return_value=probe,
        ):
            return _capture(["doctor", *args])

    def test_missing_database_and_directories_are_not_created(self) -> None:
        database = self.work / "a" / "b" / "tracemotive.sqlite3"
        before = _tree(self.work)
        code, stdout, stderr = self._doctor("--db", str(database))
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(_tree(self.work), before)
        self.assertFalse((self.work / "a").exists())
        self.assertIn("not created yet", stdout)
        self.assertIn("Result: all checks passed.", stdout)

    def test_default_paths_are_not_created(self) -> None:
        home = self.work / "home"
        local = self.work / "local"
        before = _tree(self.work)
        for platform, environ in (
            ("linux", {}),
            ("linux", {"XDG_DATA_HOME": str(self.work / "xdg")}),
            ("darwin", {}),
            ("win32", {"LOCALAPPDATA": str(local)}),
            ("win32", {}),
        ):
            with self.subTest(platform=platform, environ=environ):
                with _installed(self.ui):
                    run_doctor(
                        db=None,
                        target=TARGET,
                        environ=environ,
                        platform=platform,
                        home=home,
                        probe=lambda port: diagnostics.PROBE_OK,
                    )
                self.assertEqual(_tree(self.work), before)

    def test_doctor_never_prepares_paths_or_opens_writable_sqlite(self) -> None:
        database = self.work / "state.sqlite3"
        _seed_database(database, "seeded")
        real_connect = sqlite3.connect
        calls: list[tuple] = []

        def read_only_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)

        failure = AssertionError("doctor prepared a database path")
        with patch("tracemotive.storage.paths.prepare_database_path", side_effect=failure), patch(
            "tracemotive.storage.repository.prepare_database_path", side_effect=failure
        ), patch("tracemotive.diagnostics.sqlite3.connect", side_effect=read_only_connect):
            code, stdout, stderr = self._doctor("--db", str(database))
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertTrue(args[0].startswith("file:"))
        self.assertTrue(args[0].endswith("?mode=ro"))
        self.assertIs(kwargs.get("uri"), True)

    def test_existing_database_is_byte_identical_and_contents_are_not_printed(self) -> None:
        sentinel = "SENTINEL-TRACE-CONTENT-0b7e"
        database = self.work / "state.sqlite3"
        _seed_database(database, sentinel)
        before = _tree(self.work)
        before_mtime = database.stat().st_mtime_ns

        code, stdout, stderr = self._doctor("--db", str(database))
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn(f"version {CURRENT_MIGRATION_VERSION} (current)", stdout)
        self.assertNotIn(sentinel, stdout + stderr)

        result = _run(["doctor", "--db", str(database), "--endpoint", f"http://127.0.0.1:{_closed_port()}"])
        self.assertNotIn(sentinel, result.stdout + result.stderr)
        self.assertIn(f"version {CURRENT_MIGRATION_VERSION} (current)", result.stdout)

        self.assertEqual(_tree(self.work), before)
        self.assertEqual(database.stat().st_mtime_ns, before_mtime)

    def test_uninitialized_database_reports_version_zero_without_migrating(self) -> None:
        database = self.work / "empty.sqlite3"
        database.write_bytes(b"")
        code, stdout, stderr = self._doctor("--db", str(database))
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("not initialized (version 0)", stdout)
        self.assertEqual(database.read_bytes(), b"")
        self.assertEqual(sorted(path.name for path in self.work.iterdir()), ["empty.sqlite3"])

    def test_sqlite_without_migration_table_reports_version_zero(self) -> None:
        database = self.work / "plain.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
        connection.close()
        before = _tree(self.work)
        code, stdout, _ = self._doctor("--db", str(database))
        self.assertEqual(code, 0, stdout)
        self.assertIn("not initialized (version 0)", stdout)
        self.assertEqual(_tree(self.work), before)

    def test_newer_database_fails_without_modification(self) -> None:
        database = self.work / "newer.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_us INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES (99, 0)")
        connection.commit()
        connection.close()
        before = _tree(self.work)
        code, stdout, _ = self._doctor("--db", str(database))
        self.assertEqual(code, 1)
        self.assertIn("[fail] Database migration: version 99 was created by a newer TraceMotive", stdout)
        self.assertEqual(_tree(self.work), before)

    def test_non_sqlite_file_fails(self) -> None:
        database = self.work / "notes.sqlite3"
        database.write_bytes(b"not a database\n" * 20)
        code, stdout, _ = self._doctor("--db", str(database))
        self.assertEqual(code, 1)
        self.assertIn("not a SQLite database", stdout)
        self.assertEqual(database.read_bytes(), b"not a database\n" * 20)

    def test_wal_database_is_not_opened(self) -> None:
        database = self.work / "wal.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (v INTEGER)")
        connection.commit()
        connection.close()
        before = _tree(self.work)
        with patch("tracemotive.diagnostics.sqlite3.connect") as connect:
            code, stdout, _ = self._doctor("--db", str(database))
        connect.assert_not_called()
        self.assertEqual(code, 0, stdout)
        self.assertIn("uses WAL mode", stdout)
        self.assertEqual(_tree(self.work), before)

    def test_directory_and_file_parent_paths_fail(self) -> None:
        code, stdout, _ = self._doctor("--db", str(self.work))
        self.assertEqual(code, 1)
        self.assertIn("names a directory", stdout)

        blocker = self.work / "blocker.txt"
        blocker.write_text("x", encoding="utf-8")
        code, stdout, _ = self._doctor("--db", str(blocker / "state.sqlite3"))
        self.assertEqual(code, 1)
        self.assertIn("parent of the database path is not a directory", stdout)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support")
    def test_special_file_fails_without_being_read(self) -> None:
        fifo = self.work / "state.sqlite3"
        os.mkfifo(fifo)
        code, stdout, _ = self._doctor("--db", str(fifo))
        self.assertEqual(code, 1)
        self.assertIn("[fail] Database file: the path is not a regular file", stdout)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "POSIX permission bits as a non-root user",
    )
    def test_unwritable_location_fails(self) -> None:
        locked = self.work / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        code, stdout, _ = self._doctor("--db", str(locked / "state.sqlite3"))
        self.assertEqual(code, 1)
        self.assertIn("[fail] Database location: does not appear writable", stdout)

    def test_memory_database_is_reported_without_files(self) -> None:
        code, stdout, _ = self._doctor("--db", ":memory:")
        self.assertEqual(code, 0, stdout)
        self.assertIn("in-memory database requested", stdout)
        self.assertEqual(_tree(self.work), {})


class DoctorPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = RetryingTemporaryDirectory(prefix="tracemotive doctor ")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.ui = _write_ui(self.root / "ui")

    def _report(self, **kwargs) -> tuple[int, str]:
        kwargs.setdefault("target", TARGET)
        kwargs.setdefault("probe", lambda port: diagnostics.PROBE_OK)
        with _installed(self.ui):
            return run_doctor(**kwargs)

    def test_fake_environment_secrets_never_appear(self) -> None:
        secrets = {
            "TRACEMOTIVE_DB": str(self.root / "SENTINEL-DB-PATH-91c2" / "x.sqlite3"),
            "OPENAI_API_KEY": "sk-SENTINEL-OPENAI-5b1e",
            "ANTHROPIC_API_KEY": "sk-ant-SENTINEL-ANTHROPIC-77aa",
            "LOCALAPPDATA": str(self.root / "SENTINEL-LOCALAPPDATA-3f0d"),
            "XDG_DATA_HOME": str(self.root / "SENTINEL-XDG-6c4e"),
        }
        environment = os.environ.copy()
        environment.update(secrets)
        result = _run(
            ["doctor", "--endpoint", f"http://127.0.0.1:{_closed_port()}"],
            env=environment,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        for value in secrets.values():
            self.assertNotIn(value, combined)
        self.assertNotIn("SENTINEL", combined)
        self.assertIn("value of TRACEMOTIVE_DB (not shown)", result.stdout)
        self.assertIn("(source: TRACEMOTIVE_DB environment variable)", result.stdout)
        self.assertFalse((self.root / "SENTINEL-DB-PATH-91c2").exists())

    def test_database_source_is_reported_without_environment_values(self) -> None:
        home = self.root / "SENTINEL-HOME-2a9b"
        explicit = str(self.root / "explicit.sqlite3")
        cases = (
            ({"db": explicit, "environ": {"TRACEMOTIVE_DB": "/SENTINEL-ENV"}}, "--db option", explicit),
            ({"db": None, "environ": {"TRACEMOTIVE_DB": "/SENTINEL-ENV/x.sqlite3"}},
             "TRACEMOTIVE_DB environment variable", "value of TRACEMOTIVE_DB (not shown)"),
            ({"db": None, "environ": {"LOCALAPPDATA": "C:\\SENTINEL-LAD"}, "platform": "win32"},
             "platform default", "%LOCALAPPDATA%\\TraceMotive\\tracemotive.sqlite3"),
            ({"db": None, "environ": {}, "platform": "win32"},
             "platform default", "~\\AppData\\Local\\TraceMotive\\tracemotive.sqlite3"),
            ({"db": None, "environ": {"XDG_DATA_HOME": "/SENTINEL-XDG"}, "platform": "linux"},
             "platform default", "$XDG_DATA_HOME/tracemotive/tracemotive.sqlite3"),
            ({"db": None, "environ": {}, "platform": "linux"},
             "platform default", "~/.local/share/tracemotive/tracemotive.sqlite3"),
            ({"db": None, "environ": {}, "platform": "darwin"},
             "platform default", "~/Library/Application Support/TraceMotive/tracemotive.sqlite3"),
        )
        for kwargs, source, shown in cases:
            with self.subTest(source=source, shown=shown):
                _, report = self._report(home=home, **kwargs)
                self.assertIn(f"Database path: {shown} (source: {source})", report)
                self.assertNotIn("SENTINEL", report)

    def test_invalid_environment_path_is_reported_without_value(self) -> None:
        code, report = self._report(db=None, environ={"TRACEMOTIVE_DB": " \t "})
        self.assertEqual(code, 1)
        self.assertIn(
            "TRACEMOTIVE_DB environment variable value is not a usable path (value not shown)",
            report,
        )

    def test_control_characters_cannot_manipulate_terminal_output(self) -> None:
        hostile = str(self.root / "x\x1b[31mred\x07\r\n[ok] fake\u202edcba\u2028\x9b.sqlite3")
        code, report = self._report(db=hostile, environ={})
        for character in ("\x1b", "\x07", "\r", "\u202e", "\u2028", "\x9b"):
            self.assertNotIn(character, report)
        self.assertIn("\\x1b[31mred\\x07\\x0d\\x0a[ok] fake\\u202edcba\\u2028\\x9b", report)
        self.assertEqual(len([line for line in report.splitlines() if line.startswith("  [")]), 10)
        del code

    def test_safe_text_escapes_controls_and_keeps_printable_text(self) -> None:
        self.assertEqual(safe_text("plain ü 日本"), "plain ü 日本")
        self.assertEqual(safe_text("a\nb\tc\x00\x7f"), "a\\x0ab\\x09c\\x00\\x7f")
        self.assertEqual(safe_text("\u200b\u2029\U000e0001"), "\\u200b\\u2029\\U000e0001")

    def test_server_responses_are_never_printed(self) -> None:
        for status, body in (
            (200, b'{"status":"ok","leak":"SENTINEL-RESPONSE-8e21"}'),
            (500, b'{"error":"SENTINEL-RESPONSE-8e21"}'),
            (200, b"SENTINEL-RESPONSE-8e21 \x1b[2J"),
        ):
            with self.subTest(status=status, body=body):
                with _health_server(status, body) as port:
                    code, report = self._report(
                        db=":memory:",
                        target=LoopbackTarget("127.0.0.1", port),
                        probe=None,
                    )
                self.assertEqual(code, 1)
                self.assertNotIn("SENTINEL", report)
                self.assertIn(f"port {port} answered, but not as a TraceMotive server", report)
                self.assertIn("tracemotive serve --port PORT", report)


class DoctorExitCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = RetryingTemporaryDirectory(prefix="tracemotive doctor ")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.ui = _write_ui(self.root / "ui")
        self.db = str(self.root / "state.sqlite3")

    def test_all_checks_pass_with_a_real_health_server(self) -> None:
        with _health_server(200, b'{"status":"ok"}') as port, _installed(self.ui):
            code, stdout, stderr = _capture(
                ["doctor", "--db", self.db, "--endpoint", f"http://localhost:{port}"]
            )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(stderr, "")
        self.assertIn(
            f"[ok]   Local server: TraceMotive is responding at http://localhost:{port} "
            "(probed at 127.0.0.1)",
            stdout,
        )
        self.assertTrue(stdout.rstrip().endswith("Result: all checks passed."))

    def test_unreachable_server_fails_with_serve_guidance(self) -> None:
        port = _closed_port()
        with _installed(self.ui):
            code, stdout, _ = _capture(
                ["doctor", "--db", self.db, "--endpoint", f"http://127.0.0.1:{port}"]
            )
        self.assertEqual(code, 1)
        self.assertIn(f"[fail] Local server: no TraceMotive server is reachable at http://127.0.0.1:{port}", stdout)
        self.assertIn(f"next step: start the local server with: tracemotive serve --port {port}", stdout)
        self.assertIn("Result: 1 check failed.", stdout)

        with _installed(self.ui), patch(
            "tracemotive.diagnostics.probe_health",
            return_value=diagnostics.PROBE_UNREACHABLE,
        ):
            _, stdout, _ = _capture(["doctor", "--db", self.db])
        self.assertIn("next step: start the local server with: tracemotive serve\n", stdout)

    def test_usage_errors_exit_two(self) -> None:
        for argv in (["doctor", "--bogus"], ["doctor", "--endpoint"], ["doctor", "extra"]):
            with self.subTest(argv=argv):
                code, stdout, _ = _capture(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")

    def test_missing_optional_server_dependencies_fail_with_install_command(self) -> None:
        with _installed(self.ui, dependencies=False), patch(
            "tracemotive.diagnostics.probe_health",
            return_value=diagnostics.PROBE_OK,
        ):
            code, stdout, _ = _capture(["doctor", "--db", self.db])
        self.assertEqual(code, 1)
        self.assertIn("[fail] Server extra (uvicorn): not installed", stdout)
        self.assertIn(f"next step: install it with: {INSTALL_COMMAND}", stdout)

    def test_doctor_runs_when_server_dependencies_are_absent(self) -> None:
        result = _run(
            ["doctor", "--db", ":memory:", "--endpoint", f"http://127.0.0.1:{_closed_port()}"],
            script=BLOCKED_SERVER_STACK,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("[fail] Server extra (uvicorn): not installed", result.stdout)
        self.assertIn("[fail] Packaged UI: cannot be checked because FastAPI/Starlette is unavailable", result.stdout)
        self.assertIn(INSTALL_COMMAND, result.stdout)

    def test_packaged_ui_present_and_missing(self) -> None:
        probe = patch("tracemotive.diagnostics.probe_health", return_value=diagnostics.PROBE_OK)
        with _installed(self.ui), probe:
            code, stdout, _ = _capture(["doctor", "--db", self.db])
        self.assertEqual(code, 0, stdout)
        self.assertIn("[ok]   Packaged UI: available", stdout)

        empty = self.root / "empty-ui"
        empty.mkdir()
        with _installed(empty), probe:
            code, stdout, _ = _capture(["doctor", "--db", self.db])
        self.assertEqual(code, 1)
        self.assertIn("[fail] Packaged UI: packaged production UI index.html is missing", stdout)
        self.assertIn("python scripts/bootstrap.py", stdout)

        no_assets = self.root / "no-assets-ui"
        (no_assets / "assets").mkdir(parents=True)
        (no_assets / "index.html").write_text("<html></html>", encoding="utf-8")
        with _installed(no_assets), probe:
            code, stdout, _ = _capture(["doctor", "--db", self.db])
        self.assertEqual(code, 1)
        self.assertIn("[fail] Packaged UI: packaged production UI assets are missing", stdout)

    def test_unexpected_internal_failure_is_contained(self) -> None:
        with patch("tracemotive.cli.run_doctor", side_effect=RuntimeError("SENTINEL-INTERNAL")):
            code, stdout, stderr = _capture(["doctor"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "tracemotive doctor: unexpected internal failure\n")

    def test_serve_default_port_matches_doctor_guidance(self) -> None:
        self.assertEqual(diagnostics._SERVE_DEFAULT_PORT, DEFAULT_PORT)


class CliGuidanceTests(unittest.TestCase):
    def test_server_unreachable_guidance_for_compare_and_last(self) -> None:
        for argv in (["compare", LEFT_ID, RIGHT_ID], ["last", "my-agent-run"]):
            with self.subTest(command=argv[0]):
                with patch(
                    "tracemotive.local_client.HTTPConnection",
                    side_effect=OSError("refused"),
                ):
                    code, stdout, stderr = _capture(argv)
                self.assertEqual(code, 4)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    f"tracemotive {argv[0]}: local TraceMotive server request failed\n"
                    f"tracemotive {argv[0]}: next step: start the local server with: "
                    "tracemotive serve (or run: tracemotive doctor)\n",
                )

    def test_other_local_client_errors_keep_their_existing_text_and_codes(self) -> None:
        code, _, stderr = _capture(["compare", LEFT_ID, RIGHT_ID, "--endpoint", "http://example.com"])
        self.assertEqual(code, 3)
        self.assertNotIn("tracemotive serve", stderr)
        with patch("tracemotive.cli.compare_traces_http", side_effect=ApiContractError("bad contract")):
            code, _, stderr = _capture(["compare", LEFT_ID, RIGHT_ID])
        self.assertEqual(code, 5)
        self.assertEqual(stderr, "tracemotive compare: bad contract\n")

    def test_missing_server_extra_names_exact_install_command(self) -> None:
        with patch.dict(sys.modules, {"uvicorn": None}):
            code, stdout, stderr = _capture(["serve", "--db", ":memory:"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "tracemotive serve requires the server extra; "
            f"install it with: {INSTALL_COMMAND}\n",
        )

    def test_bind_failure_guidance_names_port_and_port_option(self) -> None:
        server = Mock(started=False)
        uvicorn = Mock()
        uvicorn.Server.return_value = server
        collector = Mock()
        with patch.dict(sys.modules, {"uvicorn": uvicorn}), patch(
            "tracemotive.cli.create_serve_app",
            return_value=(Mock(), collector),
        ):
            code, _, stderr = _capture(["serve", "--db", ":memory:", "--port", "9123"])
        self.assertEqual(code, 1)
        self.assertIn("could not bind 127.0.0.1:9123; port 9123 may already be in use", stderr)
        self.assertIn("tracemotive serve --port PORT", stderr)
        collector.close.assert_called_once_with()

    def test_public_parser_surface_keeps_existing_commands(self) -> None:
        help_text = _parser().format_help()
        for command in ("serve", "demo", "compare", "last", "doctor", "--version"):
            self.assertIn(command, help_text)
        with self.assertRaises(SystemExit) as context:
            _parser().parse_args([])
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
