"""Non-blocking stdout/stderr for long-running Sites processes.

Writes enter a byte-bounded in-memory queue. A single daemon thread drains the
queue to the real stream, so a stalled container log consumer cannot freeze an
API or reconcile thread. When the queue fills, old log segments are discarded.
"""
from __future__ import annotations

import atexit
import sys
import threading
import time
from collections import deque
from typing import Any, TextIO


class SafeStdout:
    """A ``TextIO`` proxy whose ``write`` and ``flush`` never block on an fd."""

    def __init__(
        self,
        raw: TextIO,
        *,
        max_bytes: int = 1 << 20,
        poll_seconds: float = 0.05,
    ) -> None:
        self._raw = raw
        self._max_bytes = max_bytes
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, int]] = deque()
        self._queued_bytes = 0
        self._dropped_segments = 0
        self._dropped_reported = 0
        self._closed = False
        self._drain = threading.Thread(
            target=self._drain_loop, name="safe-stdout", daemon=True
        )
        self._drain.start()

    def write(self, text: Any) -> int:
        if not text:
            return 0
        data = str(text)
        size = len(data.encode("utf-8", "replace"))
        with self._lock:
            if self._closed:
                return len(data)
            if size > self._max_bytes:
                self._dropped_segments += 1
                return len(data)
            while self._queued_bytes + size > self._max_bytes and self._queue:
                _, dropped_size = self._queue.popleft()
                self._queued_bytes -= dropped_size
                self._dropped_segments += 1
            self._queue.append((data, size))
            self._queued_bytes += size
        return len(data)

    def flush(self) -> None:
        return None

    @property
    def dropped_segments(self) -> int:
        with self._lock:
            return self._dropped_segments

    def drain(self, timeout: float = 2.0) -> None:
        """Block until the queue is empty, then flush the real stream.

        🔴 The drain thread is a daemon, so on exit the interpreter stops it
        wherever it is and everything still queued is gone. That silently ate
        the last thing a process ever writes, which is the line explaining why
        it is exiting -- measured: an operator refusing to start on a
        contradicted Pod CIDR printed the reason to a queue nobody drained and
        died with completely empty stdout and stderr. A fatal error you cannot
        read is not much better than no error.

        Bounded rather than unbounded: this runs at exit, and a consumer that
        is still blocked must not turn a shutdown into a hang.
        """
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            with self._lock:
                drained = (
                    self._closed
                    or (not self._queue and self._dropped_reported == self._dropped_segments)
                )
            if drained:
                break
            time.sleep(self._poll_seconds)
        try:
            self._raw.flush()
        except (OSError, ValueError):
            return

    def _drain_loop(self) -> None:
        while True:
            with self._lock:
                if self._queue:
                    text, size = self._queue.popleft()
                    self._queued_bytes -= size
                else:
                    text = None
                # Read the drop counter on every pass, not only on idle ones.
                # It used to be read in the else-branch alone, which is the
                # branch that leaves text as None -- and the None case sleeps
                # and continues below, so the notice this counter exists for
                # could never be written, while _dropped_reported was advanced
                # anyway. Truncated logs then looked exactly like quiet ones.
                pending = self._dropped_segments - self._dropped_reported
                if pending:
                    self._dropped_reported = self._dropped_segments
            if text is None and not pending:
                time.sleep(self._poll_seconds)
                continue
            try:
                if text is not None:
                    self._raw.write(text)
                if pending:
                    self._raw.write(
                        f"[safe-stdout] dropped {pending} log segments while the "
                        "consumer was blocked\n"
                    )
                self._raw.flush()
            except (OSError, ValueError):
                with self._lock:
                    self._closed = True
                return

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


def install() -> SafeStdout | None:
    """Install non-blocking stdout/stderr proxies; safe to call repeatedly.

    Each proxy created here is drained at interpreter exit. Without that the
    daemon drain thread is simply stopped and whatever is still queued is lost,
    which is worst for the message that matters most: the one a process writes
    immediately before it exits.
    """
    marker = "_site_safe_stdout"
    stdout: SafeStdout | None = None
    if sys.stdout is not None and getattr(sys.stdout, marker, False):
        stdout = sys.stdout  # type: ignore[assignment]
    elif sys.stdout is not None and hasattr(sys.stdout, "write"):
        stdout = SafeStdout(sys.stdout)
        setattr(stdout, marker, True)
        sys.stdout = stdout  # type: ignore[assignment]
        atexit.register(stdout.drain)
    if sys.stderr is not None and not getattr(sys.stderr, marker, False):
        if hasattr(sys.stderr, "write"):
            stderr = SafeStdout(sys.stderr)
            setattr(stderr, marker, True)
            sys.stderr = stderr  # type: ignore[assignment]
            atexit.register(stderr.drain)
    return stdout
