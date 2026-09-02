"""The non-blocking stdout proxy, and specifically its evidence of loss.

🔴 This module had no tests at all, and the one thing it exists to promise --
"when the queue fills, old log segments are discarded" -- had no way to be
observed.  The discard notice was written under `if pending:` on a branch where
`pending` was always None, while the branch that did compute it slept and
continued without writing anything; `_dropped_reported` was advanced either
way.  So the counter was consumed, the notice was unreachable, and a truncated
log was byte-for-byte indistinguishable from a quiet one.

All three resident entrypoints (`sites/api.py`, `sites/activator.py`,
`sites/operator.py`) install this proxy, so the missing line was missing in
production, exactly when the container log consumer was stalled.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from sites.safe_stdout import SafeStdout


SRC = Path(__file__).resolve().parent.parent / "src"


NOTICE = "[safe-stdout] dropped"
DEADLINE_SECONDS = 5.0


class _GatedStream(io.StringIO):
    """A stream whose writes block until ``gate`` is set, and which records them."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.chunks: list[str] = []

    def write(self, text: str) -> int:  # type: ignore[override]
        self.gate.wait(DEADLINE_SECONDS)
        with self.lock:
            self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def text(self) -> str:
        with self.lock:
            return "".join(self.chunks)


def _wait_for(predicate, timeout: float = DEADLINE_SECONDS) -> bool:
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class DropNoticeTests(unittest.TestCase):
    def _overflow(self) -> tuple[_GatedStream, SafeStdout, int]:
        raw = _GatedStream()
        proxy = SafeStdout(raw, max_bytes=64, poll_seconds=0.01)
        self.addCleanup(raw.gate.set)
        for index in range(200):
            proxy.write(f"line-{index:04d}\n")
        dropped = proxy.dropped_segments
        # If nothing was dropped the rest of this class proves nothing, so the
        # precondition is asserted rather than assumed: a future change to
        # max_bytes accounting would surface here instead of turning the two
        # tests below into passes over an empty case.
        self.assertGreater(dropped, 0, "the fixture did not overflow the queue")
        raw.gate.set()
        return raw, proxy, dropped

    def test_a_dropped_segment_leaves_a_trace_in_the_real_stream(self) -> None:
        raw, _proxy, dropped = self._overflow()
        # Goes red if the notice becomes unreachable again -- which is the whole
        # defect -- or if the drain thread stops running at all.
        self.assertTrue(
            _wait_for(lambda: NOTICE in raw.text()),
            f"{dropped} segments were dropped and the stream never said so:\n"
            f"{raw.text()[:400]!r}",
        )

    def test_the_notice_carries_the_number_of_segments_lost(self) -> None:
        raw, _proxy, dropped = self._overflow()
        self.assertTrue(_wait_for(lambda: NOTICE in raw.text()))
        # Goes red on a hardcoded or off-by-one count: "dropped some" is not
        # evidence anyone can act on, and the counter is the only record.
        reported = sum(
            int(line.split(NOTICE, 1)[1].split()[0])
            for line in raw.text().splitlines()
            if NOTICE in line
        )
        self.assertEqual(dropped, reported)

    def test_a_stream_that_keeps_up_is_never_told_about_drops(self) -> None:
        # The other direction.  A notice emitted unconditionally would pass both
        # tests above while making every quiet log claim it lost data.
        raw = _GatedStream()
        raw.gate.set()
        proxy = SafeStdout(raw, max_bytes=1 << 20, poll_seconds=0.01)
        for index in range(20):
            proxy.write(f"line-{index}\n")
        self.assertTrue(_wait_for(lambda: "line-19\n" in raw.text()))
        self.assertEqual(0, proxy.dropped_segments)
        self.assertNotIn(NOTICE, raw.text())

    def test_a_segment_larger_than_the_queue_counts_as_a_drop(self) -> None:
        # The other discard path: write() rejects an oversized segment outright
        # instead of evicting the whole queue for it.  Red if that path ever
        # returns without accounting for the loss.
        raw = _GatedStream()
        raw.gate.set()
        proxy = SafeStdout(raw, max_bytes=32, poll_seconds=0.01)
        proxy.write("x" * 64)
        self.assertEqual(1, proxy.dropped_segments)
        self.assertTrue(_wait_for(lambda: NOTICE in raw.text()))
        self.assertNotIn("x" * 64, raw.text())


class ExitFlushTests(unittest.TestCase):
    """The last line a process writes is the one that says why it is exiting.

    🔴 The drain thread is a daemon, so the interpreter stops it wherever it is
    and drops the queue.  Measured on the real operator entrypoint: a refusal to
    start printed its reason and exited with *completely empty* stdout and
    stderr.  install() now drains at exit.
    """

    SCRIPT = (
        "import sys\n"
        "sys.path.insert(0, {root!r})\n"
        "from sites import safe_stdout\n"
        "{install}\n"
        "print('the-last-thing-this-process-said')\n"
        "raise SystemExit(3)\n"
    )

    def _run(self, install: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", self.SCRIPT.format(root=str(SRC), install=install)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_a_line_written_just_before_exit_reaches_the_real_stream(self) -> None:
        result = self._run("safe_stdout.install()")
        self.assertEqual(3, result.returncode)
        self.assertIn("the-last-thing-this-process-said", result.stdout)

    def test_the_control_shows_the_line_is_not_arriving_by_accident(self) -> None:
        # 🔴 Without the proxy the line obviously arrives, so the case above
        # would pass for the wrong reason if install() silently became a no-op.
        # This pins that the proxy really is installed in the case above.
        result = self._run(
            "safe_stdout.install()\n"
            "assert type(sys.stdout).__name__ == 'SafeStdout', type(sys.stdout)"
        )
        self.assertEqual(3, result.returncode, result.stderr[-500:])

    def test_drain_returns_even_when_the_consumer_never_unblocks(self) -> None:
        # Bounded on purpose: this runs at exit, and a stuck consumer must not
        # turn shutdown into a hang. Red if the bound is removed.
        raw = _GatedStream()  # never gated open
        proxy = SafeStdout(raw, max_bytes=1 << 20, poll_seconds=0.01)
        self.addCleanup(raw.gate.set)
        proxy.write("blocked\n")
        started = time.monotonic()
        proxy.drain(timeout=0.2)
        self.assertLess(time.monotonic() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
