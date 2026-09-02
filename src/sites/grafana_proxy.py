"""Same-origin reverse proxy for a single embedded Grafana panel.

Why a proxy at all, rather than pointing an iframe straight at Grafana: a
cross-origin iframe would need ``frame-src`` opened up in the console's CSP and
``allow_embedding`` plus ``cookie_samesite=none`` on the Grafana side, and the
browser would end up holding a Grafana credential.  Proxying on the console's
own origin keeps the console CSP (``default-src 'self'``) untouched and keeps
every Grafana credential server-side.

Three properties this module exists to guarantee, in the order they matter:

1. **Only an administrator gets through.**  ``serve`` checks it first; the
   metrics behind these panels carry no tenant or site label, deliberately
   (``api.py::_route_template`` collapses unmatched paths to ``other`` for
   exactly that reason), so every panel is cross-tenant data - an operator
   view, never a tenant view.  A tenant-scoped panel would first need a
   bounded tenant dimension on the metrics, which is a different change.
2. **Only the paths a solo panel actually needs.**  ``ALLOWED`` is a code
   constant and is deliberately *not* configurable.  Proxying ``/grafana/*``
   wholesale while injecting the service-account token would republish the
   entire Grafana API - including ``/api/datasources/proxy/...``, which is
   "run any query against any datasource" - to every console user.  A
   configurable allowlist is the same hole with a delay on it.
3. **Credentials do not cross in either direction.**  The console session
   cookie is dropped before the request leaves; Grafana's ``Set-Cookie`` is
   dropped before the response returns.  Gitpod, code-server and Daytona each
   arrived at this same rule independently, and for the same reason: a proxy
   that forwards both sides' credentials has merged two trust domains.

The service-account token must be **Viewer**, scoped to the folder holding the
dashboards in ``observability/dashboards/``.  Nothing here can enforce that -
it is granted in Grafana - but the allowlist means a leaked token could only do
what a Viewer could already do through this path.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

ENV_PREFIX = "SITES_"
#The dashboard this console embeds, shipped in observability/dashboards/.
DEFAULT_DASHBOARD_UID = "sites-control-plane"
#Panels offered to the console, as (panelId, title).  The console renders one
#iframe per entry and may not invent ids of its own: an id that no longer
#exists renders as a blank rectangle with no error anywhere, so
#test_grafana_embed.py asserts every id below is still in the shipped dashboard JSON.
PANELS: tuple[tuple[int, str], ...] = (
    (1, 'API up'),
    (2, 'Snapshot age'),
    (3, 'Route table age'),
    (4, 'Scale-to-zero sites'),
    (10, 'API requests by status class'),
    (11, 'API latency by route'),
    (12, 'Dependency availability'),
    (13, 'Snapshot sync outcomes'),
    (14, 'Operator reconciles by kind and outcome'),
    (15, 'Sweep age (time since last completed sweep)'),
    (16, 'Activator cold starts by outcome'),
    (17, 'Activator forwarded requests by outcome'),
)

#Path prefix this console serves the proxy on.  Same origin, so the console CSP
#covers the iframe without a frame-src entry.
ROUTE_PREFIX = "/grafana/"

#🔴 Closed allowlist of what a `d-solo` panel fetches.  Anything absent is 403.
#Adding an entry means republishing that Grafana API to every administrator of
#this console; `/api/datasources/proxy/` and `/api/admin/` must never appear.
_UID = r"[A-Za-z0-9_-]{1,64}"
_SLUG = r"[A-Za-z0-9._-]{0,128}"
_ASSET = r"[A-Za-z0-9._/-]{1,256}"
ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = (
    #The panel document itself.
    ("GET", re.compile(rf"^/d-solo/{_UID}(?:/{_SLUG})?$")),
    #Grafana's own front-end bundle and assets.  The charset excludes "%" so a
    #percent-encoded traversal cannot be smuggled past the "..' check below.
    ("GET", re.compile(rf"^/public/(?:build|fonts|img|lib|plugins|app)/{_ASSET}$")),
    #Bootstrap configuration the front-end reads before it can render anything.
    ("GET", re.compile(r"^/api/frontend/settings$")),
    #The dashboard model backing the panel.
    ("GET", re.compile(rf"^/api/dashboards/uid/{_UID}$")),
    #Panel plugins fetch their own settings document.
    ("GET", re.compile(r"^/api/plugins/[A-Za-z0-9._-]{1,64}/settings$")),
    #The data.  Read-only: Grafana resolves the datasource by uid from the
    #request body and returns query results.  This is the only non-GET entry.
    ("POST", re.compile(r"^/api/ds/query$")),
)

#Response headers for the framed document.  🔴 `frame-ancestors 'self'`, not
#'none': this *is* the framed document, and the console's own `'none'` would
#make the browser refuse to render our own iframe.  Grafana bootstraps through
#an inline `window.grafanaBootData` script, so 'unsafe-inline' for script-src
#is unavoidable here - which is exactly why it is scoped to this path and why
#the console's own policy is left alone.
PANEL_CSP = (
    "default-src 'self'; base-uri 'none'; connect-src 'self'; "
    "font-src 'self' data:; form-action 'none'; frame-ancestors 'self'; "
    "img-src 'self' data: blob:; object-src 'none'; "
    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
)

#Bounded: an administrator's browser pulls Grafana's front-end bundle through
#this process.  16 MiB is comfortably above the bundle and far below anything
#that could be used to pin memory.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024
UPSTREAM_TIMEOUT_SECONDS = 15.0
_CHUNK = 64 * 1024

#Request headers forwarded upstream.  An allowlist, not a blocklist: Cookie,
#Authorization and every console-specific identity header must not reach
#Grafana, and a blocklist would leak the next header somebody adds.
_FORWARD_REQUEST_HEADERS = frozenset({
    "accept", "accept-language", "content-type", "if-none-match",
    "if-modified-since",
})
#Response headers returned to the browser.  Set-Cookie is absent on purpose:
#Grafana's session cookie would land on the console's origin and path.
_FORWARD_RESPONSE_HEADERS = frozenset({
    "content-type", "cache-control", "etag", "last-modified", "vary",
})


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str
    dashboard_uid: str
    org_id: int
    datasource_uid: str

    @property
    def enabled(self) -> bool:
        """All three or nothing.

        A base URL without a token would send unauthenticated requests to
        Grafana, and the operator would see an iframe full of 401s instead of
        an absent tab - strictly worse than the feature not being there.

        🔴 The datasource uid is required for a security reason, not a
        rendering one.  ``POST /api/ds/query`` dispatches on the datasource uid
        in the request body, so it is only "read-only" for the datasource it
        happens to name.  A Grafana with a SQL datasource attached turns that
        one endpoint into arbitrary SQL - and a folder-scoped Viewer does not
        stop it, because datasource permissions in OSS Grafana are not scoped
        by folder.  We cannot control which datasources an operator has, only
        which ones we forward to, and ``allowed_datasource_uids`` can only
        answer that if the operator has named the one our panels use.
        """
        return bool(self.base_url and self.token and self.datasource_uid)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(f"{ENV_PREFIX}{name}", "") or default).strip()


def _read_token() -> str:
    direct = _env("GRAFANA_TOKEN")
    if direct:
        return direct
    path = _env("GRAFANA_TOKEN_FILE")
    if not path:
        return ""
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        #A mounted-secret path that is not there yet means "not configured",
        #the same as an unset variable.  Failing to start would make an
        #optional integration able to take the console down.
        return ""


def _clean_base_url(raw: str) -> str:
    """Accept only an absolute http(s) origin with no credentials or fragment."""
    if not raw or len(raw) > 2048:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return raw.rstrip("/")


def load_config() -> Config:
    """Read the configuration fresh.

    Not cached: tests set the environment per case, and an operator restarting
    the process is the only supported way to change it anyway.
    """
    try:
        org_id = int(_env("GRAFANA_ORG_ID", "1"))
    except ValueError:
        org_id = 1
    uid = _env("GRAFANA_DASHBOARD_UID", DEFAULT_DASHBOARD_UID)
    if not re.fullmatch(_UID, uid):
        uid = DEFAULT_DASHBOARD_UID
    datasource = _env("GRAFANA_DATASOURCE_UID")
    if datasource and not re.fullmatch(_UID, datasource):
        datasource = ""
    return Config(
        base_url=_clean_base_url(_env("GRAFANA_URL")),
        token=_read_token(),
        dashboard_uid=uid,
        org_id=max(1, org_id),
        datasource_uid=datasource,
    )


def capabilities(config: Config) -> dict:
    """What the console needs in order to decide whether to render the tab."""
    if not config.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "route": ROUTE_PREFIX,
        "dashboardUid": config.dashboard_uid,
        "panels": [{"id": pid, "title": title} for pid, title in PANELS],
    }


def upstream_path(request_path: str) -> str | None:
    """Strip ROUTE_PREFIX and reject anything that could escape it."""
    if not request_path.startswith(ROUTE_PREFIX):
        return None
    suffix = request_path[len(ROUTE_PREFIX) - 1:]
    #"//host" would make urljoin treat the rest as an authority; ".." would
    #climb out of the prefix; a NUL or newline would let a header be forged.
    if suffix.startswith("//") or ".." in suffix:
        return None
    if any(ch in suffix for ch in ("\r", "\n", "\x00")):
        return None
    return suffix


def is_allowed(method: str, path: str) -> bool:
    return any(
        method == allowed_method and pattern.match(path)
        for allowed_method, pattern in ALLOWED
    )


def is_same_origin(headers, host: str) -> bool:
    """CSRF check for the one non-GET entry in the allowlist.

    🔴 A deliberate substitution, and a **fail-closed** one.  ``POST
    /api/ds/query`` is issued by Grafana's own front-end, which cannot know
    this console's CSRF token, so the console's double-submit check would
    reject every panel load.  Fetch metadata plus an Origin comparison is the
    replacement - not an exemption.

    Both headers are "meaningful only when present", which is how this kind of
    check fails open: "check Sec-Fetch-Site, else check Origin, else allow"
    admits anything that simply omits both.  The rule is therefore:

    * ``Sec-Fetch-Site: same-origin``            -> allow
    * otherwise an ``Origin`` equal to this host -> allow
    * **neither header present                   -> refuse**

    🔴 Be precise about what that buys, because overstating it would let
    somebody skip a control elsewhere.  A browser always attaches at least one
    of these headers to a request a page caused, so the rule turns away **every
    page-driven cross-origin request** - which is the entire class CSRF is about.
    It does **not** stop a caller that composes its own request: such a caller
    can equally well send ``Origin: <this host>``, and ``host`` here comes from
    the caller's own ``Host`` header, so the comparison would agree with itself.
    That is not a hole in this check; an attacker who already holds the session
    cookie *and* can compose requests is a session-theft problem, and no
    origin-based check has ever addressed it.

    Refusing the headerless case still costs nothing real: every browser sends
    ``Origin`` on a same-origin POST, so the only callers it turns away are the
    ones that were never following a page.

    Residual risk, named rather than buried: a forged same-origin request would
    achieve "an administrator's browser ran one of our dashboard queries" - the
    response is not readable cross-origin, the allowlist is read-only, and
    ``check_query_datasources`` bounds which datasource it can even name.
    """
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site == "same-origin":
        return True
    if fetch_site:
        #Present and something else: cross-site, same-site or none. All refused.
        return False
    origin = (headers.get("Origin") or "").strip()
    if not origin:
        return False
    parsed = urllib.parse.urlsplit(origin)
    return bool(parsed.netloc) and bool(host) and parsed.netloc == host


# ---------------------------------------------------------------- tracing ---
# W3C trace context, propagated by hand.  No SDK: this repository takes no
# tracing dependency, and the header is a string - accepting one, checking its
# shape, minting a child and forwarding it needs no library.  Choosing the
# standard header over an invented one is what lets an operator who already runs
# a traced gateway in front of this service see one continuous trace instead of
# two halves nobody can join.
_TRACEPARENT = re.compile(
    r"^00-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
# Sampled. Only ever written when *we* mint the trace id; see inbound_trace.
_DEFAULT_FLAGS = "01"


def parse_traceparent(value: str) -> tuple[str, str] | None:
    """``(trace_id, flags)`` from a well-formed traceparent, else None.

    🔴 The flags come back with the trace id on purpose.  If this returned the
    trace id alone, dropping the flags would be the path of least resistance at
    every call site - and dropping them is silent: the trace stays continuous,
    no test goes red, and the only symptom is a collector receiving spans the
    caller had already decided not to sample.  The signature is the guard.

    An all-zero trace id is not a trace: the spec reserves it as the invalid
    value, and treating it as inherited would join unrelated requests together.
    """
    match = _TRACEPARENT.match((value or "").strip())
    if match is None:
        return None
    trace = match.group("trace")
    if trace == "0" * 32 or match.group("span") == "0" * 16:
        return None
    return trace, match.group("flags")


def inbound_trace(headers) -> tuple[str, str, bool]:
    """``(trace_id, flags, inherited)`` for the request being served.

    Priority: a valid ``traceparent``, then an ``X-Request-Id`` that already has
    the shape of a trace id, then a fresh one.

    🔴 Flags are inherited verbatim when a caller supplied them, and
    ``_DEFAULT_FLAGS`` is written **only** when we mint the id ourselves.
    Upgrading an inbound ``00`` to ``01`` would overturn a sampling decision
    somebody upstream already made, and overturn it invisibly - the trace stays
    connected and nothing fails, the collector just quietly receives a stretch
    of spans it was told to skip.  We are a propagator, not a sampling
    authority; we decide only when nobody has decided.

    Header lookup is case-insensitive because ``http.client`` message objects
    are.  The wire spelling is **not** part of any contract here - ``urllib``
    emits ``Traceparent`` and that is fine; what is required is that a receiver
    matches without regard to case.  Normalising the sent form would mean
    working around the standard library for no gain.
    """
    supplied = headers.get("traceparent") or headers.get("Traceparent") or ""
    parsed = parse_traceparent(supplied)
    if parsed is not None:
        return parsed[0], parsed[1], True
    request_id = (headers.get("X-Request-Id") or "").strip().lower()
    if _HEX32.match(request_id):
        # An id that already has the shape carries the correlation across for
        # free. One that does not is left alone rather than mangled into hex:
        # it stays a separate log field and keeps meaning what it meant.
        return request_id, _DEFAULT_FLAGS, False
    return secrets.token_hex(16), _DEFAULT_FLAGS, False


def child_traceparent(trace_id: str, flags: str) -> str:
    """A traceparent for one outbound hop: same trace, a fresh span id."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


