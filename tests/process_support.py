"""Cross-platform helpers for tests that run a real local server process.

On Windows, a virtual environment's ``python.exe`` is a launcher that runs the
base interpreter as a child process.  ``Popen.wait()`` observes the launcher,
while the child that owns the listening socket and the SQLite file handle can
exit slightly later.  Deleting the database directory immediately can then
fail with a sharing violation (``WinError 32``).  These helpers wait for the
observable effects of shutdown and retry only that class of failure, within a
bounded deadline, instead of ignoring cleanup errors.

Windows also refuses to delete read-only files and directories
(``WinError 5``), which is not transient, so removal clears the read-only
attribute of entries inside the tree being removed instead of retrying.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import warnings
import weakref


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


def _is_within(root: str, path: str) -> bool:
    root = os.path.normcase(os.path.abspath(root))
    path = os.path.normcase(os.path.abspath(path))
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def _clear_read_only(root: str, path: str) -> bool:
    """Make ``path`` and its parent writable if they are read-only entries under ``root``.

    Links and other reparse points are never changed, so a link inside the
    tree cannot be used to change anything outside it.
    """

    changed = False
    for candidate in (path, os.path.dirname(path)):
        if not _is_within(root, candidate):
            continue
        try:
            status = os.lstat(candidate)
        except OSError:
            continue
        if (
            stat.S_ISLNK(status.st_mode)
            or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or status.st_mode & stat.S_IWRITE
        ):
            continue
        os.chmod(candidate, stat.S_IMODE(status.st_mode) | stat.S_IWRITE)
        changed = True
    return changed


def _retry_after_clearing_read_only(
    root: str,
    function: Callable[..., Any],
    path: str,
    error: BaseException,
) -> None:
    """``shutil.rmtree`` error handler for read-only entries inside ``root``.

    Any other failure, including a sharing violation on a writable entry, is
    re-raised unchanged so ``remove_tree`` can apply its bounded retry.
    """

    if (
        function not in (os.unlink, os.remove, os.rmdir)
        or not isinstance(error, PermissionError)
        or not _clear_read_only(root, path)
    ):
        raise error
    function(path)


def _rmtree(path: str) -> None:
    root = os.path.abspath(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(
            path,
            onexc=lambda function, failed, error: _retry_after_clearing_read_only(
                root, function, failed, error
            ),
        )
    else:
        shutil.rmtree(
            path,
            onerror=lambda function, failed, info: _retry_after_clearing_read_only(
                root, function, failed, info[1]
            ),
        )


def remove_tree(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_REMOVE_TIMEOUT_SECONDS,
    remove: Callable[[str], None] = _rmtree,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Remove a directory tree, retrying transient sharing violations.

    Read-only entries inside the tree are made writable and removed on the
    first attempt.  Only ``PermissionError`` (how Windows reports
    ``WinError 32`` and ``WinError 5``) is retried, and only until ``timeout``
    elapses; the last error is then re-raised.  Every other error propagates
    immediately.
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


def _implicit_cleanup(name: str, timeout: float, message: str) -> None:
    warnings.warn(message, ResourceWarning, stacklevel=2)
    remove_tree(name, timeout=timeout)


class RetryingTemporaryDirectory:
    """A ``tempfile.TemporaryDirectory`` replacement with bounded retry cleanup.

    Like ``tempfile.TemporaryDirectory``, a directory that was never cleaned up
    explicitly is removed, with a ``ResourceWarning``, when the object is
    garbage collected or at interpreter exit.  That is only a safety net: a
    ``setUpClass`` that fails never reaches ``tearDownClass``, so callers
    register ``cleanup`` with ``addClassCleanup`` as soon as the directory
    exists.  A failed ``cleanup`` stays armed and may be attempted again.
    """

    def __init__(
        self,
        prefix: str | None = None,
        *,
        cleanup_timeout: float = DEFAULT_REMOVE_TIMEOUT_SECONDS,
    ) -> None:
        self.name = tempfile.mkdtemp(prefix=prefix)
        self._cleanup_timeout = cleanup_timeout
        self._finalizer = weakref.finalize(
            self,
            _implicit_cleanup,
            self.name,
            cleanup_timeout,
            f"Implicitly cleaning up {self!r}",
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"

    def cleanup(self) -> None:
        if not self._finalizer.alive:
            return
        remove_tree(self.name, timeout=self._cleanup_timeout)
        self._finalizer.detach()

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
