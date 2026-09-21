"""Background role: run migrations once, then periodically delete expired
sessions, OTP codes, and rate-limit buckets. Serves ``/healthz`` so the
Deployment's probe has something to check -- readiness is "the last cleanup
cycle completed within N seconds", not "the process is alive", because a wedged
cleanup loop is exactly the failure this probe exists to catch.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv

from sites import shutdown, telemetry
from sites.broker import db as broker_db


def cleanup_interval_seconds() -> int:
    return int(getenv("HULL_BROKER_WORKER_INTERVAL_SECONDS", "60") or "60")


def stale_after_seconds() -> int:
    # Three missed cycles before health turns red -- the same "one miss is
    # noise, several is stuck" shape as SITES_OPERATOR_HEALTH_STALE_SWEEPS.
    default = cleanup_interval_seconds() * 3
    return int(getenv("HULL_BROKER_WORKER_STALE_SECONDS", str(default)) or str(default))


_state_lock = threading.Lock()
_last_cycle_monotonic: float | None = None


def _run_cycle(db: broker_db.BrokerDatabase) -> None:
    global _last_cycle_monotonic
    now = datetime.now(timezone.utc)
    with db.transaction() as cursor:
        sessions = broker_db.delete_expired_sessions(cursor, now)
        otps = broker_db.delete_expired_otps(cursor, now)
        # A rate-limit bucket outlives the window it gates well before this: one
        # day is generous, and nothing reads a bucket once its own window passed.
        buckets = broker_db.delete_expired_rate_limits(cursor, now - timedelta(days=1))
    telemetry.log("hull_broker_worker_cycle", sessions=sessions, otps=otps, buckets=buckets)
    with _state_lock:
        _last_cycle_monotonic = time.monotonic()


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        with _state_lock:
            last = _last_cycle_monotonic
        healthy = last is not None and (time.monotonic() - last) < stale_after_seconds()
        payload = b'{"status":"ok"}' if healthy else b'{"status":"stale"}'
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve() -> None:
    db = broker_db.BrokerDatabase.from_env()
    broker_db.migrate(db)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                _run_cycle(db)
            except Exception as exc:  # noqa: BLE001 - the loop must keep running
                telemetry.log_exception("hull_broker_worker_cycle_failed", exc)
            stop.wait(cleanup_interval_seconds())

    threading.Thread(target=loop, name="hull-broker-worker", daemon=True).start()
    host = getenv("HULL_BROKER_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(getenv("HULL_BROKER_PORT", "8080") or "8080")
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    shutdown.install_stop_handler(server.shutdown, stop.set)
    telemetry.log("hull_broker_worker_listening", host=host, port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