class ProxyError(Exception):
    """Upstream could not be reached or answered unusably."""


def forward(
    config: Config,
    method: str,
    path: str,
    query: str,
    headers,
    body: bytes,
):
    """Send one allowlisted request to Grafana and return (status, headers, chunks).

    ``headers`` is the inbound message object; only ``_FORWARD_REQUEST_HEADERS``
    are copied, so no caller can add a header to this hop by sending it.

    🔴 That allowlist is also why the caller's ``traceparent`` cannot leak
    through: it is not on the list, so it is dropped, and a **new** one is
    minted below for this hop.  Forwarding the browser's own header verbatim
    would file this proxy's wait on Grafana as time spent inside the caller's
    span - the caller would appear to have taken however long Grafana took.
    Same trace, new span: that is the whole point of the child id.
    """
    target = f"{config.base_url}{path}"
    if query:
        target = f"{target}?{query}"
    forwarded = {}
    for name in _FORWARD_REQUEST_HEADERS:
        value = headers.get(name)
        if value:
            forwarded[name] = value
    #The only credential on this hop.  Set last so no inbound header can
    #shadow it through a differently-cased key.
    forwarded["Authorization"] = f"Bearer {config.token}"
    forwarded["Accept-Encoding"] = "identity"
    trace_id, flags, _inherited = inbound_trace(headers)
    forwarded["traceparent"] = child_traceparent(trace_id, flags)
    request = urllib.request.Request(
        target, data=body or None, method=method, headers=forwarded
    )
    try:
        response = urllib.request.urlopen(
            request, timeout=UPSTREAM_TIMEOUT_SECONDS
        )
    except urllib.error.HTTPError as error:
        #Grafana's own 4xx/5xx are meaningful to the panel; pass the status
        #through but never the body of a 5xx, which can carry internals.
        response = error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ProxyError(str(error)) from error

    status = getattr(response, "status", None) or response.getcode() or 502
    out_headers = {}
    for name, value in response.headers.items():
        if name.lower() in _FORWARD_RESPONSE_HEADERS:
            out_headers[name] = value
    chunks: list[bytes] = []
    total = 0
    with response:
        while True:
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ProxyError("grafana response exceeds the size limit")
            chunks.append(chunk)
    return status, out_headers, chunks


