import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tracemotive.canonical import (
    AGENTLENS_SCHEMA_VERSION,
    Capture,
    CaptureInfo,
    Error,
    LLMDetails,
    LLMUsage,
    Span,
    SpanSource,
    Trace,
    TraceSource,
    ValidationError,
)
from tracemotive.storage import (
    CURRENT_MIGRATION_VERSION,
    EntityConflictError,
    NewerDatabaseError,
    Repository,
)


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
OTHER_TRACE_ID = "5bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
OTHER_SPAN_ID = "00f067aa0ba902b8"
SOURCE = TraceSource("framework", "1.0", "integration", "1.0", "native-trace")


def make_trace(trace_id=TRACE_ID, *, ended=False):
    return Trace(
        AGENTLENS_SCHEMA_VERSION,
        trace_id,
        "Agent workflow",
        "2026-08-10T13:00:00.000000Z",
        "2026-08-10T13:00:01.000000Z" if ended else None,
        "ok" if ended else "unset",
        SOURCE,
        {"request": "Bearer [REDACTED]"},
        {"attempt": 1},
    )


def make_span(trace_id=TRACE_ID, span_id=SPAN_ID, *, parent_span_id=None, ended=False):
    return Span(
        AGENTLENS_SCHEMA_VERSION,
        trace_id,
        span_id,
        parent_span_id,
        "llm",
        "llm.generate",
        "generation",
        "2026-08-10T13:00:00.100000Z",
        "2026-08-10T13:00:00.900000Z" if ended else None,
        "error" if ended else "unset",
        None if not ended else Error("TimeoutError", "request timed out"),
        {"prompt": "hello"} if ended else None,
        {"answer": "world"} if ended else None,
        Capture(
            CaptureInfo("captured" if ended else "not_captured", None if ended else "disabled", False),
            CaptureInfo("captured" if ended else "not_captured", None if ended else "not_yet_available", False),
        ),
        SpanSource("framework", "1.0", "integration", "1.0", "native-trace", "native-span", None),
        {"request": "api_key=[REDACTED]"},
        {"attempt": 1},
        LLMDetails(
            "llm",
            "openai",
            "gpt-request",
            "gpt-response" if ended else None,
            "resp_1" if ended else None,
            LLMUsage(2, 3 if ended else None, None, None, None),
            ["stop"] if ended else [],
            {"temperature": 0} if ended else None,
            None,
        ),
    )


def replace_span_io(span, *, input_value, output_value):
    return replace(
        span,
        input=input_value,
        output=output_value,
        capture=Capture(
            CaptureInfo("captured", None, False),
            CaptureInfo("captured", None, False),
        ),
    )


