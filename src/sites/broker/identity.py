"""Who is calling the Broker: an app workload, and optionally the end user it acts for.

Also home to the app-identity flow (contract section 3.3): a Kubernetes projected
service-account token is exchanged, through ``TokenReview``, for a Broker app token.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sites.broker import db as broker_db
from sites.broker import tokens as broker_tokens

if TYPE_CHECKING:
    # http.py imports AppCaller/EndUser from this module, so this module must not
    # import http.py back at module scope -- that would be a real import cycle,
    # not just one the architecture test would catch (it only scans src/sites/*.py
    # at the top level, not this subpackage). register() below imports http.py
    # lazily, inside the function body, once both modules already finished
    # loading.
    from sites.broker.http import Router, Services


@dataclass(frozen=True)
class AppCaller:
    app_env_id: str
    site: str | None


@dataclass(frozen=True)
class EndUser:
    id: str
    org: str | None
    role: str
    plan: str
    env: str


def verify_app_token(keys: broker_tokens.KeyRing, token: str) -> AppCaller:
    claims = keys.verify(token, aud=broker_tokens.APP_AUDIENCE, typ="app")
    return AppCaller(app_env_id=claims["sub"], site=claims.get("site"))


def verify_user_token(keys: broker_tokens.KeyRing, token: str, *,
                       app_env_id: str) -> EndUser:
    """Raises ``tokens.TokenError`` when the audience does not match ``app_env_id``
    -- this is the check that keeps one app's users from acting as another's.
    """
    claims = keys.verify(token, aud=app_env_id, typ="user")
    return EndUser(id=claims["sub"], org=claims.get("org"), role=claims.get("role", ""),
                   plan=claims.get("plan", ""), env=claims.get("env", ""))


class TokenReviewer:
    """Validates a workload's bearer credential and names the Kubernetes identity
    behind it, or reports that it does not hold up.
    """

    def review(self, service_account_token: str) -> str | None:
        """Return ``system:serviceaccount:<namespace>:<name>``, or None when the
        token does not authenticate (expired, wrong audience, unknown, malformed).
        """
        raise NotImplementedError


def parse_service_account(username: str) -> tuple[str, str] | None:
    prefix = "system:serviceaccount:"
    if not username.startswith(prefix):
        return None
    rest = username[len(prefix):]
    parts = rest.split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


class KubeTokenReviewer(TokenReviewer):
    """Real reviewer: POSTs ``TokenReview`` to the API server the gateway Pod's own
    in-cluster service account is projected into. The gateway never trusts the
    workload token's claims directly -- it only trusts what the API server says
    about it, which is what makes a token that a workload cannot forge on its own.
    """

    def __init__(self, kube_client: Any = None, audiences: tuple[str, ...] = ("hull-broker",)):
        self._kube_client = kube_client
        self._audiences = list(audiences)

    def _kube(self) -> Any:
        if self._kube_client is not None:
            return self._kube_client
        from sites.kube import KubeClient

        self._kube_client = KubeClient()
        return self._kube_client

    def review(self, service_account_token: str) -> str | None:
        from sites.kube import ApiError

        body = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": service_account_token, "audiences": self._audiences},
        }
        try:
            result = self._kube().create("/apis/authentication.k8s.io/v1/tokenreviews", body)
        except ApiError:
            return None
        status = result.get("status") or {}
        if not status.get("authenticated"):
            return None
        username = (status.get("user") or {}).get("username", "")
        if parse_service_account(username) is None:
            return None
        audiences = status.get("audiences") or []
        if self._audiences and not any(a in audiences for a in self._audiences):
            return None
        return username


class FakeTokenReviewer(TokenReviewer):
    """Test double: a fixed map from opaque token string to Kubernetes username."""

    def __init__(self, tokens: dict[str, str] | None = None):
        self._tokens = dict(tokens or {})

    def add(self, token: str, username: str) -> None:
        self._tokens[token] = username

    def review(self, service_account_token: str) -> str | None:
        return self._tokens.get(service_account_token)


def issue_app_token_for_workload(db: broker_db.BrokerDatabase,
                                  keys: broker_tokens.KeyRing,
                                  reviewer: TokenReviewer,
                                  service_account_token: str) -> tuple[str, int] | None:
    """The full contract section 3.3 flow. None means ``401 invalid_workload``."""
    username = reviewer.review(service_account_token)
    if username is None:
        return None
    parsed = parse_service_account(username)
    if parsed is None:
        return None
    namespace, service_account = parsed
    with db.transaction() as cursor:
        app_env_id = broker_db.app_env_id_for_workload(cursor, namespace, service_account)
        if app_env_id is None:
            return None
        settings = broker_db.get_settings(cursor, app_env_id)
    site = None
    for workload in settings.get("workloads") or []:
        if workload.get("namespace") == namespace and workload.get("serviceAccount") == service_account:
            site = workload.get("site")
            break
    return broker_tokens.issue_app_token(keys, app_env_id, site)


def register(router: "Router", services: "Services") -> None:
    """``POST /_hull/v1/app-token`` (contract section 3.3). No caller identity is
    known yet -- the service-account token in the body is what establishes one --
    so this is the one Broker route besides the JWKS document that needs no prior
    authentication.
    """
    from sites.broker.http import HttpError, Request, Response

    def app_token(request: Request) -> Response:
        body = request.body if isinstance(request.body, dict) else {}
        sa_token = body.get("serviceAccountToken")
        if not isinstance(sa_token, str) or not sa_token:
            raise HttpError(401, "invalid_workload", "serviceAccountToken is required")
        result = issue_app_token_for_workload(
            services.db, services.keys, services.reviewer, sa_token
        )
        if result is None:
            raise HttpError(
                401, "invalid_workload",
                "the service account token does not map to a known app",
            )
        token, expires_at = result
        return Response(200, {"token": token, "expiresAt": expires_at})

    router.add("POST", "/_hull/v1/app-token", app_token, auth="none")