#The one endpoint in ALLOWED that is not addressed by URL alone.
DS_QUERY_PATH = "/api/ds/query"


def allowed_datasource_uids(config: Config) -> frozenset[str]:
    """Datasource uids this console will forward a query for.

    Exactly one: the shipped dashboards select their datasource through a
    single template variable rather than pinning one per panel, so "which
    datasources do our panels use" has one answer and the operator supplies it.
    ``test_grafana_embed`` asserts the dashboard still has that shape, so a
    future panel that pins a second datasource turns this assumption red
    instead of silently widening what the proxy will forward.
    """
    return frozenset({config.datasource_uid})


def check_query_datasources(body: bytes, allowed: frozenset[str]) -> bool:
    """Whether every query in a ``/api/ds/query`` body names an allowed datasource.

    🔴 Why the body has to be read at all.  ``/api/ds/query`` is Grafana's
    generic query endpoint: it dispatches on the datasource uid *inside the
    request*, so calling it "read-only" is only true of the datasource it names.
    If the operator's Grafana also has a MySQL, Postgres or MSSQL datasource
    attached, that same endpoint runs arbitrary SQL there - and the
    service account being a folder-scoped Viewer does not prevent it, because
    OSS Grafana does not scope datasource permissions by folder.

    We cannot control what an operator has connected.  We can control what we
    forward.  This turns the boundary from "whatever the service account may
    reach", which we cannot describe, into "the datasource our own panels use",
    which we can.

    Fail-closed in every uncertain case: unparseable body, no queries, a query
    with no datasource uid (Grafana would fall back to its default, which we
    have not vetted), or a legacy numeric ``datasourceId`` we cannot compare.
    One out-of-bounds query refuses the whole request rather than the single
    entry - a partial forward would still have run it.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        return False
    default = payload.get("datasource")
    for query in queries:
        if not isinstance(query, dict):
            return False
        if "datasourceId" in query or "datasourceId" in payload:
            #The pre-uid addressing scheme. A numeric id cannot be compared
            #against a uid allowlist, so it is refused rather than guessed at.
            return False
        source = query.get("datasource")
        if source is None:
            source = default
        if not isinstance(source, dict):
            return False
        uid = source.get("uid")
        if not isinstance(uid, str) or uid not in allowed:
            return False
    return True


def panel_query(
    config: Config,
    *,
    panel_id: int,
    from_spec: str,
    to_spec: str,
    theme: str,
) -> str:
    """Build the d-solo query string from console-controlled values only.

    Nothing here is copied from the request.  The console picks a range from a
    fixed list and a panel id from ``PANELS``; letting a caller assemble the
    Grafana URL would put datasource and variable selection back in its hands.
    """
    params = [
        ("orgId", str(config.org_id)),
        ("panelId", str(panel_id)),
        ("from", from_spec),
        ("to", to_spec),
        ("theme", theme),
    ]
    if config.datasource_uid:
        params.append(("var-datasource", config.datasource_uid))
    return urllib.parse.urlencode(params)


def serve(handler, path: str) -> None:
    """Answer one ``/grafana/...`` request on the console's own origin.

    Lives here rather than in ``api.py`` so the whole feature is one file: the
    Handler only needs two dispatch lines, and everything that decides who may
    see a panel and which upstream paths exist is in one place to review.

    Order matters and is not cosmetic:

    1. Administrator first.  A non-administrator must get the same answer
       whether or not Grafana is wired up, or the 403/404 difference becomes a
       configuration oracle for anyone with a session.
    2. Allowlist second.  The request that leaves here carries a Grafana
       service-account token, so a path outside ``ALLOWED`` would be a Grafana
       API republished to every administrator of this console.
    3. Same-origin third, for the single POST entry.  See ``is_same_origin``.
    """
    from sites import identity  # local: keeps this module free of import cycles

    # 🔴 ``unsafe=False`` on purpose, even for the POST.  The console's CSRF
    # check is a double-submit cookie, and the one non-GET entry in ALLOWED is
    # issued by Grafana's own front-end, which cannot read that cookie - so the
    # check would reject every panel load.  ``is_same_origin`` below is the
    # replacement, applied to exactly that request.  This is a substitution,
    # not an exemption; the residual risk is written out in its docstring.
    if not identity.is_admin(
        handler.headers,
        handler.service_token,
        session_key=handler.session_key,
        local_login_enabled=handler.local_login_enabled,
        unsafe=False,
    ):
        handler._json(403, {"error": "this endpoint requires the admin token"})
        return
    config = load_config()
    if not config.enabled:
        # 404, not 503: with no Grafana configured this route does not exist.
        # The console hides the tab for the same reason, and the repository
        # stays deployable with no Grafana anywhere.
        handler._json(404, {"error": "grafana is not configured"})
        return
    upstream = upstream_path(path)
    if upstream is None or not is_allowed(handler.command, upstream):
        handler._json(403, {"error": "path is not part of the embedded panel"})
        return
    if handler.command != "GET" and not is_same_origin(
        handler.headers, handler.headers.get("Host", "")
    ):
        handler._json(403, {"error": "cross-origin request"})
        return
    body = b""
    if handler.command == "POST":
        length = int(handler.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            handler._json(413, {"error": "request body is too large"})
            return
        body = handler.rfile.read(length) if length else b""
    if upstream == DS_QUERY_PATH and not check_query_datasources(
        body, allowed_datasource_uids(config)
    ):
        # 🔴 The allowlist bounds the URL; this bounds the body. /api/ds/query
        # dispatches on a datasource uid inside the request, so URL-only
        # filtering would leave "run anything against any datasource this
        # Grafana can reach" open. See check_query_datasources.
        handler._json(
            403, {"error": "query names a datasource this panel does not use"}
        )
        return
    try:
        status, headers, chunks = forward(
            config,
            handler.command,
            upstream,
            urllib.parse.urlsplit(handler.path).query,
            handler.headers,
            body,
        )
    except ProxyError as error:
        # The message is ours, never the upstream body: a Grafana error page can
        # name internal hosts and this response renders inside the console.
        telemetry_log(handler, error)
        handler._json(502, {"error": "grafana is unreachable"})
        return
    payload = b"".join(chunks)
    content_type = headers.pop("Content-Type", None) or "application/octet-stream"
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    # 🔴 The panel policy, not the console's.  This response *is* the framed
    # document: the console's own ``frame-ancestors 'none'`` would make the
    # browser refuse to render the console's own iframe.  Every console page
    # keeps the stricter policy; the override is scoped to this path.
    handler.send_header("Content-Security-Policy", PANEL_CSP)
    handler.send_header("X-Frame-Options", "SAMEORIGIN")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.end_headers()
    try:
        handler.wfile.write(payload)
    except (BrokenPipeError, ConnectionResetError):
        # A browser that navigated away mid-panel is not an error worth logging.
        pass


def telemetry_log(handler, error: Exception) -> None:
    """Record the upstream failure where the address is allowed to be."""
    from sites import telemetry

    telemetry.log_exception("grafana_panel_proxy_failed", error)
