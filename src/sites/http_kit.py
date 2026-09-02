"""Small HTTP utilities shared by the Sites API.

Provides JSON and static-file helpers, bounded request-body reads, route matching, and the
console SPA fallback. The module intentionally has no control-plane business logic.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from sites.validation import SITES_REQUEST_MAX_BYTES, ValidationError
from os import getenv


# Query string parsing failure must be separated from "this parameter was not given": if both return None, the caller will
# A request that has already written 400 is treated as "continue with default values".
QUERY_REFUSED = object()

# Static artifacts of the management console. It is a normal state that it is not included in the image (only the API build is run), so it is missing
# To return 503 instead of 404 - see _serve_console.
CONSOLE_ROOT = getenv("SITES_CONSOLE_ROOT", "/app/console") or "/app/console"
CONSOLE_PREFIX = "/console/"
# Extension whitelist. Unknown extensions are always application/octet-stream, and mimetypes.guess_type is not used.
# Bottom line: That will allow any file imported into the image to select the response type according to the local mime.types, which is equivalent to opening a new window.
# The path returned when the content is of renderable/executable type.
CONSOLE_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".gif": "image/gif",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
CONSOLE_CSP = (
    "default-src 'self'; connect-src 'self'; script-src 'self'; "
    "style-src 'self'; img-src 'self' data:; form-action 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
# The console has the same origin as /v1/*, so no CORS headers are sent or needed here. Adding it will only expose the surface
# Expanding from "same origin" to "a certain source" cannot be exchanged for anything.


class HTTPKitMixin:
    """Response primitives and console static servo; combined by Handler, see module docstring."""

    def _common_security_headers(self) -> None:
        """Apply headers appropriate for every API-generated response."""
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self._common_security_headers()
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            # Readiness clients may disconnect while PostgreSQL is recovering.
            pass

    def _text(self, status: int, payload: str, content_type: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self._common_security_headers()
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(
        self, max_bytes: int = SITES_REQUEST_MAX_BYTES
    ) -> dict[str, Any]:
        def values(name: str) -> list[str]:
            get_all = getattr(self.headers, "get_all", None)
            if get_all is not None:
                return get_all(name, [])
            value = self.headers.get(name, "")
            return [value] if value else []

        if any(
            value.strip() for value in values("Transfer-Encoding")
        ):
            raise ValidationError(
                "request body must use a single Content-Length"
            )
        content_lengths = values("Content-Length")
        if len(content_lengths) != 1:
            raise ValidationError("request body must have one Content-Length")
        try:
            length = int(content_lengths[0])
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if not 0 < length <= max_bytes:
            raise ValidationError(
                "request body must be between 1 byte and "
                f"{max_bytes} bytes"
            )
        try:
            decoded = self.rfile.read(length).decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("request body must be a JSON object")
        return value

    def _route(self) -> tuple[str, dict[str, str]]:
        """Split the request target into path and query.

        Routing always uses self.path to directly compare strings, and the filtering parameters on the admin console (?merchantId=
        etc.) can only use the query string: without splitting, GET /v1/tenants?merchantId=x will fall into 404.
        Repeated parameters are taken as the last one - consistent with most HTTP stacks, and to avoid
        ?limit=1&limit=999 has two interpretations on the server side.
        """
        parsed = urlsplit(self.path)
        query = {
            key: values[-1]
            for key, values in parse_qs(
                parsed.query, keep_blank_values=True
            ).items()
        }
        return parsed.path, query

    def _serve_console(self, path: str) -> None:
        """Serve the bundled admin console. Read-only, same origin, does not send any CORS header."""
        root = os.path.realpath(CONSOLE_ROOT)
        if not os.path.isdir(root):
            # 503 instead of 404. 404 will be read as "Route not configured", but the real reason is that there is no such route in the image.
            # console product - these two troubleshooting directions are completely opposite.
            self._json(
                503,
                {"error": "console assets are not bundled in this image"},
            )
            return
        try:
            relative = unquote(path[len(CONSOLE_PREFIX):], errors="strict")
        except UnicodeDecodeError:
            self._json(400, {"error": "invalid path encoding"})
            return
        if "\x00" in relative:
            self._json(400, {"error": "invalid path"})
            return
        candidate = os.path.realpath(os.path.join(root, relative))
        # Realpath is then asserted: `..`, absolute paths, and symbolic links pointing outside root are all there
        # This is the step where it becomes apparent - only `..` filtering at the string level cannot block soft links.
        if candidate != root and not candidate.startswith(root + os.sep):
            self._json(403, {"error": "forbidden"})
            return
        if not os.path.isfile(candidate):
            # SPA routing: Any /console/* that does not fall on the file will return index.html, letting the frontend
            # The router takes over. The traversal request has been blocked by 403 above and will not go here.
            candidate = os.path.join(root, "index.html")
            if not os.path.isfile(candidate):
                self._json(
                    503,
                    {"error": "console assets are not bundled in this image"},
                )
                return
        try:
            with open(candidate, "rb") as handle:
                body = handle.read()
        except OSError:
            self._json(503, {"error": "console asset is unreadable"})
            return
        extension = os.path.splitext(candidate)[1].lower()
        # All octet-stream outside the whitelist: the browser will not render it, so there is no "unknown extension"
        # Returns "this path" as an executable type.
        content_type = CONSOLE_CONTENT_TYPES.get(
            extension, "application/octet-stream"
        )
        self._static(200, body, content_type)

    def _static(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        cache_control = (
            "no-store"
            if content_type.startswith("text/html;")
            else "public, max-age=31536000, immutable"
        )
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", CONSOLE_CSP)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
