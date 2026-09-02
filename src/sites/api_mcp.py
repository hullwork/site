"""The MCP tool surface as a network endpoint on the control-plane API.

Why this exists
---------------
The MCP tools used to be reachable only over stdio, which meant the calling agent host
had to run ``python -m sites.mcp`` *inside its own container* from a copy of this
repository's source. That is a runtime coupling between two products that
``docs/CROSS_REPO_AUTH_CONTRACT`` (§0, restated in ``docs/AUTH.md``) already says must
not exist: an agent host is an **external tenant**, not a sibling component. This module
is the transport that lets it be one.

🔴 **The security premise of stdio was "whoever can open the pipe is us".** Over a network
that premise is gone, so this module must not carry any authorization logic of its own -
adding some would be a second place where tenancy is decided, and the two places would
drift. It therefore does exactly one thing with identity: it authenticates the request
with :meth:`Handler._authenticate`, the same call every ``/v1/*`` route makes, and then
re-presents *the caller's own credential* on a loopback request per tool call. Every tool
call is a normal, authenticated ``/v1/*`` request made by the caller's credential, with
the caller's rights and nobody else's. There is no privileged inner client, so there is
nothing here to escalate through.

The one identity rule this module adds is a **refusal**, not a grant: the reserved
``_agent_user_id`` argument is rejected. Over stdio a trusted runtime injected it after
stripping model-supplied values; over HTTP the acting subject is part of the credential
presentation (``X-Acting-Subject``, admitted only for a merchant key carrying
``may_act_as_subjects``), so a subject named in the request *body* is a caller-declared
identity. It is refused rather than ignored, for the same reason ``X-Merchant-ID`` is:
a caller that believes it deployed for user B while the resources landed under user A
gets a 2xx and nobody ever finds out.

Transport is MCP Streamable HTTP, stateless: one JSON-RPC message per POST, one JSON
response back. No session id is issued and no SSE stream is opened, which the
specification permits and which keeps this file free of connection state that the rest
of the API does not have.
"""
from __future__ import annotations

from os import getenv

from sites import identity as _identity
from sites.client import Client, SitesError
from sites.mcp import Server as _McpServer
from sites.validation import ValidationError


ROUTE = "/mcp"

# The credentials this endpoint accepts, expressed as "which header carries them".
# 🔴 This is a *subset* of what identity.authenticate accepts, never an addition: the
# admin console's signed cookie is deliberately not accepted here. Two reasons, both
# structural. A cookie is ambient, so accepting it on a POST that performs deployments
# would make this endpoint a CSRF target that no other write route is. And the loopback
# hop re-presents a token, not a cookie jar, so honouring one would mean inventing a
# second way to carry an identity across that hop - the exact duplication this module
# exists to avoid.
_TOKEN_CREDENTIAL_HEADER = "X-Sites-Service-Token"


def endpoint_enabled_from_env() -> bool:
    """Whether ``/mcp`` is routed at all.

    Default on. The endpoint exposes no capability that ``/v1/*`` does not already expose
    to the same credential - it is a second spelling of the same authenticated requests -
    so leaving it off by default would not narrow any trust boundary. It would only mean
    that the twelve tools are silently absent until an operator finds a flag, and "the
    tools are just not there" is the failure this endpoint was built to end.

    A value that is neither true nor false stops the process rather than picking one:
    an operator who typed ``SITES_MCP_ENDPOINT_ENABLED=disabled`` meant to turn it off.
    """
    raw = (getenv("SITES_MCP_ENDPOINT_ENABLED", "") or "").strip().lower()
    if not raw:
        return True
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw not in {"false", "0", "no", "off"}:
        raise RuntimeError("SITES_MCP_ENDPOINT_ENABLED must be true or false")
    return False


def _acceptable(header: str) -> bool:
    """Whether the caller will take ``application/json``.

    Absent means "anything", per HTTP. A caller that lists only ``text/event-stream``
    is told 406 instead of being handed a JSON body it said it would not read: this
    server never opens a stream, and pretending otherwise would surface as a parse
    error on the client with nothing pointing back here.
    """
    if not header.strip():
        return True
    for part in header.split(","):
        kind = part.split(";", 1)[0].strip().lower()
        if kind in {"application/json", "application/*", "*/*"}:
            return True
    return False


