"""Cooperative SIGTERM handling shared by the long-running entry points.

Each entry point (``sites.api``, ``sites.operator``, ``sites.activator``) runs as PID 1
through an exec-form ``command:``. The kernel drops signals whose disposition is the
default for PID 1, and CPython installs no SIGTERM handler, so kubelet's SIGTERM used
to be ignored and every Pod termination waited out ``terminationGracePeriodSeconds``
before SIGKILL. Measured on 2026-08-27 with ``python:3.12-alpine`` as PID 1:
``docker stop -t 8`` took 8.3s and exited 137 without a handler, 0.2s with one.

Usage (must be called from the main thread; ``signal.signal`` refuses other threads)::

    stop = install_stop_handler(server.shutdown)
    server.serve_forever()          # returns once shutdown() is called
    server.server_close()

Callbacks run on a separate daemon thread because ``HTTPServer.shutdown`` blocks until
``serve_forever`` returns, and a signal handler executes on the main thread that may
itself be inside ``serve_forever``.
"""
from __future__ import annotations

import signal
import threading
from typing import Callable

from sites import telemetry

Callback = Callable[[], object]

_HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def install_stop_handler(*callbacks: Callback) -> threading.Event:
    """Register SIGTERM/SIGINT handling and return the stop event.

    The event is set synchronously inside the signal handler, so loops that poll it
    (``event.wait(interval)`` instead of ``time.sleep``) stop at the next check. The
    callbacks then run once, in order, on a background thread; a failing callback is
    logged and does not prevent the remaining ones from running.
    """
    stop = threading.Event()
    lock = threading.Lock()
    fired = False

    def run_callbacks() -> None:
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - shutdown must keep going
                telemetry.log_exception("shutdown_callback_failed", exc)

    def handler(signum: int, _frame: object) -> None:
        nonlocal fired
        stop.set()
        with lock:
            if fired:
                return
            fired = True
        telemetry.log("shutdown_signal", signal=signal.Signals(signum).name)
        threading.Thread(
            target=run_callbacks, name="sites-shutdown", daemon=True
        ).start()

    for signum in _HANDLED_SIGNALS:
        signal.signal(signum, handler)
    return stop
