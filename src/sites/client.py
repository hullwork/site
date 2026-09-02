"""HTTP client for the Sites control plane.

This is the only outbound API path shared by the CLI and MCP server. Both surfaces expose
the same capabilities and must not duplicate request logic. Credentials are assembled only
here and are never echoed to callers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import quote, urlencode, urlsplit

from os import getenv
from sites import tracing


# The control face is capped at 64 KiB per request, leaving the same order of magnitude margin for responses.
MAX_RESPONSE_BYTES = 512 * 1024
DEFAULT_TIMEOUT = 15.0

ACTING_SUBJECT_HEADER = "X-Acting-Subject"
# Public: the MCP server validates the pseudonym it forwards against the same pattern
# the client validates the one it sends. Two copies of this regex is how the two ends
# of one hop start disagreeing about what a pseudonym is.
ACTING_SUBJECT_RE = re.compile(r"^[0-9a-f]{32}$")
# Minimum salt strength, in **bytes** of UTF-8 - the same floor every repository on this
# contract enforces (docs/acting-subject-vectors.json, ``min_salt_bytes``). A weaker salt is
# the one input that quietly lowers the security of the whole pseudonym scheme: the output
# still looks like a correct 32-hex value, so nothing downstream can tell.
MIN_ACTING_SUBJECT_SALT_BYTES = 32


def acting_subject(salt: str, tenant_id: str, subject_id: str) -> str:
    """The cross-boundary pseudonym for one of this deployment's own users (contract §3.2).

        HMAC-SHA256(salt, tenant_id + "\0" + subject_id)[:16] -> 32 lowercase hex

    Three things follow from the shape, and all three are the reason for it:
    the real account identifier never crosses the boundary; two different (tenant, subject)
    pairs cannot collide by structure rather than by probability; and lowercase hex is a
    subset of every identifier syntax the services on this contract use, so nobody has to
    agree on a regular expression.

    The salt belongs to this deployment and never leaves it - it is what stops the receiving
    side, or anyone reading its database, from recomputing the pseudonym of a known account.
    """
    if not salt:
        raise SitesError(
            "SITES_ACTING_SUBJECT_SALT is required to act for a user; without it "
            "this deployment can only call as its own identity",
            code="sites_acting_salt_missing",
        )
    if len(salt.encode("utf-8")) < MIN_ACTING_SUBJECT_SALT_BYTES:
        # Separate from "missing" on purpose: the two have different fixes, and a
        # configured-but-weak salt is the one an operator believes is already done.
        raise SitesError(
            "the acting-subject salt must be at least "
            f"{MIN_ACTING_SUBJECT_SALT_BYTES} bytes",
            code="sites_acting_salt_too_short",
        )
    digest = hmac.new(
        salt.encode("utf-8"),
        f"{tenant_id}\0{subject_id}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return digest[:16].hex()


class SitesError(RuntimeError):
    """The control plane refused a request or is unreachable."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "sites_error",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


def _read_token(value: str, file_value: str) -> str:
    if value.strip():
        return value.strip()
    if file_value.strip():
        try:
            return Path(file_value).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SitesError(
                f"cannot read token file: {file_value}",
                code="sites_token_unreadable",
            ) from exc
    return ""