class McpMixin:
    """``/mcp``. Combined into ``api.Handler``; see the module docstring for the model."""

    # Overwritten by api.serve() from the environment. The default matches
    # endpoint_enabled_from_env() so unit tests that construct a Handler directly get
    # the shipped behaviour rather than a quietly different one.
    mcp_endpoint_enabled: bool = True

    def _mcp_not_allowed(self) -> None:
        """GET and DELETE on the endpoint.

        Streamable HTTP lets a server decline the server-initiated stream (GET) and the
        session-teardown request (DELETE); this one is stateless, so both are 405 with
        ``Allow``. 404 would be worse than useless here - it reads as "the endpoint is
        turned off", which is a different operator action entirely.
        """
        if not self.mcp_endpoint_enabled:
            self._json(404, {"error": "not found"})
            return
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self._common_security_headers()
        self.end_headers()

    def _serve_mcp(self) -> None:
        if not self.mcp_endpoint_enabled:
            self._json(404, {"error": "not found"})
            return
        # DNS-rebinding defence required by the Streamable HTTP specification. A browser
        # cannot reach this endpoint anyway - the credential is a header it cannot set
        # cross-origin without a preflight this API never answers, and no CORS response
        # header is ever sent - so refusing an Origin outright costs nothing real and
        # states the rule instead of leaving it as a property of two other decisions.
        if self.headers.get("Origin", "").strip():
            self._json(
                403,
                {
                    "error": (
                        "the MCP endpoint does not serve browser origins; call it "
                        "server-side with a token credential"
                    ),
                    "code": "mcp_origin_refused",
                },
            )
            return
        if not _acceptable(self.headers.get("Accept", "")):
            self._json(
                406,
                {
                    "error": (
                        "this endpoint answers application/json; it does not open "
                        "a text/event-stream"
                    ),
                    "code": "mcp_not_acceptable",
                },
            )
            return

        authenticated = self._authenticate()
        if authenticated is None:
            return
        merchant_id, _user_id = authenticated
        token = self.headers.get(_TOKEN_CREDENTIAL_HEADER, "").strip()
        if not token:
            # Reached only by a valid console session, which authenticate() accepted.
            # Saying so plainly beats a bare 401: the caller holds a credential that
            # works everywhere else and would otherwise have no way to learn why.
            self._json(
                401,
                {
                    "error": (
                        f"the MCP endpoint requires a {_TOKEN_CREDENTIAL_HEADER} "
                        "credential; console sessions are not accepted"
                    ),
                    "code": "mcp_token_credential_required",
                },
            )
            return

        try:
            message = self._read_body()
        except ValidationError as exc:
            # A JSON-RPC batch is an array, so it lands here as "request body must be a
            # JSON object". That is the right answer: batching was removed from the MCP
            # specification in 2025-06-18 and this server never accepted it.
            self._json(400, {"error": str(exc), "code": "sites_invalid_input"})
            return

        try:
            server = self._mcp_server(token, merchant_id)
            response = server.handle(message)
        except SitesError as exc:
            self._json(
                502,
                {"error": str(exc), "code": exc.code},
            )
            return
        if response is None:
            # A JSON-RPC notification. 202 with no body, per Streamable HTTP.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self._common_security_headers()
            self.end_headers()
            return
        self._json(200, response)

    def _mcp_server(self, token: str, merchant_id: str) -> _McpServer:
        """One MCP server bound to this request's caller, for this request only.

        The client it dispatches through is the caller: same token, same
        ``X-Acting-Subject``, aimed at the socket this very request arrived on. So the
        control plane re-authenticates every tool call from scratch and applies the
        merchant, tenant, quota and impersonation rules it applies to any other client.
        Nothing is cached across requests, because a cache keyed by anything less than
        the whole credential is how one caller starts answering with another's identity.
        """
        base_url = self._loopback_url()
        subject = _identity.acting_subject_from(self.headers)
        caller = Client(base_url, token, subject=subject)

        def refuse_other_subject(_subject: str) -> Client:
            # Unreachable: _agent_user_id is refused before dispatch (subject_from_
            # arguments=False). It exists so that if that refusal is ever weakened, the
            # result is a loud error rather than mcp.Server falling back to
            # Client.from_env() - which would build a client from *this process's*
            # environment credential and act on it under another caller's request.
            raise SitesError(
                "the acting subject is carried by the credential, not by tool "
                "arguments",
                code="sites_invalid_caller_identity",
            )

        return _McpServer(
            client_factory=lambda: caller,
            user_client_factory=refuse_other_subject,
            subject_from_arguments=False,
            merchant_id=merchant_id,
        )

    def _loopback_url(self) -> str:
        """The address this request arrived on.

        Read from the accepted socket rather than from configuration: that is the one
        address guaranteed to be listening, whatever ``SITES_API_HOST`` was bound to,
        and it takes no caller input, so there is nothing here to point elsewhere.
        """
        host, port = self.connection.getsockname()[:2]
        if ":" in str(host):
            host = f"[{host}]"
        return f"http://{host}:{int(port)}"
