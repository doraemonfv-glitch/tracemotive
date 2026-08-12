"""The TraceMotive v0.1 bounded local transport.

The transport boundary accepts only already-created canonical event envelopes.
It serializes those envelopes before queue insertion so the queue never owns
mutable framework objects or unsanitized source values.  Delivery is kept on a
daemon worker and all transport failures are terminal to telemetry only.
"""

from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass
import http.client
import ipaddress
import json
import socket
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .canonical.models import _canonical_json_dumps


DEFAULT_ENDPOINT = "http://127.0.0.1:8765"
QUEUE_CAPACITY = 2048
MAX_BATCH_EVENTS = 64
MAX_EVENT_BYTES = 1_048_576
MAX_REQUEST_BYTES = 4_194_304
SEND_INTERVAL_SECONDS = 0.250
ATTEMPT_TIMEOUT_SECONDS = 1.0
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.100, 0.250)
SHUTDOWN_BUDGET_SECONDS = 2.0
INGEST_PATH = "/api/v1/ingest"

# These aliases make the Frozen values easy to discover without introducing a
# second set of independently configurable constants.
QUEUE_SIZE = QUEUE_CAPACITY
BATCH_MAX_EVENTS = MAX_BATCH_EVENTS
BATCH_MAX_BYTES = MAX_REQUEST_BYTES

_RETRYABLE_STATUS_CODES = frozenset({408, 429})
_REQUEST_PREFIX = b'{"events":['
_REQUEST_SUFFIX = b'],"protocol_version":1}'


class _InvalidCollectorResponse(Exception):
    """Raised when a 200 response does not satisfy the Issue 04 contract."""


def validate_loopback_endpoint(endpoint: Any) -> str:
    """Validate the Frozen HTTP loopback endpoint without DNS resolution."""

    if not isinstance(endpoint, str):
        raise ValueError("endpoint must be a string")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        parsed.port  # Validate malformed or out-of-range ports.
    except ValueError as exc:
        raise ValueError("endpoint must be a loopback HTTP URL") from exc

    if parsed.scheme.casefold() != "http" or hostname is None:
        raise ValueError("endpoint must be a loopback HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint must not contain credentials")
    if hostname.casefold() == "localhost":
        return endpoint
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError(
            "endpoint host must be localhost or a loopback IP address"
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "endpoint host must be localhost or a loopback IP address"
        )
    return endpoint


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    """An accepted event and its internal, non-persistent FIFO position."""

    sequence: int
    serialized: bytes