class Client:
    """Bounded client for the deployment, bundle and capability endpoints."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        subject: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        parsed = urlsplit(base_url.strip().rstrip("/"))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SitesError(
                f"invalid Sites URL: {base_url!r}",
                code="sites_invalid_url",
            )
        if not token:
            raise SitesError(
                "Sites service token is required",
                code="sites_token_missing",
            )
        self.base_url = base_url.strip().rstrip("/")
        self._token = token
        # 🔴 There is no merchant here any more, by design: the merchant is whatever the
        # credential belongs to, and the control plane refuses a request that names one
        # (identity._refuse_caller_declared_identity). The only thing a caller may still say
        # is *which subject inside that merchant* it speaks for.
        #
        # The value is checked locally because a header carrying control characters raises
        # deep inside http.client with a message about neither the subject nor the request,
        # and a non-ASCII one raises UnicodeEncodeError, which is neither OSError nor
        # URLError and therefore escapes every handler below - the MCP server used to die on
        # the spot and the agent only saw a broken pipe.
        candidate = subject.strip()
        if candidate and not ACTING_SUBJECT_RE.fullmatch(candidate):
            raise SitesError(
                f"{ACTING_SUBJECT_HEADER} must be 32 lowercase hexadecimal "
                "characters; derive it with sites.client.acting_subject",
                code="sites_invalid_subject",
            )
        self._subject = candidate
        self._timeout = timeout

    @classmethod
    def from_env(cls, **overrides: Any) -> "Client":
        """Build a client from the environment.

        The merchant API key and the tenant/admin token use the same request header, and the server relies on table lookup to distinguish them, so
        There can only be one source here: reject it directly if both variables are matched, instead of picking one to win. Replacement voucher
        The most common situation is that the old variables are still left in the shell, and silently selecting one will cause people to troubleshoot the new key.
        A request made with the old token.
        """
        token = _read_token(
            getenv("SITES_TOKEN", "") or "",
            getenv("SITES_TOKEN_FILE", "") or "",
        )
        merchant_key = _read_token(
            getenv("SITES_MERCHANT_KEY", "") or "",
            getenv("SITES_MERCHANT_KEY_FILE", "") or "",
        )
        if token and merchant_key:
            raise SitesError(
                "SITES_TOKEN and SITES_MERCHANT_KEY are both set; keep one",
                code="sites_ambiguous_credentials",
            )
        return cls(
            overrides.pop("base_url", None)
            or getenv("SITES_URL", "http://127.0.0.1:18091"),
            overrides.pop("token", None) or merchant_key or token,
            subject=overrides.pop("subject", getenv("SITES_ACTING_SUBJECT", "") or ""),
            **overrides,
        )

    @staticmethod
    def subject_for(subject_id: str) -> str:
        """Derive the pseudonym for one local account from this deployment's own salt.

        Fails closed when no salt is configured: falling back to the service identity would
        quietly file every user's sites under one tenant, and it would look like success.
        """
        return acting_subject(
            _read_token(
                getenv("SITES_ACTING_SUBJECT_SALT", "") or "",
                getenv("SITES_ACTING_SUBJECT_SALT_FILE", "") or "",
            ),
            getenv("SITES_ACTING_TENANT", "") or "",
            subject_id,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Sites-Service-Token": self._token,
            "User-Agent": "sites-client",
            **tracing.outbound_headers(),
        }
        # Acting for a subject is a grant on the key itself (may_act_as_subjects). A key
        # without it that sends this header is refused rather than demoted, so there is no
        # configuration in which this line silently means something else.
        if self._subject:
            headers[ACTING_SUBJECT_HEADER] = self._subject
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with tracing.span(
            "sites.client.request",
            kind=3,
            attributes={"http.request.method": method, "server.address": self.base_url},
        ) as request_span:
            try:
                return self._request_traced(method, path, payload, query=query)
            except BaseException as exc:
                request_span.set_error(exc)
                raise

    def _request_traced(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/v1/") or "?" in path or "#" in path:
            raise SitesError(
                f"invalid API path: {path!r}", code="sites_invalid_path"
            )
        # The query string can only be spelled from here: naked "?" is still prohibited in path, otherwise an unescaped
        # The service name can turn itself into an additional filter.
        filtered = {
            key: value
            for key, value in (query or {}).items()
            if value not in (None, "")
        }
        if filtered:
            path = f"{path}?{urlencode(filtered)}"
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = _urlrequest.Request(
            f"{self.base_url}{path}", data=body, method=method, headers=headers
        )
        try:
            with _urlrequest.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except _urlerror.HTTPError as exc:
            raise self._upstream_error(exc) from exc
        except (OSError, _urlerror.URLError) as exc:
            raise SitesError(
                f"Sites is unreachable at {self.base_url}",
                code="sites_unreachable",
                retryable=True,
            ) from exc
        return self._decode(raw)

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SitesError(
                "Sites response exceeded the size limit",
                code="sites_response_too_large",
            )
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SitesError(
                "Sites returned invalid JSON", code="sites_invalid_json"
            ) from exc
        if not isinstance(payload, dict):
            raise SitesError(
                "Sites returned a non-object payload",
                code="sites_invalid_payload",
            )
        return payload

    @classmethod
    def _upstream_error(cls, error: _urlerror.HTTPError) -> SitesError:
        message = f"Sites request failed with {error.code}"
        code = "sites_upstream_error"
        try:
            payload = cls._decode(error.read(MAX_RESPONSE_BYTES + 1))
            message = str(payload.get("error") or message)[:500]
            code = str(payload.get("code") or code)[:100]
        except (SitesError, OSError):
            pass
        return SitesError(
            message,
            status=int(error.code),
            code=code,
            retryable=int(error.code) in {502, 503, 504},
        )

    # --- endpoints -----------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/v1/capabilities")

    def scaffolds(self) -> dict[str, Any]:
        """Return live scaffold support and executable contract evidence."""
        return self._request("GET", "/v1/scaffolds")

    def list_deployments(self) -> dict[str, Any]:
        return self._request("GET", "/v1/deployments")

    def get_deployment(self, service_name: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/deployments/{quote(service_name, safe='')}"
        )

    def deploy(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/deployments", payload)

    def delete_deployment(self, service_name: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/v1/deployments/{quote(service_name, safe='')}"
        )

    def create_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit a Dockerfile source build. 202 Building only means that the source code has been accepted."""
        return self._request("POST", "/v1/builds", payload)

    def get_build(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/builds/{quote(name, safe='')}")

    def delete_build(self, name: str) -> dict[str, Any]:
        """Delete build: Accurately recycle Job, SiteDeployment, source code and Registry manifest."""
        return self._request("DELETE", f"/v1/builds/{quote(name, safe='')}")

    def whoami(self) -> dict[str, Any]:
        """This caller's own identity and quota. No other tenants are visible."""
        return self._request("GET", "/v1/tenants/self")

    def query_site(
        self,
        site_name: str,
        query: str,
        *,
        row_limit: int = 100,
        timeout_seconds: int = 5,
    ) -> dict[str, Any]:
        """Run one bounded read-only query against a dynamic site's schema."""
        return self._request(
            "POST",
            f"/v1/sites/{quote(site_name, safe='')}/query",
            {
                "query": query,
                "rowLimit": row_limit,
                "timeoutSeconds": timeout_seconds,
            },
        )

    def list_site_versions(self, site_name: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/sites/{quote(site_name, safe='')}/versions"
        )

    def create_site_version(
        self, site_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/sites/{quote(site_name, safe='')}/versions", payload
        )

    def create_static_site_version(
        self,
        site_name: str,
        files: dict[str, str],
        *,
        content_sha256: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upload private static content and create one immutable version."""
        payload: dict[str, Any] = {
            "siteType": "static",
            "files": files,
            "metadata": metadata or {},
        }
        if content_sha256:
            payload["contentSha256"] = content_sha256
        return self.create_site_version(site_name, payload)

    def promote_site_version(self, site_name: str, version: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sites/{quote(site_name, safe='')}/promote",
            {"version": version},
        )

    # --- tenant administration (admin token only) ----------------------
    # user_id is only unique within a merchant, so naming a tenant must include merchantId - without it on the server
    # Would 400 instead of guessing a merchant. The list is the only exception: without it it's all platforms.
    def create_tenant(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/tenants", payload)

    def list_tenants(self, *, merchant_id: str = "") -> dict[str, Any]:
        return self._request(
            "GET", "/v1/tenants", query={"merchantId": merchant_id}
        )

    def update_tenant(
        self, user_id: str, payload: dict[str, Any], *, merchant_id: str
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/v1/tenants/{quote(user_id, safe='')}",
            payload,
            query={"merchantId": merchant_id},
        )

    def rotate_tenant_token(
        self, user_id: str, *, merchant_id: str = ""
    ) -> dict[str, Any]:
        """Issue a new token; re-enables the tenant if it was disabled."""
        return self._request(
            "POST",
            f"/v1/tenants/{quote(user_id, safe='')}/token",
            query={"merchantId": merchant_id},
        )

    def disable_tenant(
        self, user_id: str, *, merchant_id: str = ""
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/tenants/{quote(user_id, safe='')}",
            query={"merchantId": merchant_id},
        )

    # --- merchant administration (admin token only) --------------------
    def list_merchants(self) -> dict[str, Any]:
        return self._request("GET", "/v1/merchants")

    def create_merchant(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a merchant. The apiKey plaintext in the response only appears once."""
        return self._request("POST", "/v1/merchants", payload)

    def get_merchant(self, merchant_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/merchants/{quote(merchant_id, safe='')}"
        )

    def update_merchant(
        self, merchant_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/v1/merchants/{quote(merchant_id, safe='')}", payload
        )

    def rotate_merchant_key(self, merchant_id: str) -> dict[str, Any]:
        """Issue a new API key; also only returns plain text once."""
        return self._request(
            "POST", f"/v1/merchants/{quote(merchant_id, safe='')}/key"
        )

    def disable_merchant(self, merchant_id: str) -> dict[str, Any]:
        """Soft-disable a merchant. The data is retained, and the tokens of the tenants under the name are also invalidated."""
        return self._request(
            "DELETE", f"/v1/merchants/{quote(merchant_id, safe='')}"
        )

    # --- platform-wide read-only views (admin token only) --------------
    def admin_deployments(
        self,
        *,
        merchant_id: str = "",
        phase: str = "",
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Every deployment on the platform, across merchants and tenants."""
        return self._request(
            "GET",
            "/v1/admin/deployments",
            query={
                "merchantId": merchant_id,
                "phase": phase,
                "limit": limit,
            },
        )

    def admin_health(self) -> dict[str, Any]:
        """Control-plane self-check.

        Each item has its own reachable, and the whole will not fail because one item fails - this is exactly what the administrator is doing
        Only look at this when something goes wrong.
        """
        return self._request("GET", "/v1/admin/health")

    def submit_bundle(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/bundles", payload)

    def get_bundle(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/bundles/{quote(name, safe='')}")

    def delete_bundle(self, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/bundles/{quote(name, safe='')}")
