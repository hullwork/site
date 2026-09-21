"""The Broker gateway: stdlib ``ThreadingHTTPServer`` bridging real sockets to the
transport-neutral ``Router``/``Request``/``Response`` types in ``http.py``.

Kept deliberately close to ``sites.api``'s shape (same per-connection socket
timeout rationale, same daemon_threads=False so shutdown joins in-flight
requests) because that shape is already reviewed and battle-tested in this repo;
diverging from it here would need its own justification and this module has none
to offer.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sites import shutdown, telemetry, tracing
from sites.broker import identity
from sites.broker import tokens as broker_tokens
from sites.broker.http import (
    HttpError,
    Request,
    Response,
    Router,
    Services,
    StreamResponse,
)

# Same default as SITES_REQUEST_MAX_BYTES / SITES_ACTIVATOR_MAX_INFLIGHT's peers:
# generous for a JSON API body, small enough that a client cannot park a thread's
# memory on an unbounded read.
_DEFAULT_MAX_BODY_BYTES = 65536
_DEFAULT_MAX_INFLIGHT = 64
_MAX_COOKIE_HEADER_BYTES = 8192


def max_body_bytes() -> int:
    return int(getenv("HULL_MAX_BODY_BYTES", str(_DEFAULT_MAX_BODY_BYTES))
               or str(_DEFAULT_MAX_BODY_BYTES))


def max_inflight() -> int:
    # Same default as SITES_ACTIVATOR_MAX_INFLIGHT by contract (section C7): the
    # two gateways sit at comparable places in the request path.
    return int(getenv("HULL_MAX_INFLIGHT", str(_DEFAULT_MAX_INFLIGHT))
               or str(_DEFAULT_MAX_INFLIGHT))


def _bearer_token(headers: Any) -> str | None:
    value = headers.get("Authorization", "") or ""
    if not value.startswith("Bearer "):
        return None
    token = value[len("Bearer "):].strip()
    return token or None


def _cookies(headers: Any) -> dict[str, str]:
    raw = headers.get("Cookie", "") or ""
    if not raw or len(raw) > _MAX_COOKIE_HEADER_BYTES:
        return {}
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except (CookieError, ValueError):
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


class _GatewayServer(ThreadingHTTPServer):
    # ThreadingHTTPServer defaults to daemon request threads, which server_close()
    # does not join -- shutdown would return before in-flight requests finish
    # writing. Non-daemon threads are joined by server_close(); the wait stays
    # bounded because every handler's own DB calls carry a statement timeout.
    daemon_threads = False


def build_handler(router: Router, services: Services) -> type[BaseHTTPRequestHandler]:
    """A fresh Handler subclass per call, so tests can stand up independent
    servers without one test's router leaking into another's class attributes.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hull-broker"
        sys_version = ""
        # Same rationale as sites.api.Handler.timeout: without it, a client that
        # declares Content-Length and never sends the body parks the thread
        # forever, and nothing here yet limits total connections without it.
        timeout = 30

        inflight = threading.BoundedSemaphore(max_inflight())

        def version_string(self) -> str:
            return self.server_version

        def log_message(self, *args: Any) -> None:  # structured logs replace this
            return

        def send_response(self, code: int, message: str | None = None) -> None:
            self._status = code
            super().send_response(code, message)

        def parse_request(self) -> bool:
            parsed = super().parse_request()
            if parsed:
                self._trace_id, self._trace_flags = tracing.inbound_context(self.headers)
            return parsed

        def handle_one_request(self) -> None:
            self._status = 0
            trace_id = getattr(self, "_trace_id", None) or tracing.new_trace_id()
            trace_flags = getattr(self, "_trace_flags", tracing.current_flags())
            token = tracing.bind(trace_id, trace_flags)
            started = time.monotonic()
            try:
                super().handle_one_request()
            finally:
                tracing.release(token)
                telemetry.log(
                    "hull_broker_http_access", level="debug",
                    method=getattr(self, "command", ""), status=self._status,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        # -- dispatch -----------------------------------------------------------

        def _dispatch(self) -> None:
            path, _ = self._split_path()
            if path == "/healthz":
                self._json(200, {"status": "ok"})
                return
            if path == "/readyz":
                self._readyz()
                return
            if not self.inflight.acquire(blocking=False):
                self._json(503, {"error": "the gateway is at its concurrency limit",
                                  "code": "overloaded"})
                return
            try:
                self._handle()
            finally:
                self.inflight.release()

        def _handle(self) -> None:
            path, query = self._split_path()
            try:
                match = router.match(self.command, path)
            except HttpError as exc:
                self._error(exc)
                return
            if match is None:
                self._error(HttpError(404, "route_not_found", "no route matches this path"))
                return
            route, params = match
            try:
                body = self._maybe_read_body()
                request = Request(
                    method=self.command, path=path, params=params, query=query,
                    headers=self.headers, body=body,
                    host=(self.headers.get("Host") or "").split(":")[0],
                    client_ip=self.client_address[0], cookies=_cookies(self.headers),
                )
                request = self._authenticate(request, route.auth)
            except HttpError as exc:
                self._error(exc)
                return
            try:
                result = route.handler(request)
            except HttpError as exc:
                self._error(exc)
                return
            except Exception as exc:  # noqa: BLE001 - last resort, must still answer
                telemetry.log_exception("hull_broker_handler_failed", exc)
                self._error(HttpError(500, "internal_error",
                                       "the request could not be completed"))
                return
            if isinstance(result, StreamResponse):
                self._write_stream(result)
            else:
                self._write_response(result)

        def _authenticate(self, request: Request, mode: str) -> Request:
            if mode == "none":
                return request
            if mode == "admin":
                token = _bearer_token(request.headers)
                admin_token = services.admin_token
                if not admin_token or not token or not hmac.compare_digest(token, admin_token):
                    raise HttpError(401, "invalid_admin_token",
                                     "the admin token is missing or does not match")
                return request
            request.app = self._resolve_app(request)
            request.runtime = services.config.active(request.app.app_env_id)
            if mode == "app":
                return request
            user_token = request.headers.get("X-Hull-User")
            if mode == "app+user" and not user_token:
                raise HttpError(401, "user_token_required",
                                 "this route requires X-Hull-User")
            if user_token:
                try:
                    request.user = identity.verify_user_token(
                        services.keys, user_token, app_env_id=request.app.app_env_id
                    )
                except broker_tokens.TokenError as exc:
                    raise HttpError(401, "invalid_user_token",
                                     "the X-Hull-User token does not verify") from exc
            return request

        def _resolve_app(self, request: Request) -> identity.AppCaller:
            token = _bearer_token(request.headers)
            if token:
                try:
                    app = identity.verify_app_token(services.keys, token)
                except broker_tokens.TokenError as exc:
                    raise HttpError(401, "invalid_app_token",
                                     "the app token does not verify") from exc
                host_header = request.headers.get("X-Hull-Host")
                if host_header:
                    owner = services.config.by_host(host_header)
                    if owner != app.app_env_id:
                        raise HttpError(403, "host_mismatch",
                                         "X-Hull-Host does not belong to this app token")
                return app
            app_env_id = services.config.by_host(request.host)
            if app_env_id is None:
                raise HttpError(404, "unknown_app", "no app is configured for this host")
            return identity.AppCaller(app_env_id=app_env_id, site=None)

        # -- body / query ---------------------------------------------------------

        def _split_path(self) -> tuple[str, dict[str, str]]:
            parsed = urlsplit(self.path)
            query = {key: values[-1] for key, values in
                     parse_qs(parsed.query, keep_blank_values=True).items()}
            return parsed.path, query

        def _maybe_read_body(self) -> Any:
            get_all = getattr(self.headers, "get_all", None)
            lengths = get_all("Content-Length", []) if get_all else (
                [self.headers["Content-Length"]] if "Content-Length" in self.headers else []
            )
            if not lengths:
                return None
            if (self.headers.get("Transfer-Encoding") or "").strip():
                raise HttpError(400, "invalid_body",
                                 "chunked request bodies are not supported")
            if len(lengths) != 1:
                raise HttpError(400, "invalid_body",
                                 "request body must have one Content-Length")
            try:
                length = int(lengths[0])
            except ValueError as exc:
                raise HttpError(400, "invalid_body", "Content-Length is invalid") from exc
            limit = max_body_bytes()
            if length < 0 or length > limit:
                raise HttpError(413, "body_too_large",
                                 f"request body must not exceed {limit} bytes")
            if length == 0:
                return None
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HttpError(400, "invalid_json",
                                 "request body must be valid UTF-8 JSON") from exc
            if not isinstance(value, dict):
                raise HttpError(400, "invalid_json", "request body must be a JSON object")
            return value

        # -- responses ------------------------------------------------------------

        def _error(self, exc: HttpError) -> None:
            self._json(exc.status, exc.body())

        def _write_response(self, response: Response) -> None:
            if response.status == 204:
                self.send_response(204)
                for key, value in (response.headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json(response.status, response.body if response.body is not None else {},
                       response.headers)

        def _json(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            headers.update(extra_headers or {})
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _write_stream(self, stream: StreamResponse) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Transfer-Encoding", "chunked")
            for key, value in (stream.headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                for event_name, data in stream.events:
                    chunk = (
                        f"event: {event_name}\ndata: "
                        f"{json.dumps(data, ensure_ascii=False)}\n\n"
                    ).encode("utf-8")
                    self._write_chunk(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                closer = getattr(stream.events, "close", None)
                if callable(closer):
                    closer()
                try:
                    self._write_chunk(b"")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _write_chunk(self, data: bytes) -> None:
            size = format(len(data), "x").encode("ascii")
            self.wfile.write(size + b"\r\n" + data + b"\r\n")
            self.wfile.flush()

        def _readyz(self) -> None:
            # Same criterion as sites-api's /readyz: only the database can turn
            # this red. Everything else the gateway depends on (K8s TokenReview,
            # the mailer) is on a specific request's path, not every request's.
            try:
                with services.db.transaction() as cursor:
                    cursor.execute("SELECT 1")
            except Exception as exc:  # noqa: BLE001 - readiness must not raise
                telemetry.log_exception("hull_broker_readyz_database_unavailable", exc)
                self._json(503, {"status": "not_ready", "error": "database"})
                return
            self._json(200, {"status": "ready"})

    return Handler


def listen(host: str, port: int, router: Router, services: Services) -> ThreadingHTTPServer:
    """Bind the gateway socket and wire SIGTERM/SIGINT to a graceful stop. Split
    from serve() so the wiring is testable; must run on the main thread.
    """
    handler = build_handler(router, services)
    server = _GatewayServer((host, port), handler)
    shutdown.install_stop_handler(server.shutdown)
    return server


def build_router(services: Services) -> Router:
    """Wire every route module. Runtime routes (D) are optional: this branch's
    own tests must pass before D's module exists, so a missing
    ``sites.broker.runtime_api`` is not an error here -- only a missing
    *unrelated* module further down its own import chain is.
    """
    from sites.broker import admin_api, auth, identity as identity_module

    router = Router()
    identity_module.register(router, services)
    auth.register(router, services)
    admin_api.register(router, services)
    try:
        from sites.broker.runtime_api import register as register_runtime
    except ModuleNotFoundError as exc:
        if exc.name != "sites.broker.runtime_api":
            raise
        telemetry.log("hull_broker_runtime_api_absent",
                       detail="sites.broker.runtime_api is not present on this build")
    else:
        register_runtime(router, services)
    return router
