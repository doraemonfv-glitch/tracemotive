import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tracemotive.canonical import CaptureInfo
from tracemotive.canonical.models import _canonical_json_dumps
from tracemotive.collector import create_app
from tracemotive.storage import (
    CURRENT_MIGRATION_VERSION,
    DatabasePathError,
    MigrationError,
    NewerDatabaseError,
    Repository,
)
from tests.test_collector import SPAN_ID, TRACE_ID, batch, event, make_span, make_trace
from tests.test_collector_http import http_request
from tests.test_query_api import request


class PersistentLifecycleTests(unittest.TestCase):
    def test_file_backed_collector_restart_preserves_trace_span_io_and_idempotency(self):
        trace = make_trace(
            ended=True,
            metadata={"exact_integer": 9_007_199_254_740_993},
        )
        span = make_span(
            ended=True,
            input_value={"prompt": "persisted input"},
            input_capture=CaptureInfo("captured", None, False),
            output_value={"answer": "persisted output"},
        )
        trace_event = event(
            "trace.ended",
            trace,
            event_id="00000000-0000-4000-8000-000000000201",
        )
        span_event = event(
            "span.ended",
            span,
            event_id="00000000-0000-4000-8000-000000000202",
        )

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state" / "tracemotive.sqlite3"
            first_app = create_app(database_path=database_path, clock=lambda: 123)
            first_collector = first_app.state.tracemotive_collector
            try:
                status, body, _ = http_request(
                    first_app,
                    _canonical_json_dumps(batch(trace_event, span_event)).encode("utf-8"),
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"accepted": 2, "duplicates": 0, "stale": 0})
            finally:
                first_collector.close()

            second_app = create_app(database_path=database_path, clock=lambda: 456)
            second_collector = second_app.state.tracemotive_collector
            try:
                status, body = request(second_app, "GET", f"/api/v1/traces/{TRACE_ID}")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["trace"]["metadata"]["exact_integer"], 9_007_199_254_740_993)
                self.assertEqual(second_collector.repository.get_trace(TRACE_ID), trace)
                self.assertEqual(second_collector.repository.get_span(TRACE_ID, SPAN_ID), span)
                self.assertIsNotNone(second_collector.repository.get_ingest_event(trace_event["event_id"]))
                self.assertIsNotNone(second_collector.repository.get_ingest_event(span_event["event_id"]))
                self.assertEqual(
                    second_collector.ingest(batch(trace_event, span_event)),
                    {"accepted": 0, "duplicates": 2, "stale": 0},
                )
            finally:
                second_collector.close()

    def test_file_backed_repository_reopens_repeatedly_without_losing_state(self):
        trace = make_trace(ended=True)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tracemotive.sqlite3"
            with Repository(database_path) as repository:
                repository.upsert_trace(trace, now_us=123)

            for _ in range(3):
                with Repository(database_path) as reopened:
                    self.assertEqual(reopened.get_trace(TRACE_ID), trace)
                    self.assertEqual(
                        reopened.connection.execute(
                            "SELECT MAX(version) FROM schema_migrations"
                        ).fetchone()[0],
                        CURRENT_MIGRATION_VERSION,
                    )

    def test_supported_migration_is_applied_and_database_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pending-migration.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_us INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO schema_migrations VALUES (0, 0)")
            connection.commit()
            connection.close()

            with Repository(database_path) as migrated:
                self.assertEqual(
                    migrated.connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    CURRENT_MIGRATION_VERSION,
                )
                self.assertIsNotNone(
                    migrated.connection.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'traces'"
                    ).fetchone()
                )

            with Repository(database_path) as reopened:
                self.assertEqual(
                    reopened.connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    CURRENT_MIGRATION_VERSION,
                )

    def test_migration_initialization_failure_preserves_existing_database(self):
        trace = make_trace(ended=True)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "existing.sqlite3"
            with Repository(database_path) as repository:
                repository.upsert_trace(trace, now_us=123)
            before = database_path.read_bytes()

            with patch(
                "tracemotive.storage.repository.run_migrations",
                side_effect=MigrationError("forced migration failure"),
            ):
                with self.assertRaises(MigrationError):
                    Repository(database_path)

            self.assertEqual(database_path.read_bytes(), before)
            with Repository(database_path) as reopened:
                self.assertEqual(reopened.get_trace(TRACE_ID), trace)

    def test_newer_database_is_rejected_without_memory_fallback_or_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "newer.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_us INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, 123)",
                (CURRENT_MIGRATION_VERSION + 1,),
            )
            connection.commit()
            connection.close()
            before = database_path.read_bytes()

            with self.assertRaises(NewerDatabaseError):
                create_app(database_path=database_path)

            self.assertEqual(database_path.read_bytes(), before)

    def test_file_backed_delete_survives_restart_and_reingest_remains_allowed(self):
        trace_event = event(
            "trace.started",
            make_trace(),
            event_id="00000000-0000-4000-8000-000000000203",
        )
        span_event = event(
            "span.ended",
            make_span(
                ended=True,
                input_value={"prompt": "delete me"},
                input_capture=CaptureInfo("captured", None, False),
            ),
            event_id="00000000-0000-4000-8000-000000000204",
        )

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tracemotive.sqlite3"
            first_app = create_app(database_path=database_path, clock=lambda: 123)
            first_collector = first_app.state.tracemotive_collector
            try:
                self.assertEqual(
                    first_collector.ingest(batch(trace_event, span_event)),
                    {"accepted": 2, "duplicates": 0, "stale": 0},
                )
                self.assertEqual(request(first_app, "DELETE", f"/api/v1/traces/{TRACE_ID}")[0], 204)
                self.assertEqual(request(first_app, "DELETE", f"/api/v1/traces/{TRACE_ID}")[0], 204)
                for table in ("traces", "spans", "span_io", "ingest_events"):
                    self.assertEqual(
                        first_collector.repository.connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE trace_id = ?", (TRACE_ID,)
                        ).fetchone()[0],
                        0,
                    )
            finally:
                first_collector.close()

            second_app = create_app(database_path=database_path, clock=lambda: 456)
            second_collector = second_app.state.tracemotive_collector
            try:
                self.assertEqual(request(second_app, "GET", f"/api/v1/traces/{TRACE_ID}")[0], 404)
                self.assertEqual(request(second_app, "DELETE", f"/api/v1/traces/{TRACE_ID}")[0], 204)
                self.assertEqual(
                    second_collector.ingest(batch(trace_event)),
                    {"accepted": 1, "duplicates": 0, "stale": 0},
                )
                self.assertIsNotNone(second_collector.repository.get_trace(TRACE_ID))
            finally:
                second_collector.close()

    def test_persistent_main_database_and_created_sidecars_exclude_raw_secret(self):
        secret = "V02-02-RECOGNIZABLE-SECRET"
        span = make_span(
            ended=True,
            input_value={"password": secret},
            input_capture=CaptureInfo("captured", None, False),
            output_value={"authorization": f"Bearer {secret}"},
        )
        span_event = event(
            "span.ended",
            span,
            event_id="00000000-0000-4000-8000-000000000205",
        )

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tracemotive.sqlite3"
            app = create_app(database_path=database_path, clock=lambda: 123)
            collector = app.state.tracemotive_collector
            observed_files: set[Path] = set()
            try:
                with collector.repository.transaction():
                    self.assertEqual(
                        collector.ingest(batch(span_event)),
                        {"accepted": 1, "duplicates": 0, "stale": 0},
                    )
                    observed_files.update(self._database_files(database_path))
                    self._assert_secret_absent(secret, observed_files)
                    stored = collector.repository.get_span(TRACE_ID, SPAN_ID)
                    self.assertEqual(stored.input, {"password": "[REDACTED]"})
                    self.assertEqual(stored.output, {"authorization": "[REDACTED]"})
                    self.assertTrue(stored.capture.input.redacted)
                    self.assertTrue(stored.capture.output.redacted)
            finally:
                collector.close()

            after_close_files = self._database_files(database_path)
            self.assertIn(database_path, after_close_files)
            self._assert_secret_absent(secret, after_close_files)

    def test_path_failure_does_not_include_recognizable_secret_or_fallback(self):
        secret = "V02-02-PATH-SECRET"
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / secret / "tracemotive.sqlite3"
            with patch("tracemotive.storage.paths.Path.mkdir", side_effect=PermissionError):
                with self.assertRaises(DatabasePathError) as context:
                    Repository(database_path)
            self.assertNotIn(secret, str(context.exception))
            self.assertFalse(database_path.exists())

    @staticmethod
    def _database_files(database_path: Path) -> set[Path]:
        candidates = {database_path}
        candidates.update(database_path.parent.glob(database_path.name + "-*"))
        return {candidate for candidate in candidates if candidate.is_file()}

    def _assert_secret_absent(self, secret: str, database_files: set[Path]) -> None:
        self.assertTrue(database_files)
        secret_bytes = secret.encode("utf-8")
        for database_file in database_files:
            self.assertNotIn(secret_bytes, database_file.read_bytes(), database_file.name)


if __name__ == "__main__":
    unittest.main()
