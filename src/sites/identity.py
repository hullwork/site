"""Pure authentication and credential-resolution functions.

Given request headers, a metadata store, and credential lookup functions, produce either an
authenticated identity or a refusal.

🔴 The one invariant of this module: **the merchant is decided by the credential and can never
be named by the caller.** A request may ask to act for a subject *inside* that merchant, and
only when the credential itself carries that grant. Everything else here follows from it.
"""
from __future__ import annotations

import hmac
import re
from os import getenv
from typing import Any, NamedTuple

from sites.naming import new_tenant_token, token_digest
from sites.validation import (
    DEFAULT_MERCHANT_ID,
    DEFAULT_MERCHANT_MAX_DEPLOYMENTS,
    DEFAULT_MAX_DEPLOYMENTS,
    DEFAULT_MAX_PUBLIC_ROUTES,
    ValidationError,
    normalize_user_id,
)
from sites.exposure import bounded_public_route_default
from sites.storage import StorageError
from sites import console_session
from sites import telemetry


# Identity of the break-glass admin credential. It owns one pinned tenant and cannot
# become anyone else.
DEFAULT_USER_ID = getenv("SITES_DEFAULT_USER_ID", "local") or "local"

# Cross-boundary pseudonym header. The caller derives it as
# HMAC-SHA256(salt, tenant_id + "\0" + subject_id)[:16] -> 32 lowercase hex characters
# (sites.client.acting_subject). The character set is deliberately a strict subset of
# _USER_ID_RE, so the pseudonym is a valid tenant identifier here without any further
# negotiation about identifier syntax between the two sides.
ACTING_SUBJECT_HEADER = "X-Acting-Subject"
_ACTING_SUBJECT_RE = re.compile(r"^[0-9a-f]{32}$")

# Headers a caller used to be able to declare its own identity with. They are refused
# rather than ignored: a caller that believes it is writing into merchant B while the
# credential lands in merchant A produces resources in a place nobody looks at, and the
# failure is silent. Refusing costs one round trip and is impossible to misread.
_CALLER_DECLARED_IDENTITY_HEADERS = ("X-Merchant-ID", "X-User-ID")


class Refusal(NamedTuple):
    """A determined rejection response: the Handler shell is responsible for writing it out."""

    status: int
    payload: dict[str, Any]


def service_token_from(headers: Any) -> str:
    return headers.get("X-Sites-Service-Token", "")


def acting_subject_from(headers: Any) -> str:
    return headers.get(ACTING_SUBJECT_HEADER, "").strip()


def admin_token_matches(supplied: str, service_token: str) -> bool:
    """Constant-time comparison, and an unset service token matches nothing."""
    return bool(supplied) and bool(service_token) and hmac.compare_digest(
        supplied, service_token
    )


def is_admin(
    headers: Any,
    service_token: str,
    *,
    session_key: str = "",
    local_login_enabled: bool = True,
    unsafe: bool = False,
) -> bool:
    """Whether the request carries platform-wide admin authority.

    Two sources, both first class: the break-glass service token (only while local login is
    enabled — the switch disables the authentication path itself, not just the login form),
    and an admin console session issued by the OIDC callback or by the local login endpoint.
    """
    if local_login_enabled and admin_token_matches(
        service_token_from(headers), service_token
    ):
        return True
    claims = console_session.verify(headers, session_key, unsafe=unsafe)
    return claims is not None and bool(claims.get("adm"))


