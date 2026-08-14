import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tracemotive.cli import DEFAULT_PORT, _parser, _port_value, create_serve_app
from tracemotive.storage import Repository, resolve_database_path
from tracemotive.ui import get_ui_root
from tracemotive.ui.server import PackagedUIError
from tests.test_collector import TRACE_ID, batch, event, make_trace
from tests.test_collector_http import http_request
from tests.test_query_api import request


class ServeCliTests(unittest.TestCase):
    def test_parser_has_fixed_loopback_command_without_host_option(self):
        arguments = _parser().parse_args(["serve", "--db", "state.sqlite3", "--port", "9876"])
        self.assertEqual(arguments.command, "serve")
        self.assertEqual(arguments.db, "state.sqlite3")
        self.assertEqual(arguments.port, 9876)
        self.assertEqual(DEFAULT_PORT, 8765)

        with self.assertRaises(SystemExit) as context:
            _parser().parse_args(["serve", "--host", "127.0.0.1"])
        self.assertEqual(context.exception.code, 2)

    def test_port_validation_rejects_invalid_values(self):
        self.assertEqual(_port_value("1"), 1)
        self.assertEqual(_port_value("65535"), 65535)
        for value in ("0", "65536", "not-a-port"):
            with self.assertRaises(Exception):
                _port_value(value)

    def test_unavailable_port_fails_without_selecting_another_port(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            port = blocker.getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from tracemotive.cli import main; raise SystemExit(main())",
                        "serve",
                        "--db",
                        str(Path(directory) / "state.sqlite3"),
                        "--port",
                        str(port),
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not bind 127.0.0.1", result.stderr)

    def test_serve_database_resolution_preserves_explicit_env_and_default_order(self):
        explicit = resolve_database_path(
            "/explicit.sqlite3",
            environ={"TRACEMOTIVE_DB": "/environment.sqlite3"},
            platform="linux",
            home="/home/alice",
        )
        self.assertEqual(explicit, "/explicit.sqlite3")

        with patch.dict(os.environ, {"TRACEMOTIVE_DB": "/environment.sqlite3"}, clear=True):
            self.assertEqual(resolve_database_path(), "/environment.sqlite3")

    def test_default_serve_resolution_is_file_backed(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = resolve_database_path(
                environ={},
                platform="linux",
                home=Path(directory) / "home",
            )
            self.assertNotEqual(database_path, ":memory:")
            app, collector = create_serve_app(database_path)
            try:
                self.assertEqual(Path(collector.repository.path), Path(database_path))
                self.assertTrue(Path(database_path).is_file())
            finally:
                collector.close()

    def test_create_serve_app_serves_packaged_ui_and_existing_api(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state" / "tracemotive.sqlite3"
            app, collector = create_serve_app(str(database_path))
            try:
                status, body, _ = http_request(app, b"", method="GET", path="/")
                self.assertEqual(status, 200)
                self.assertIn(b'<div id="root">', body)
                self.assertIn(b'/assets/', body)

                asset = next(
                    item
                    for item in get_ui_root().joinpath("assets").iterdir()
                    if item.name.endswith((".js", ".css"))
                )
                status, body, _ = http_request(
                    app,
                    b"",
                    method="GET",
                    path=f"/assets/{asset.name}",
                )
                self.assertEqual(status, 200)
                self.assertTrue(body)

                status, body, _ = http_request(
                    app,
                    b"",
                    method="GET",
                    path="/api/v1/health",
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, b'{"status":"ok"}')

                status, body, _ = http_request(
                    app,
                    b"",
                    method="GET",
                    path="/api/v1/not-a-route",
                )
                self.assertEqual(status, 404)
                self.assertNotIn(b"<div id=\"root\">", body)

                status, _, _ = http_request(
                    app,
                    b"",
                    method="GET",
                    path="/assets/",
                )
                self.assertEqual(status, 404)
                status, _, _ = http_request(
                    app,
                    b"",
                    method="GET",
                    path="/assets/../index.html",
                )
                self.assertEqual(status, 404)
            finally:
                collector.close()

    def test_explicit_memory_mode_remains_ephemeral_for_serve_app(self):
        app, collector = create_serve_app(":memory:")
        try:
            self.assertEqual(collector.repository.path, ":memory:")
            self.assertIn("/api/v1/health", {route.path for route in app.routes})
        finally:
            collector.close()

    def test_persistent_serve_app_reopens_trace_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tracemotive.sqlite3"
            first_app, first_collector = create_serve_app(str(database_path))
            try:
                self.assertEqual(
                    first_collector.ingest(batch(event("trace.ended", make_trace(ended=True)))),
                    {"accepted": 1, "duplicates": 0, "stale": 0},
                )
            finally:
                first_collector.close()

            second_app, second_collector = create_serve_app(str(database_path))
            try:
                self.assertEqual(request(second_app, "GET", f"/api/v1/traces/{TRACE_ID}")[0], 200)
                self.assertIsNotNone(second_collector.repository.get_trace(TRACE_ID))
            finally:
                second_collector.close()

    def test_missing_packaged_ui_fails_before_app_is_returned(self):
        class MissingUI:
            def joinpath(self, *parts):
                del parts
                return self

            def is_file(self):
                return False

        with tempfile.TemporaryDirectory() as directory:
            with patch("tracemotive.ui.server.get_ui_root", return_value=MissingUI()):
                with self.assertRaisesRegex(PackagedUIError, "index.html is missing"):
                    create_serve_app(str(Path(directory) / "state.sqlite3"))

    def test_bare_programmatic_defaults_remain_memory_backed(self):
        with Repository() as repository:
            self.assertEqual(repository.path, ":memory:")


if __name__ == "__main__":
    unittest.main()