class Transport:
    """A bounded, asynchronous, loopback-only TraceMotive transport."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        start: bool = True,
        sleeper: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoint = validate_loopback_endpoint(endpoint)
        self._sleeper = sleeper
        self._clock = clock
        self._condition = threading.Condition()
        self._queue: deque[_QueuedEvent] = deque()
        self._pending: set[int] = set()
        self._next_sequence = 1
        self._accepting = True
        self._state = "new"
        self._worker: threading.Thread | None = None
        self._shutdown_deadline: float | None = None
        self._shutdown_cutoff: int | None = None
        self._start_error: BaseException | None = None
        self._flush_cutoff_hook: Callable[[int], Any] | None = None
        atexit.register(self.shutdown)
        if start:
            self.start()

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    @property
    def queue_size(self) -> int:
        with self._condition:
            return len(self._queue)

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    @property
    def shutdown_cutoff(self) -> int | None:
        with self._condition:
            return self._shutdown_cutoff

    def start(self) -> bool:
        """Start the worker once; startup failures never escape user code."""

        with self._condition:
            if self._state != "new":
                return self._state == "running"
            if not self._accepting:
                self._state = "stopped"
                return False
            self._state = "running"
            worker = threading.Thread(
                target=self._worker_main,
                name="tracemotive-transport",
                daemon=True,
            )
            self._worker = worker
            try:
                worker.start()
            except BaseException as exc:
                self._start_error = exc
                self._state = "stopped"
                self._accepting = False
                self._drop_all_locked()
                return False
            return True

    def emit(self, event: Any) -> bool:
        """Enqueue one canonical envelope, returning its atomic outcome."""

        # Serialization is deliberately before queue ownership.  This both
        # rejects raw values and makes queued content immutable and sanitized
        # as supplied by the SDK boundary.
        if type(event) is not dict:
            return False
        try:
            serialized = _canonical_json_dumps(event).encode("utf-8")
        except BaseException:
            return False
        if len(serialized) > MAX_EVENT_BYTES:
            return False

        with self._condition:
            if not self._accepting:
                return False
            if len(self._queue) >= QUEUE_CAPACITY:
                # Frozen overflow is drop-newest.  Existing entries and their
                # accepted FIFO positions remain untouched.
                return False
            sequence = self._next_sequence
            self._next_sequence += 1
            self._queue.append(_QueuedEvent(sequence, serialized))
            self._pending.add(sequence)
            self._condition.notify()
            return True

    enqueue = emit

    def flush(self, timeout_seconds: float = SHUTDOWN_BUDGET_SECONDS) -> bool:
        """Wait for the accepted FIFO prefix present at one atomic cutoff."""

        try:
            timeout = max(0.0, float(timeout_seconds))
        except (TypeError, ValueError, OverflowError):
            return False
        deadline = self._clock() + timeout
        with self._condition:
            cutoff = self._next_sequence - 1
            hook = self._flush_cutoff_hook
            if hook is not None:
                try:
                    hook(cutoff)
                except BaseException:
                    pass
            while any(sequence <= cutoff for sequence in self._pending):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def shutdown(self, timeout_seconds: float = SHUTDOWN_BUDGET_SECONDS) -> bool:
        """Close acceptance and drain the final accepted prefix for two seconds."""

        try:
            budget = max(0.0, float(timeout_seconds))
        except (TypeError, ValueError, OverflowError):
            budget = SHUTDOWN_BUDGET_SECONDS

        with self._condition:
            if self._state == "stopped":
                return True
            if self._shutdown_deadline is None:
                self._shutdown_cutoff = self._next_sequence - 1
                self._shutdown_deadline = self._clock() + budget
                self._accepting = False
                self._state = "stopping"
            worker = self._worker
            if worker is None:
                self._drop_all_locked()
                self._state = "stopped"
                self._condition.notify_all()
                return True
            self._condition.notify_all()

        if worker is threading.current_thread():
            return False
        deadline = self._shutdown_deadline
        remaining = max(0.0, (deadline - self._clock()) if deadline is not None else budget)
        try:
            worker.join(timeout=remaining)
            worker_alive = worker.is_alive()
        except BaseException:
            worker_alive = True
        if worker_alive:
            with self._condition:
                self._drop_all_locked()
                self._state = "stopped"
                self._condition.notify_all()
            return False
        return True

    close = shutdown

    def _worker_main(self) -> None:
        try:
            while True:
                with self._condition:
                    batch = self._claim_batch_locked()
                    if batch is None:
                        if self._state == "stopped":
                            self._condition.notify_all()
                            return
                        if self._state == "stopping":
                            if not self._pending:
                                self._state = "stopped"
                                self._condition.notify_all()
                                return
                            deadline = self._shutdown_deadline
                            remaining = (
                                (deadline - self._clock())
                                if deadline is not None
                                else 0.0
                            )
                            if remaining <= 0:
                                self._drop_all_locked()
                                self._state = "stopped"
                                self._condition.notify_all()
                                return
                            self._condition.wait(
                                timeout=min(remaining, SEND_INTERVAL_SECONDS)
                            )
                        else:
                            self._condition.wait(timeout=SEND_INTERVAL_SECONDS)
                        continue
                try:
                    self._deliver_batch(*batch)
                except BaseException:
                    # A worker bug, response parser failure, or test-injected
                    # client failure must not terminate the host application.
                    self._complete_batch(batch[0])
        finally:
            with self._condition:
                self._drop_all_locked()
                self._accepting = False
                self._state = "stopped"
                self._condition.notify_all()

    def _claim_batch_locked(self) -> tuple[list[_QueuedEvent], bytes] | None:
        if not self._queue:
            return None

        selected: list[_QueuedEvent] = []
        while self._queue and len(selected) < MAX_BATCH_EVENTS:
            candidate = self._queue[0]
            candidate_body = self._batch_body([*selected, candidate])
            if len(candidate_body) > MAX_REQUEST_BYTES:
                if selected:
                    break
                # No valid request can be built for this leading event.  It
                # must reach a terminal local drop so later FIFO entries can
                # make progress; no event is truncated or mutated.
                self._queue.popleft()
                self._pending.discard(candidate.sequence)
                self._condition.notify_all()
                continue
            selected.append(self._queue.popleft())

        if not selected:
            return None
        return selected, self._batch_body(selected)

    @staticmethod
    def _batch_body(events: list[_QueuedEvent]) -> bytes:
        return _REQUEST_PREFIX + b",".join(
            event.serialized for event in events
        ) + _REQUEST_SUFFIX

    def _deliver_batch(self, events: list[_QueuedEvent], body: bytes) -> None:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            timeout = self._attempt_timeout()
            if timeout <= 0:
                self._complete_batch(events)
                return
            retryable = False
            try:
                status = self._send_attempt(body, timeout)
                if type(status) is not int:
                    raise _InvalidCollectorResponse()
                if status == 200:
                    retryable = False
                elif status in _RETRYABLE_STATUS_CODES or 500 <= status <= 599:
                    retryable = True
                else:
                    # Includes all Frozen terminal 4xx responses and any
                    # non-success status for which no retry is specified.
                    retryable = False
            except BaseException:
                retryable = True

            if not retryable or attempt == MAX_ATTEMPTS:
                self._complete_batch(events)
                return
            if not self._wait_backoff(RETRY_BACKOFF_SECONDS[attempt - 1]):
                self._complete_batch(events)
                return

    def _attempt_timeout(self) -> float:
        with self._condition:
            deadline = self._shutdown_deadline
        if deadline is None:
            return ATTEMPT_TIMEOUT_SECONDS
        return min(ATTEMPT_TIMEOUT_SECONDS, max(0.0, deadline - self._clock()))

    def _wait_backoff(self, delay: float) -> bool:
        with self._condition:
            deadline = self._shutdown_deadline
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - self._clock()))
        if delay <= 0:
            return False
        try:
            self._sleeper(delay)
        except BaseException:
            return False
        if deadline is not None and self._clock() >= deadline:
            return False
        return True

    def _complete_batch(self, events: list[_QueuedEvent] | _QueuedEvent) -> None:
        if isinstance(events, _QueuedEvent):
            completed = (events,)
        else:
            completed = events
        with self._condition:
            for event in completed:
                self._pending.discard(event.sequence)
            self._condition.notify_all()

    def _drop_all_locked(self) -> None:
        self._queue.clear()
        self._pending.clear()

    def _send_attempt(self, body: bytes, timeout_seconds: float) -> int:
        """POST one request without proxies or redirect handling."""

        parsed = urlsplit(self.endpoint)
        hostname = parsed.hostname
        if hostname is None:  # Defensive; construction already validates it.
            raise _InvalidCollectorResponse()
        port = parsed.port or 80
        connection = http.client.HTTPConnection(
            hostname,
            port,
            timeout=timeout_seconds,
        )
        deadline = self._clock() + timeout_seconds
        response = None
        try:
            connection.request(
                "POST",
                INGEST_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                },
            )
            self._set_socket_timeout(connection, deadline)
            response = connection.getresponse()
            status = response.status
            if status != 200:
                return status
            self._set_socket_timeout(connection, deadline)
            response_body = response.read()
            self._validate_success_response(response_body)
            return status
        finally:
            if response is not None:
                try:
                    response.close()
                except BaseException:
                    pass
            try:
                connection.close()
            except BaseException:
                pass

    def _set_socket_timeout(
        self,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise socket.timeout("TraceMotive transport attempt timed out")
        sock = getattr(connection, "sock", None)
        if sock is not None:
            sock.settimeout(remaining)

    @staticmethod
    def _validate_success_response(response_body: bytes) -> None:
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _InvalidCollectorResponse() from exc
        if type(decoded) is not dict or set(decoded) != {"accepted", "duplicates", "stale"}:
            raise _InvalidCollectorResponse()
        for field in ("accepted", "duplicates", "stale"):
            value = decoded[field]
            if type(value) is not int or value < 0:
                raise _InvalidCollectorResponse()


LocalTransport = Transport


__all__ = [
    "ATTEMPT_TIMEOUT_SECONDS",
    "BATCH_MAX_BYTES",
    "BATCH_MAX_EVENTS",
    "DEFAULT_ENDPOINT",
    "INGEST_PATH",
    "LocalTransport",
    "MAX_ATTEMPTS",
    "MAX_BATCH_EVENTS",
    "MAX_EVENT_BYTES",
    "MAX_REQUEST_BYTES",
    "QUEUE_CAPACITY",
    "QUEUE_SIZE",
    "RETRY_BACKOFF_SECONDS",
    "SEND_INTERVAL_SECONDS",
    "SHUTDOWN_BUDGET_SECONDS",
    "Transport",
    "validate_loopback_endpoint",
]