def authenticate(
    headers: Any,
    store: Any,
    service_token: str,
    *,
    session_key: str = "",
    local_login_enabled: bool = True,
    unsafe: bool = False,
) -> tuple[str, str] | Refusal:
    """Resolve the (merchant, tenant) this request acts as, or the refusal.

    Four mutually exclusive credentials, each of which fully determines the merchant:
    1. Tenant token      -> the row it hashes to; it may not act for anyone else
    2. Merchant API key  -> that merchant; the subject comes from X-Acting-Subject and only
                            when the key carries ``may_act_as_subjects``
    3. Admin token       -> pinned (DEFAULT_MERCHANT_ID, DEFAULT_USER_ID), break-glass only
    4. Console session   -> the identity signed into the cookie, re-read from the database on
                            every request so unbinding or disabling takes effect at once

    The metric only records success or failure, **not which path was successful**: that is
    equivalent to publishing "this token is an admin token" as an observable signal. For the
    same reason it does not record the merchant or tenant identifier. (The accounting of
    AUTH_TOTAL is in the Handler shell of api.py. The shell knows success or failure.)
    """
    declared = _refuse_caller_declared_identity(headers)
    if declared is not None:
        return declared
    supplied = service_token_from(headers)
    if supplied:
        if local_login_enabled and admin_token_matches(supplied, service_token):
            return _identity_from_admin_token(headers, store)
        return _identity_from_credential(headers, store, supplied)
    claims = console_session.verify(headers, session_key, unsafe=unsafe)
    if claims is not None:
        return _identity_from_session(headers, store, claims)
    return Refusal(401, {"error": "invalid service token"})


def _refuse_caller_declared_identity(headers: Any) -> Refusal | None:
    for header in _CALLER_DECLARED_IDENTITY_HEADERS:
        if headers.get(header, "").strip():
            return Refusal(
                403,
                {
                    "error": (
                        f"{header} is not accepted; the merchant and tenant are "
                        "determined by the credential"
                    )
                },
            )
    return None


def _identity_from_credential(
    headers: Any, store: Any, supplied: str
) -> tuple[str, str] | Refusal:
    """The merchant API key and the tenant token go through the same process, and they are distinguished by looking up tables. Both tables must be checked.

    You must not shunt first by a prefix like MERCHANT_KEY_PREFIX: the prefix itself is a "this
    What type of bypass signal does the certificate belong to, and the failure response of the two paths after the shunt will almost certainly diverge.
    Four types of failures (admin does not match / merchant table cannot be found / expired key /
    tenant table cannot be found) must return exactly the same 401 - with just a little
    difference, this endpoint becomes a detector for merchant name and tenant name. Deactivated merchant
    It cannot be found by key (SQL has been filtered), so it also falls in the same 401.
    """
    digest = token_digest(supplied)
    try:
        merchant = store.merchant_by_api_key(digest)
        tenant = (
            None if merchant is not None else store.tenant_by_token(digest)
        )
    except StorageError:
        return Refusal(503, {"error": "database unavailable"})
    if merchant is not None:
        return _identity_from_merchant_key(headers, store, merchant)
    if tenant is None:
        return Refusal(401, {"error": "invalid service token"})
    return _identity_from_tenant_token(headers, store, tenant)


def _identity_from_merchant_key(
    headers: Any, store: Any, merchant: dict[str, Any]
) -> tuple[str, str] | Refusal:
    """(this merchant, a subject inside it). The merchant is never in question here.

    ``may_act_as_subjects`` is a property of the calling credential, exactly like Kubernetes
    impersonation: whether *you* may speak for someone else. A key without the grant that
    still sends the header is refused, not silently demoted to its own identity - the caller
    would otherwise write one subject's site into another's namespace and see 2xx.
    """
    merchant_id = str(merchant["merchant_id"])
    subject = acting_subject_from(headers)
    may_act = bool(merchant.get("may_act_as_subjects"))
    if subject and not may_act:
        return Refusal(
            403,
            {"error": "this key is not authorized to act for a subject"},
        )
    if may_act and not subject:
        # No default subject: a key whose whole purpose is acting for someone must say for
        # whom. Guessing sends the resources of a caller that forgot the header somewhere
        # else without a word.
        return Refusal(
            400,
            {"error": f"{ACTING_SUBJECT_HEADER} is required with this key"},
        )
    if subject and not _ACTING_SUBJECT_RE.fullmatch(subject):
        return Refusal(
            400,
            {
                "error": (
                    f"{ACTING_SUBJECT_HEADER} must be 32 lowercase hexadecimal "
                    "characters"
                )
            },
        )
    user_id = subject or DEFAULT_USER_ID
    admitted = admit_tenant(store, merchant, user_id)
    if isinstance(admitted, Refusal):
        return admitted
    return merchant_id, user_id


