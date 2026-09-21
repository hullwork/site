"""MCP (streamable-HTTP JSON-RPC) client for capability tool calls.

The outbound target is a URL a tenant put in their own compiled config
(``capabilities[].url``, contract §3.6), so unlike ``llm.py``'s fixed, operator-set
upstream, this one needs the same SSRF defense the agent gateway already has for
BYOK upstreams. The address-classification, resolve-and-pin, and pinned-connection
pieces below are a direct port of ``hullwork/agent`` ``gateway/gateway.py``
``_is_public_address`` / ``byok_resolve_public`` / ``_PinnedHTTPSConnection``
(~L741-900 there): same predicate, same "resolve once, connect to what was
resolved" defense against DNS rebinding, so re-verifying the invariant here would
just be re-deriving the docstring that already explains it — see that module for
why each clause exists.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_UNSAFE_TARGET = re.compile(r"[\x00-\x20\x7f\\]")
_PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    """Base class for MCP call failures."""


class SSRFRejected(MCPError):
    """The capability URL does not resolve to an address this Broker may reach."""


class MCPUnavailable(MCPError):
    """The MCP server could not be reached or returned an invalid JSON-RPC frame."""


class NotConnected(MCPError):
    """``credential: end_user`` capability with no end-user connection in this slice."""


# -- SSRF defense, ported from hullwork/agent gateway/gateway.py --------------------

# Process-local, test-only escape hatch for loopback addresses. There is no settings
# key, env var, or config field that reaches this: the only way to flip it is to
# import this private context manager from a test module, which a deployed app's
# settings can never do.
_test_state = threading.local()


class _allow_loopback_for_tests:
    """``with mcp_client._allow_loopback_for_tests(): ...`` — tests only."""

    def __enter__(self) -> None:
        self._previous = getattr(_test_state, "enabled", False)
        _test_state.enabled = True

    def __exit__(self, *exc: Any) -> None:
        _test_state.enabled = self._previous


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    address = getattr(address, "ipv4_mapped", None) or address
    if getattr(_test_state, "enabled", False) and address.is_loopback:
        return True
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _validate_capability_url(raw_url: str):
    if not raw_url or _UNSAFE_TARGET.search(raw_url):
        raise SSRFRejected("capability url is malformed")
    try:
        base = urlsplit(raw_url)
        _ = base.port
    except ValueError:
        raise SSRFRejected("capability url is malformed") from None
    allow_http = getattr(_test_state, "enabled", False)
    if base.scheme != "https" and not (allow_http and base.scheme == "http"):
        raise SSRFRejected("capability url must use https")
    if (
        not base.hostname
        or base.username is not None
        or base.password is not None
        or bool(base.query)
        or bool(base.fragment)
        or "%" in base.hostname
    ):
        raise SSRFRejected(
            "capability url must be https://host[:port][/path] without userinfo, query or fragment"
        )
    return base


def _resolve_public(hostname: str, port: int) -> list[tuple]:
    try:
        answers = socket.getaddrinfo(hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise SSRFRejected(f"capability host does not resolve: {error.strerror or error}") from None
    if not answers:
        raise SSRFRejected("capability host resolved to no addresses")
    for answer in answers:
        raw = str(answer[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise SSRFRejected("capability host resolved to an unusable address") from None
        if not _is_public_address(address):
            raise SSRFRejected(f"capability host resolves to a non-public address ({address})")
    return answers


def _connect_to_addrinfos(addrinfos, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _, sockaddr in addrinfos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            last_error = error
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("no capability address to connect to")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, addrinfos, timeout, context) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_addrinfos = tuple(addrinfos)
        self._create_connection = self._connect_pinned

    def _connect_pinned(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None) -> socket.socket:
        del address
        return _connect_to_addrinfos(self._pinned_addrinfos, timeout, source_address)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plaintext counterpart, reachable only through the loopback test override."""

    def __init__(self, host, port, *, addrinfos, timeout) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_addrinfos = tuple(addrinfos)
        self._create_connection = self._connect_pinned

    def _connect_pinned(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None) -> socket.socket:
        del address
        return _connect_to_addrinfos(self._pinned_addrinfos, timeout, source_address)


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    return context


