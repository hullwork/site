"""Ingest role: reserved for future asynchronous ingestion (webhooks, batch
imports -- out of scope per contract section 0). This slice serves only
``/healthz`` so the chart can stand up an ``hull-broker-ingest`` Deployment now
without that Deployment being able to do anything yet; see docs/BROKER.md.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv

from sites import shutdown, telemetry


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:
        payload = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve() -> None:
    host = getenv("HULL_BROKER_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(getenv("HULL_BROKER_PORT", "8080") or "8080")
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    shutdown.install_stop_handler(server.shutdown)
    telemetry.log("hull_broker_ingest_listening", host=host, port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