def _identity_from_tenant_token(
    headers: Any, store: Any, tenant: dict[str, Any]
) -> tuple[str, str] | Refusal:
    merchant_id = str(tenant["merchant_id"])
    # To deactivate a merchant, both paths must be closed at the same time. tenant_by_token only looks at the tenant's own disabled_at,
    # Merchant deactivation does not go through it at all - without the following sentence, DELETE /v1/merchants/<id> just
    # The merchant's key has been revoked, and almost all the requests that are actually running use the tenant token: deactivate the most common ones
    # It didn't work at all on that path, and when I tested it using the merchant key, it "really didn't work."
    merchant_record = active_merchant(store, merchant_id)
    if isinstance(merchant_record, Refusal):
        return merchant_record
    if acting_subject_from(headers):
        # A tenant token is one tenant. Acting is a merchant-level grant.
        return Refusal(
            403,
            {"error": "this token is not authorized to act for a subject"},
        )
    return merchant_id, str(tenant["user_id"])


def _identity_from_admin_token(
    headers: Any, store: Any
) -> tuple[str, str] | Refusal:
    """Break-glass admin: one pinned identity, no impersonation.

    The admin token is not an API key and carries no ``may_act_as_subjects`` row, so there is
    nothing to check the header against. Fail closed rather than treat "is admin" as an
    implicit grant to be anyone - that is the exact shape the trusted-proxy path had.
    """
    if acting_subject_from(headers):
        return Refusal(
            403,
            {"error": "the admin token is not authorized to act for a subject"},
        )
    merchant_record = active_merchant(store, DEFAULT_MERCHANT_ID)
    if isinstance(merchant_record, Refusal):
        return merchant_record
    return DEFAULT_MERCHANT_ID, DEFAULT_USER_ID


def _identity_from_session(
    headers: Any, store: Any, claims: dict[str, Any]
) -> tuple[str, str] | Refusal:
    """The console session, re-resolved against the database on every request.

    🔴 The cookie is a credential that carries a tenant, so it gets the same treatment as one:
    the merchant must still be active and the tenant row must still be there and enabled.
    Trusting the signed copy would leave a disabled tenant working for the remaining lifetime
    of a cookie that was signed before it was disabled.
    """
    if acting_subject_from(headers):
        return Refusal(
            403,
            {"error": "a console session is not authorized to act for a subject"},
        )
    if claims.get("adm"):
        merchant_record = active_merchant(store, DEFAULT_MERCHANT_ID)
        if isinstance(merchant_record, Refusal):
            return merchant_record
        return DEFAULT_MERCHANT_ID, DEFAULT_USER_ID
    merchant_id = str(claims.get("mid") or "")
    user_id = str(claims.get("uid") or "")
    try:
        user_id = normalize_user_id(user_id)
    except ValidationError:
        return Refusal(401, {"error": "invalid console session"})
    merchant_record = active_merchant(store, merchant_id)
    if isinstance(merchant_record, Refusal):
        return merchant_record
    try:
        record = store.tenant(merchant_id, user_id)
    except StorageError:
        return Refusal(503, {"error": "database unavailable"})
    if record is None or record.get("disabled_at") is not None:
        return Refusal(403, {"error": "tenant is disabled"})
    return merchant_id, user_id


def audit_acting_call(
    headers: Any, method: str, path: str, outcome: str
) -> None:
    """One line per impersonating call (contract §3.4). No-op when nobody is acting.

    ``key`` is the digest prefix rather than the plaintext prefix: it identifies the key in
    logs just as well and leaks no part of the secret into a log store that is read by more
    people than the database is.
    """
    subject = acting_subject_from(headers)
    if not subject:
        return
    supplied = service_token_from(headers)
    telemetry.log(
        "auth_acting_call",
        key=token_digest(supplied)[:12] if supplied else "",
        acting_as=subject[:32],
        route=f"{method} {path.split('?', 1)[0]}",
        outcome=outcome,
    )


def active_merchant(store: Any, merchant_id: str) -> dict[str, Any] | Refusal:
    """Merchant row, gives a 403 rejection when deactivated or does not exist.

    If the line cannot be found, press fail closed and use the same sentence: The caller who can get here
    Either you have the merchant's credentials or you are the admin, there is no new information that can be detected.
    """
    try:
        record = store.merchant(merchant_id)
    except StorageError:
        return Refusal(503, {"error": "database unavailable"})
    if record is None or record.get("disabled_at") is not None:
        return Refusal(403, {"error": "merchant is disabled"})
    return record


