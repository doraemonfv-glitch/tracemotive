from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from tests.process_support import (
    RetryingTemporaryDirectory,
    remove_tree,
    stop_process,
    wait_for_port_release,
)


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