class StorageTests(unittest.TestCase):
    def test_schema_tables_indexes_and_composite_span_identity(self):
        with Repository() as repository:
            objects = {
                row["name"]: row["type"]
                for row in repository.connection.execute(
                    "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index')"
                )
            }
            for name in (
                "schema_migrations",
                "traces",
                "spans",
                "span_io",
                "ingest_events",
                "idx_spans_trace_started_at",
                "idx_spans_trace_parent",
                "idx_spans_trace_type",
                "idx_spans_trace_status",
            ):
                self.assertIn(name, objects)

            expected_columns = {
                "schema_migrations": {"version", "applied_at_us"},
                "traces": {
                    "trace_id", "schema_version", "name", "started_at_us", "ended_at_us",
                    "lifecycle_stage", "status", "source_json", "metadata_json",
                    "attributes_json", "created_at_us", "updated_at_us",
                },
                "spans": {
                    "trace_id", "span_id", "schema_version", "parent_span_id", "type",
                    "operation", "name", "started_at_us", "ended_at_us", "lifecycle_stage",
                    "status", "error_json", "source_json", "metadata_json", "attributes_json",
                    "details_json", "created_at_us", "updated_at_us",
                },
                "span_io": {
                    "trace_id", "span_id", "input_json", "output_json",
                    "input_capture_json", "output_capture_json",
                },
                "ingest_events": {
                    "event_id", "event_content_sha256", "event_type", "trace_id", "span_id",
                    "received_at_us",
                },
            }
            for table, columns in expected_columns.items():
                actual = {
                    row["name"]
                    for row in repository.connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertEqual(actual, columns)

            repository.upsert_span(make_span(TRACE_ID), now_us=10)
            repository.upsert_span(
                make_span(TRACE_ID, OTHER_SPAN_ID, parent_span_id=SPAN_ID), now_us=11
            )
            repository.upsert_span(make_span(OTHER_TRACE_ID), now_us=12)
            self.assertIsNotNone(repository.get_span(TRACE_ID, SPAN_ID))
            self.assertIsNotNone(repository.get_span(TRACE_ID, OTHER_SPAN_ID))
            self.assertIsNotNone(repository.get_span(OTHER_TRACE_ID, SPAN_ID))
            with self.assertRaises(sqlite3.IntegrityError):
                repository.connection.execute(
                    "INSERT INTO spans (trace_id, span_id, schema_version, parent_span_id, type, operation, name, started_at_us, ended_at_us, lifecycle_stage, status, error_json, source_json, metadata_json, attributes_json, details_json, created_at_us, updated_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (TRACE_ID, SPAN_ID, "0.1", None, "llm", "x", "x", 1, None, 1, "unset", None, "{}", "{}", "{}", "{}", 1, 1),
                )

    def test_migration_runner_persists_version_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "tracemotive.db")
            with Repository(path) as repository:
                repository.upsert_trace(make_trace(), now_us=100)
                version_rows = repository.connection.execute(
                    "SELECT version, applied_at_us FROM schema_migrations"
                ).fetchall()
                self.assertEqual([(CURRENT_MIGRATION_VERSION, version_rows[0][1])], [tuple(row) for row in version_rows])
            with Repository(path) as repository:
                self.assertEqual(repository.get_trace(TRACE_ID), make_trace())
                self.assertEqual(
                    repository.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                    1,
                )

    def test_newer_database_is_refused_without_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "newer.db")
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_us INTEGER NOT NULL)")
            connection.execute("INSERT INTO schema_migrations VALUES (2, 123)")
            connection.commit()
            connection.close()
            before = Path(path).read_bytes()
            with self.assertRaises(NewerDatabaseError) as context:
                Repository(path)
            self.assertIn("newer TraceMotive version created the database", str(context.exception))
            self.assertEqual(Path(path).read_bytes(), before)

    def test_failed_migration_rolls_back_and_refuses_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "broken.db")
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_us INTEGER NOT NULL)")
            connection.execute("INSERT INTO schema_migrations VALUES (0, 0)")
            connection.execute("CREATE TABLE spans (unusable INTEGER)")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                Repository(path)
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 0)
            connection.close()

    def test_canonical_reconstruction_preserves_full_trace_and_span(self):
        with Repository() as repository:
            trace = make_trace(ended=True)
            span = make_span(ended=True)
            repository.upsert_trace(trace, now_us=100)
            repository.upsert_span(span, now_us=101)
            self.assertEqual(repository.get_trace(TRACE_ID).to_dict(), trace.to_dict())
            self.assertEqual(repository.get_span(TRACE_ID, SPAN_ID).to_dict(), span.to_dict())
            self.assertEqual(repository.get_span(TRACE_ID, SPAN_ID).to_json(), span.to_json())

    def test_span_io_is_separate_and_captured_json_null_is_preserved(self):
        with Repository() as repository:
            span = make_span(ended=True)
            span = Span(
                span.schema_version,
                span.trace_id,
                span.span_id,
                span.parent_span_id,
                span.type,
                span.operation,
                span.name,
                span.started_at,
                span.ended_at,
                span.status,
                span.error,
                None,
                None,
                Capture(CaptureInfo("captured", None, False), CaptureInfo("captured", None, False)),
                span.source,
                span.metadata,
                span.attributes,
                span.details,
            )
            repository.upsert_span(span, now_us=100)
            row = repository.connection.execute(
                "SELECT input_json, output_json FROM span_io WHERE trace_id = ? AND span_id = ?",
                (TRACE_ID, SPAN_ID),
            ).fetchone()
            self.assertEqual((row["input_json"], row["output_json"]), ("null", "null"))
            self.assertEqual(repository.get_span(TRACE_ID, SPAN_ID).to_dict(), span.to_dict())

    def test_rehydration_preserves_captureinfo_history_and_canonical_json(self):
        redacted_input = replace_span_io(
            make_span(span_id=SPAN_ID, ended=True),
            input_value={"api_key": "input-secret"},
            output_value=None,
        )
        redacted_output = replace_span_io(
            make_span(span_id=OTHER_SPAN_ID, ended=True),
            input_value=None,
            output_value="Bearer output-secret",
        )
        safe = make_span(trace_id=OTHER_TRACE_ID, span_id=SPAN_ID, ended=True)
        started = make_span(trace_id=OTHER_TRACE_ID, span_id=OTHER_SPAN_ID)

        with Repository() as repository:
            for span in (redacted_input, redacted_output, safe, started):
                repository.upsert_span(span, now_us=100)

            restored_input = repository.get_span(TRACE_ID, SPAN_ID)
            restored_output = repository.get_span(TRACE_ID, OTHER_SPAN_ID)
            restored_safe = repository.get_span(OTHER_TRACE_ID, SPAN_ID)
            restored_started = repository.get_span(OTHER_TRACE_ID, OTHER_SPAN_ID)

            self.assertEqual(restored_input.to_json(), redacted_input.to_json())
            self.assertEqual(restored_output.to_json(), redacted_output.to_json())
            self.assertEqual(restored_safe.to_json(), safe.to_json())
            self.assertEqual(restored_started.to_json(), started.to_json())
            self.assertTrue(restored_input.capture.input.redacted)
            self.assertTrue(restored_output.capture.output.redacted)
            self.assertFalse(restored_safe.capture.input.redacted)
            self.assertFalse(restored_safe.capture.output.redacted)
            self.assertEqual(restored_started.capture.input.reason, "disabled")
            self.assertEqual(restored_started.capture.output.reason, "not_yet_available")

    def test_rehydration_rejects_malformed_stored_canonical_content(self):
        with Repository() as repository:
            repository.upsert_span(make_span(ended=True), now_us=100)
            with repository.transaction() as connection:
                connection.execute(
                    "UPDATE span_io SET input_json = ? WHERE trace_id = ? AND span_id = ?",
                    ("{", TRACE_ID, SPAN_ID),
                )
            with self.assertRaises(ValidationError):
                repository.get_span(TRACE_ID, SPAN_ID)

    def test_duplicate_noop_does_not_update_timestamp(self):
        with Repository() as repository:
            trace = make_trace()
            repository.upsert_trace(trace, now_us=100)
            repository.upsert_trace(trace, now_us=200)
            row = repository.connection.execute(
                "SELECT created_at_us, updated_at_us FROM traces WHERE trace_id = ?", (TRACE_ID,)
            ).fetchone()
            self.assertEqual(tuple(row), (100, 100))

            span = make_span()
            repository.upsert_span(span, now_us=300)
            repository.upsert_span(span, now_us=400)
            row = repository.connection.execute(
                "SELECT created_at_us, updated_at_us FROM spans WHERE trace_id = ? AND span_id = ?",
                (TRACE_ID, SPAN_ID),
            ).fetchone()
            self.assertEqual(tuple(row), (300, 300))

    def test_lifecycle_promotion_and_conflicts_are_monotonic(self):
        with Repository() as repository:
            started = make_trace()
            ended = make_trace(ended=True)
            self.assertTrue(repository.upsert_trace(started, now_us=100))
            self.assertTrue(repository.upsert_trace(ended, now_us=200))
            self.assertFalse(repository.upsert_trace(started, now_us=300))
            self.assertEqual(repository.get_trace(TRACE_ID), ended)

            conflicting = make_trace()
            conflicting = Trace(
                conflicting.schema_version,
                conflicting.trace_id,
                "Different name",
                conflicting.started_at,
                None,
                "unset",
                conflicting.source,
                conflicting.metadata,
                conflicting.attributes,
            )
            with self.assertRaises(EntityConflictError):
                repository.upsert_trace(conflicting, now_us=400)

    def test_ingest_event_record_and_transactional_deletion(self):
        event_id = "4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4"
        event_hash = hashlib.sha256(b"event").hexdigest()
        with Repository() as repository:
            repository.upsert_trace(make_trace(), now_us=100)
            repository.upsert_span(make_span(), now_us=101)
            repository.record_ingest_event(
                event_id=event_id,
                event_content_sha256=event_hash,
                event_type="span.started",
                trace_id=TRACE_ID,
                span_id=SPAN_ID,
                received_at_us=102,
            )
            self.assertEqual(repository.get_ingest_event(event_id)["trace_id"], TRACE_ID)
            self.assertTrue(repository.delete_trace(TRACE_ID))
            for table in ("traces", "spans", "span_io", "ingest_events"):
                self.assertEqual(
                    repository.connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE trace_id = ?", (TRACE_ID,)
                    ).fetchone()[0],
                    0,
                )
            self.assertFalse(repository.delete_trace(TRACE_ID))

            repository.upsert_span(make_span(), now_us=200)
            self.assertIsNotNone(repository.get_span(TRACE_ID, SPAN_ID))
            repository.record_ingest_event(
                event_id=event_id,
                event_content_sha256=event_hash,
                event_type="span.started",
                trace_id=TRACE_ID,
                span_id=SPAN_ID,
                received_at_us=201,
            )
            self.assertIsNotNone(repository.get_ingest_event(event_id))

    def test_transactional_deletion_rolls_back_all_related_rows_on_failure(self):
        event_id = "4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4"
        with Repository() as repository:
            repository.upsert_trace(make_trace(), now_us=100)
            repository.upsert_span(make_span(), now_us=101)
            repository.record_ingest_event(
                event_id=event_id,
                event_content_sha256=hashlib.sha256(b"event").hexdigest(),
                event_type="span.started",
                trace_id=TRACE_ID,
                span_id=SPAN_ID,
                received_at_us=102,
            )
            repository.connection.execute(
                """
                CREATE TRIGGER fail_span_delete BEFORE DELETE ON spans
                BEGIN SELECT RAISE(ABORT, 'test deletion failure'); END
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                repository.delete_trace(TRACE_ID)
            for table in ("traces", "spans", "span_io", "ingest_events"):
                self.assertEqual(
                    repository.connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE trace_id = ?", (TRACE_ID,)
                    ).fetchone()[0],
                    1,
                )

    def test_invalid_event_hash_and_ids_are_rejected_before_write(self):
        with Repository() as repository:
            with self.assertRaises(ValueError):
                repository.record_ingest_event(
                    event_id="not-a-uuid",
                    event_content_sha256="0" * 64,
                    event_type="span.started",
                    trace_id=TRACE_ID,
                )
            with self.assertRaises(ValueError):
                repository.record_ingest_event(
                    event_id="4dbd9b3f-9c54-42ed-b0c0-529e99c35ca4",
                    event_content_sha256="not-a-hash",
                    event_type="span.started",
                    trace_id=TRACE_ID,
                )


if __name__ == "__main__":
    unittest.main()
