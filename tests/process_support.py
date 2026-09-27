"""Cross-platform helpers for tests that run a real local server process.

On Windows, a virtual environment's ``python.exe`` is a launcher that runs the
base interpreter as a child process.  ``Popen.wait()`` observes the launcher,
while the child that owns the listening socket and the SQLite file handle can
exit slightly later.  Deleting the database directory immediately can then
fail with a sharing violation (``WinError 32``).  These helpers wait for the
observable effects of shutdown and retry only that class of failure, within a
bounded deadline, instead of ignoring cleanup errors.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Callable


DEFAULT_STOP_TIMEOUT_SECONDS = 10.0
DEFAULT_RELEASE_TIMEOUT_SECONDS = 10.0
DEFAULT_REMOVE_TIMEOUT_SECONDS = 10.0


def stop_process(
    process: subprocess.Popen[Any],
    *,
    timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS,
) -> None:
    """Terminate a process, escalating to kill after a bounded wait."""

    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def wait_for_port_release(
    port: int,
    *,
    host: str = "127.0.0.1",
    timeout: float = DEFAULT_RELEASE_TIMEOUT_SECONDS,
) -> None:
    """Wait until nothing accepts TCP connections on a loopback port.

    A stopped server that still accepts connections means a process is still
    alive and may still hold its database open, so this raises rather than
    returning silently.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                pass
        except OSError:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{host}:{port} still accepts connections after the server process stopped"
            )
        time.sleep(0.05)


def remove_tree(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_REMOVE_TIMEOUT_SECONDS,
    remove: Callable[[str], None] = shutil.rmtree,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Remove a directory tree, retrying transient sharing violations.

    Only ``PermissionError`` (how Windows reports ``WinError 32`` and
    ``WinError 5``) is retried, and only until ``timeout`` elapses; the last
    error is then re-raised.  Every other error propagates immediately.
    """

    target = os.fspath(path)
    deadline = clock() + timeout
    delay = 0.05
    while True:
        try:
            remove(target)
            return
        except FileNotFoundError:
            if not Path(target).exists():
                return
            if clock() >= deadline:
                raise
        except PermissionError:
            if clock() >= deadline:
                raise
        sleep(delay)
        delay = min(delay * 2, 0.5)


class RetryingTemporaryDirectory:
    """A ``tempfile.TemporaryDirectory`` replacement with bounded retry cleanup."""

    def __init__(
        self,
        prefix: str | None = None,
        *,
        cleanup_timeout: float = DEFAULT_REMOVE_TIMEOUT_SECONDS,
    ) -> None:
        self.name = tempfile.mkdtemp(prefix=prefix)
        self._cleanup_timeout = cleanup_timeout
        self._removed = False

    def cleanup(self) -> None:
        if self._removed:
            return
        remove_tree(self.name, timeout=self._cleanup_timeout)
        self._removed = True

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.cleanup()


__all__ = [
    "RetryingTemporaryDirectory",
    "remove_tree",
    "stop_process",
    "wait_for_port_release",
]
