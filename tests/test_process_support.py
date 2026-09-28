from __future__ import annotations

import gc
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
import warnings

import tests.process_support as process_support
from tests.process_support import (
    RetryingTemporaryDirectory,
    remove_tree,
    stop_process,
    wait_for_port_release,
)
import tests.test_demo as demo_tests
import tests.test_packaging as packaging_tests
import tests.test_v02_p0_fullstack as fullstack_tests


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RemoveTreeTests(unittest.TestCase):
    def test_sharing_violation_is_retried_until_removal_succeeds(self) -> None:
        clock = _FakeClock()
        attempts: list[str] = []

        def remove(path: str) -> None:
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError(32, "The process cannot access the file")

        remove_tree("held", timeout=5, remove=remove, sleep=clock.sleep, clock=clock)

        self.assertEqual(attempts, ["held", "held", "held"])
        self.assertEqual(len(clock.sleeps), 2)

    def test_persistent_sharing_violation_is_raised_after_the_deadline(self) -> None:
        clock = _FakeClock()
        attempts = 0

        def remove(path: str) -> None:
            nonlocal attempts
            attempts += 1
            raise PermissionError(32, "The process cannot access the file")

        with self.assertRaises(PermissionError):
            remove_tree("held", timeout=1, remove=remove, sleep=clock.sleep, clock=clock)
        self.assertGreater(attempts, 1)
        self.assertGreaterEqual(clock.now, 1)
        self.assertTrue(all(delay <= 0.5 for delay in clock.sleeps))

    def test_other_errors_propagate_without_retry(self) -> None:
        clock = _FakeClock()
        attempts = 0

        def remove(path: str) -> None:
            nonlocal attempts
            attempts += 1
            raise OSError(28, "No space left on device")

        with self.assertRaises(OSError) as raised:
            remove_tree("full", timeout=5, remove=remove, sleep=clock.sleep, clock=clock)
        self.assertNotIsInstance(raised.exception, PermissionError)
        self.assertEqual(attempts, 1)
        self.assertEqual(clock.sleeps, [])

    def test_missing_directory_is_already_removed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            remove_tree(Path(parent) / "absent", timeout=0)

    def test_real_tree_with_a_database_like_file_is_removed(self) -> None:
        directory = RetryingTemporaryDirectory(prefix="tracemotive-support-")
        root = Path(directory.name)
        nested = root / "nested"
        nested.mkdir()
        (nested / "demo.sqlite3").write_bytes(b"SQLite format 3\x00")
        with directory as name:
            self.assertEqual(name, str(root))
        self.assertFalse(root.exists())
        directory.cleanup()

    def test_read_only_entries_are_removed_without_waiting_for_the_deadline(self) -> None:
        directory = RetryingTemporaryDirectory(prefix="tracemotive-support-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        nested = root / "nested"
        nested.mkdir()
        read_only_file = nested / "packed.idx"
        read_only_file.write_bytes(b"read-only")
        read_only_file.chmod(stat.S_IREAD)
        nested.chmod(stat.S_IREAD | stat.S_IEXEC)
        # timeout=0 allows a single attempt, so success cannot come from the
        # sharing-violation retry loop.
        remove_tree(root, timeout=0)
        self.assertFalse(root.exists())

    def test_read_only_state_outside_the_tree_is_never_changed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            outside = Path(parent) / "outside.txt"
            outside.write_bytes(b"keep read-only")
            outside.chmod(stat.S_IREAD)
            tree = Path(parent) / "tree"
            tree.mkdir()
            try:
                os.symlink(outside, tree / "link.txt")
            except (NotImplementedError, OSError):
                pass  # Windows without symlink privilege; the direct check below still runs.
            error = PermissionError(5, "Access is denied")
            with self.assertRaises(PermissionError):
                process_support._retry_after_clearing_read_only(
                    str(tree), os.unlink, str(outside), error
                )
            if (tree / "link.txt").is_symlink():
                with self.assertRaises(PermissionError):
                    process_support._retry_after_clearing_read_only(
                        str(tree), os.unlink, str(tree / "link.txt"), error
                    )
            remove_tree(tree, timeout=0)
            self.assertTrue(outside.exists())
            self.assertFalse(os.stat(outside).st_mode & stat.S_IWRITE)
            outside.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_sharing_violation_on_a_writable_entry_is_not_masked(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            held = Path(root) / "held.sqlite3"
            held.write_bytes(b"SQLite format 3\x00")
            mode = os.stat(held).st_mode
            error = PermissionError(32, "The process cannot access the file")
            with self.assertRaises(PermissionError) as raised:
                process_support._retry_after_clearing_read_only(root, os.unlink, str(held), error)
            self.assertIs(raised.exception, error)
            self.assertTrue(held.exists())
            self.assertEqual(os.stat(held).st_mode, mode)


class RetryingTemporaryDirectoryLifetimeTests(unittest.TestCase):
    def test_unreferenced_directory_is_removed_with_a_resource_warning(self) -> None:
        directory = RetryingTemporaryDirectory(prefix="tracemotive-support-")
        root = Path(directory.name)
        with self.assertWarnsRegex(ResourceWarning, "Implicitly cleaning up"):
            del directory
            gc.collect()
        self.assertFalse(root.exists())

    def test_explicit_cleanup_disarms_the_implicit_cleanup(self) -> None:
        directory = RetryingTemporaryDirectory(prefix="tracemotive-support-")
        root = Path(directory.name)
        directory.cleanup()
        self.assertFalse(root.exists())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            del directory
            gc.collect()
        self.assertEqual([item for item in caught if item.category is ResourceWarning], [])

    def test_failed_cleanup_stays_armed_for_a_later_attempt(self) -> None:
        directory = RetryingTemporaryDirectory(prefix="tracemotive-support-")
        root = Path(directory.name)
        with patch.object(
            process_support,
            "remove_tree",
            side_effect=PermissionError(32, "The process cannot access the file"),
        ):
            with self.assertRaises(PermissionError):
                directory.cleanup()
        self.assertTrue(root.exists())
        directory.cleanup()
        self.assertFalse(root.exists())


class _RecordingTemporaryDirectory(RetryingTemporaryDirectory):
    created: list[Path] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        type(self).created.append(Path(self.name))


class FailedClassSetupCleanupTests(unittest.TestCase):
    """A failing ``setUpClass`` never reaches ``tearDownClass``; nothing may leak."""

    def _run_class_expecting_setup_error(self, case: type[unittest.TestCase]) -> list[Path]:
        _RecordingTemporaryDirectory.created = []
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("setUpClass", str(result.errors[0][0]))
        self.assertEqual(len(_RecordingTemporaryDirectory.created), 1)
        return _RecordingTemporaryDirectory.created

    def test_demo_setup_failure_before_the_server_starts(self) -> None:
        with (
            patch.object(demo_tests, "RetryingTemporaryDirectory", _RecordingTemporaryDirectory),
            patch.object(demo_tests.subprocess, "Popen", side_effect=OSError("cannot start")),
        ):
            created = self._run_class_expecting_setup_error(demo_tests.DemoTests)
        self.assertFalse(created[0].exists())

    def test_demo_setup_failure_after_the_server_process_exits(self) -> None:
        real_popen = subprocess.Popen

        def exiting_server(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            return real_popen([sys.executable, "-c", "pass"])

        with (
            patch.object(demo_tests, "RetryingTemporaryDirectory", _RecordingTemporaryDirectory),
            patch.object(demo_tests.subprocess, "Popen", side_effect=exiting_server),
        ):
            created = self._run_class_expecting_setup_error(demo_tests.DemoTests)
        self.assertFalse(created[0].exists())

    def test_packaging_setup_failure(self) -> None:
        with (
            patch.object(packaging_tests, "RetryingTemporaryDirectory", _RecordingTemporaryDirectory),
            patch.object(
                packaging_tests,
                "_extract_tracked_checkout",
                side_effect=RuntimeError("checkout failed"),
            ),
        ):
            created = self._run_class_expecting_setup_error(
                packaging_tests.BuiltArtifactPackagingTests
            )
        self.assertFalse(created[0].exists())

    def test_release_fullstack_setup_failure(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".whl", delete=False) as wheel:
            pass
        self.addCleanup(os.unlink, wheel.name)
        case = fullstack_tests.V02P0FullStackTests
        with (
            patch.object(case, "__unittest_skip__", False, create=True),
            patch.dict(os.environ, {"TRACEMOTIVE_V02_22_WHEEL": wheel.name}),
            patch.object(fullstack_tests, "RetryingTemporaryDirectory", _RecordingTemporaryDirectory),
            patch.object(fullstack_tests, "_run", side_effect=RuntimeError("venv failed")),
        ):
            created = self._run_class_expecting_setup_error(case)
        self.assertFalse(created[0].exists())


class ServerProcessTests(unittest.TestCase):
    def test_stop_process_terminates_a_running_process(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        stop_process(process, timeout=10)
        self.assertIsNotNone(process.returncode)

    def test_stop_process_kills_a_process_that_ignores_terminate(self) -> None:
        script = textwrap.dedent(
            """
            import signal
            import sys
            import time

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            sys.stdout.write("ready\\n")
            sys.stdout.flush()
            time.sleep(60)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            started = time.monotonic()
            stop_process(process, timeout=0.5)
            self.assertIsNotNone(process.returncode)
            self.assertLess(time.monotonic() - started, 30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()

    def test_stop_process_accepts_an_already_exited_process(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=30)
        stop_process(process, timeout=1)
        self.assertEqual(process.returncode, 0)

    def test_port_release_waits_for_the_listener_to_close(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        try:
            with self.assertRaisesRegex(AssertionError, "still accepts connections"):
                wait_for_port_release(port, timeout=0.3)
        finally:
            listener.close()
        wait_for_port_release(port, timeout=5)


if __name__ == "__main__":
    unittest.main()
