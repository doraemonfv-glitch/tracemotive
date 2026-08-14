import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tracemotive.collector import create_app
from tracemotive.storage import DatabasePathError, Repository, resolve_database_path
from tests.test_collector import TRACE_ID, batch, event, make_trace


class DatabasePathTests(unittest.TestCase):
    def test_repository_default_remains_memory_backed(self):
        with Repository() as repository:
            self.assertEqual(repository.path, ":memory:")

    def test_create_app_default_remains_memory_backed(self):
        app = create_app()
        collector = app.state.tracemotive_collector
        try:
            self.assertEqual(collector.repository.path, ":memory:")
        finally:
            collector.close()

    def test_create_app_explicit_file_database_persists_ingested_trace_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state" / "tracemotive.sqlite3"
            app = create_app(database_path=database_path, clock=lambda: 123)
            collector = app.state.tracemotive_collector
            try:
                self.assertEqual(collector.repository.path, str(database_path))
                self.assertEqual(
                    collector.ingest(batch(event("trace.ended", make_trace(ended=True)))),
                    {"accepted": 1, "duplicates": 0, "stale": 0},
                )
            finally:
                collector.close()

            with Repository(database_path) as reopened:
                self.assertEqual(reopened.get_trace(TRACE_ID), make_trace(ended=True))

    def test_existing_file_database_reopens_without_new_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tracemotive.sqlite3"
            with Repository(database_path) as repository:
                repository.upsert_trace(make_trace(), now_us=123)

            with Repository(database_path) as reopened:
                self.assertEqual(reopened.get_trace(TRACE_ID), make_trace())
                self.assertEqual(
                    reopened.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                    1,
                )

    def test_explicit_path_precedes_environment(self):
        self.assertEqual(
            resolve_database_path(
                "/explicit/tracemotive.sqlite3",
                environ={"TRACEMOTIVE_DB": "/environment/tracemotive.sqlite3"},
                platform="linux",
                home="/home/alice",
            ),
            "/explicit/tracemotive.sqlite3",
        )

    def test_environment_path_precedes_platform_default(self):
        self.assertEqual(
            resolve_database_path(
                environ={"TRACEMOTIVE_DB": "/environment/tracemotive.sqlite3"},
                platform="linux",
                home="/home/alice",
            ),
            "/environment/tracemotive.sqlite3",
        )

    def test_explicit_memory_mode_is_preserved(self):
        self.assertEqual(
            resolve_database_path(
                ":memory:",
                environ={"TRACEMOTIVE_DB": "/environment/tracemotive.sqlite3"},
                platform="linux",
                home="/home/alice",
            ),
            ":memory:",
        )

    def test_windows_default_path_is_host_independent(self):
        self.assertEqual(
            resolve_database_path(
                environ={"LOCALAPPDATA": r"C:\Users\alice\AppData\Local"},
                platform="win32",
                home=r"C:\Users\alice",
            ),
            r"C:\Users\alice\AppData\Local\TraceMotive\tracemotive.sqlite3",
        )

    def test_windows_default_path_uses_user_local_application_data_fallback(self):
        self.assertEqual(
            resolve_database_path(
                environ={},
                platform="win32",
                home=r"C:\Users\alice",
            ),
            r"C:\Users\alice\AppData\Local\TraceMotive\tracemotive.sqlite3",
        )

    def test_macos_default_path_is_host_independent(self):
        self.assertEqual(
            resolve_database_path(environ={}, platform="darwin", home="/Users/alice"),
            "/Users/alice/Library/Application Support/TraceMotive/tracemotive.sqlite3",
        )

    def test_linux_xdg_default_path_is_host_independent(self):
        self.assertEqual(
            resolve_database_path(
                environ={"XDG_DATA_HOME": "/data/alice"},
                platform="linux",
                home="/home/alice",
            ),
            "/data/alice/tracemotive/tracemotive.sqlite3",
        )

    def test_linux_xdg_fallback_path_is_host_independent(self):
        self.assertEqual(
            resolve_database_path(environ={}, platform="linux", home="/home/alice"),
            "/home/alice/.local/share/tracemotive/tracemotive.sqlite3",
        )

    def test_file_database_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "private" / "nested" / "tracemotive.sqlite3"
            with Repository(database_path) as repository:
                self.assertEqual(repository.path, str(database_path))
            self.assertTrue(database_path.parent.is_dir())
            self.assertTrue(database_path.is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(database_path.parent.stat().st_mode) & 0o077, 0)
                self.assertEqual(stat.S_IMODE(database_path.stat().st_mode) & 0o077, 0)

    def test_invalid_directory_path_fails_without_memory_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DatabasePathError):
                Repository(directory)
        with self.assertRaises(DatabasePathError):
            Repository("   ")

    def test_parent_permission_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "unavailable" / "tracemotive.sqlite3"
            with patch("tracemotive.storage.paths.Path.mkdir", side_effect=PermissionError):
                with self.assertRaisesRegex(DatabasePathError, "could not create database directory"):
                    Repository(database_path)

    def test_create_app_rejects_ambiguous_repository_and_database_path(self):
        with Repository() as repository:
            with self.assertRaises(ValueError):
                create_app(repository, database_path=":memory:")


if __name__ == "__main__":
    unittest.main()