def _open_connection(base, *, timeout_seconds: float):
    port = base.port or (443 if base.scheme == "https" else 80)
    addrinfos = _resolve_public(base.hostname, port)
    if base.scheme == "https":
        return _PinnedHTTPSConnection(
            base.hostname, port, addrinfos=addrinfos, timeout=timeout_seconds, context=_tls_context()
        )
    return _PinnedHTTPConnection(base.hostname, port, addrinfos=addrinfos, timeout=timeout_seconds)


# -- MCP JSON-RPC -------------------------------------------------------------------


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class ToolCallResult:
    content: Any
    is_error: bool


def canonical_tool_digest(tool_def: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of one ``tools/list`` entry — the pin check value."""
    canonical = json.dumps(tool_def, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MCPClient:
    """One capability's connection: initialize once, then list/call tools.

    Every call re-resolves and re-validates the URL (rather than caching a
    connection across calls) — connect-time pinning is only a guarantee about the
    connection it is made for, and this client does not hold connections open
    between tool calls in this slice.
    """

    def __init__(self, url: str, *, bearer_token: str | None = None, timeout_seconds: float = 10.0) -> None:
        self._base = _validate_capability_url(url)
        self._bearer_token = bearer_token
        self._timeout_seconds = timeout_seconds
        self._next_id = 0

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _request(self, body: dict[str, Any]) -> dict[str, Any] | None:
        connection = _open_connection(self._base, timeout_seconds=self._timeout_seconds)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        path = self._base.path or "/"
        if self._base.query:
            path = f"{path}?{self._base.query}"
        try:
            connection.request("POST", path, body=json.dumps(body), headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise MCPUnavailable("the MCP server could not be reached") from exc
        finally:
            connection.close()
        if status >= 400:
            raise MCPUnavailable(f"the MCP server answered with HTTP {status}")
        if not raw:
            return None  # a notification's expected empty ack
        try:
            content_type = response.getheader("Content-Type", "")
            if "text/event-stream" in content_type:
                payload = _last_json_from_sse(raw.decode("utf-8"))
            else:
                payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MCPUnavailable("the MCP server returned a malformed frame") from exc
        if payload is None:
            raise MCPUnavailable("the MCP server returned no JSON-RPC frame")
        return payload

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request({"jsonrpc": "2.0", "id": self._id(), "method": method, "params": params or {}})
        if response is None:
            raise MCPUnavailable(f"the MCP server did not answer {method}")
        if "error" in response:
            error = response["error"] or {}
            raise MCPUnavailable(f"MCP {method} failed: {error.get('message', 'unknown error')}")
        return response.get("result", {})

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._request({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> None:
        self._call("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "hull-broker", "version": "0.1"},
        })
        self._notify("notifications/initialized")

    def list_tools(self) -> list[MCPTool]:
        result = self._call("tools/list")
        tools = result.get("tools", [])
        return [
            MCPTool(
                name=tool.get("name", ""),
                description=tool.get("description", ""),
                input_schema=tool.get("inputSchema", {}) or {},
                raw=tool,
            )
            for tool in tools
            if isinstance(tool, dict)
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        is_error = bool(result.get("isError"))
        content = result.get("content", result)
        return ToolCallResult(content=content, is_error=is_error)


def _last_json_from_sse(raw_text: str) -> dict[str, Any] | None:
    """Pull the last ``data:`` JSON frame out of an SSE body.

    This slice only reads a streamable-HTTP server's *synchronous* response mode
    (one JSON-RPC reply per POST); a server that streams intermediate progress
    frames before the final result is not supported here — the last data frame is
    taken to be the answer, which is correct for a single-reply stream and a
    documented simplification otherwise (see the module report for this gap).
    """
    last: dict[str, Any] | None = None
    for line in raw_text.splitlines():
        if line.startswith("data:"):
            chunk = line[len("data:"):].strip()
            if not chunk:
                continue
            last = json.loads(chunk)
    return last