def existing_merchant(store: Any, merchant_id: str) -> dict[str, Any] | Refusal:
    """If the admin console locates a merchant, it will result in 404 if it does not exist. The difference with active_merchant is here
    **Unfiltered deactivation**: The admin console must be able to see and operate the deactivated merchant, otherwise deactivation will be equivalent to deactivating the merchant from
    The console disappears and there is no recovery entrance.
    """
    try:
        record = store.merchant(merchant_id)
    except StorageError:
        return Refusal(503, {"error": "database unavailable"})
    if record is None:
        return Refusal(404, {"error": "merchant not found"})
    return record


def admit_tenant(
    store: Any, merchant: dict[str, Any], user_id: str
) -> bool | Refusal:
    """Make sure the (merchant, user) line is available. Refusal indicates a refusal to respond.

    To deactivate the tenant, the merchant key path must be blocked: if only the tenant’s own token is blocked, the access to the merchant key will be deactivated.
    The caller of the merchant key did not happen.

    Tenants are still created on first use; merchants never are (contract §4). A tenant is a
    row inside a boundary that already exists and whose quota already bounds it, while a
    merchant *is* a boundary - one that appears by itself has no owner, no quota decision
    and nobody who agreed to it.
    """
    merchant_id = str(merchant["merchant_id"])
    try:
        record = store.tenant(merchant_id, user_id)
    except StorageError:
        return Refusal(503, {"error": "database unavailable"})
    if record is None:
        return register_tenant(store, merchant, user_id)
    if record.get("disabled_at") is not None:
        return Refusal(403, {"error": "tenant is disabled"})
    return True


def register_tenant(
    store: Any, merchant: dict[str, Any], user_id: str
) -> bool | Refusal:
    """When using it for the first time, add a tenant line to (merchant, user).

    The generated random token only saves the digest and is never returned: such identities continue to be accessed only by the merchant key or
    by a console session, its own token never exists in plain text.
    """
    merchant_id = str(merchant["merchant_id"])
    max_tenants = int(merchant["max_tenants"])
    try:
        if store.count_tenants(merchant_id) >= max_tenants:
            return Refusal(
                429,
                {
                    "error": (
                        f"merchant '{merchant_id}' may hold at most "
                        f"{max_tenants} tenants"
                    ),
                    "code": "merchant_tenant_quota_exceeded",
                },
            )
        store.create_tenant(
            merchant_id,
            user_id,
            token_digest(new_tenant_token()),
            max_deployments=DEFAULT_MAX_DEPLOYMENTS,
            max_public_routes=bounded_public_route_default(
                DEFAULT_MAX_PUBLIC_ROUTES
            ),
        )
    except StorageError:
        # Two first requests may race. A concurrent successful insert is
        # acceptable; any other failure remains fail closed.
        try:
            if store.tenant(merchant_id, user_id) is not None:
                return True
        except StorageError:
            pass
        return Refusal(503, {"error": "database unavailable"})
    return True


def tenant_quota(
    store: Any,
    merchant_id: str,
    user_id: str,
) -> dict[str, int] | Refusal:
    """A tenant's quota, plus the merchant quota above it. Refusal indicates a refusal to respond.

    The pinned admin identity is not in the tenant table, so give it the platform default
    value. Every other identity that reaches this point already has its row: the merchant-key
    path admits it and the console session path requires it to exist.
    """
    try:
        merchant = store.merchant(merchant_id)
        record = store.tenant(merchant_id, user_id)
    except StorageError:
        return Refusal(503, {"error": "database unavailable"})
    merchant_max = (
        int(merchant["max_deployments"])
        if merchant is not None
        else DEFAULT_MERCHANT_MAX_DEPLOYMENTS
    )
    if record is None:
        return {
            "max_deployments": DEFAULT_MAX_DEPLOYMENTS,
            "max_public_routes": bounded_public_route_default(
                DEFAULT_MAX_PUBLIC_ROUTES
            ),
            "merchant_max_deployments": merchant_max,
        }
    return {
        "max_deployments": int(record["max_deployments"]),
        "max_public_routes": int(record["max_public_routes"]),
        "merchant_max_deployments": merchant_max,
    }
