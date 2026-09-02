"""``/v1/tenants`` endpoints: the Handler tenant mixin.

Methods use host-handler capabilities through ``self`` (for example ``_json``,
``_read_body``, ``_require_admin``, and quota helpers). See the Handler composition in
``api.py``.
"""
from __future__ import annotations

from typing import Any

from sites.naming import new_tenant_token, token_digest
from sites.validation import (
    DEFAULT_MAX_DEPLOYMENTS,
    DEFAULT_MAX_PUBLIC_ROUTES,
    ValidationError,
    normalize_merchant_id,
    normalize_user_id,
)
from sites.http_kit import QUERY_REFUSED
from sites.serializers import iso_timestamp, positive_int
from sites.admission import reject_over_capacity
from sites.exposure import bounded_public_route_default
from sites.storage import StorageConflictError, StorageError


class TenantsMixin:
    """Tenant management endpoint (admin) and tenant self-check endpoint (self-service)."""

    def _merchant_id_from_query(
        self, query: dict[str, str], *, required: bool
    ) -> Any:
        """Read ``?merchantId=`` off an admin request.

        The user_id is only unique within the merchant, so the endpoint that locates a single row by {id} must explicitly include the merchant:
        If it's missing, it's 400, and you won't guess a default merchant - if you guess wrong, it will be silently changed to another merchant.
        Tenants of the same name, and both sides look "successful".
        Returning QUERY_REFUSED indicates that a rejection response has been written.
        """
        raw = query.get("merchantId", "").strip()
        if not raw:
            if required:
                self._json(
                    400,
                    {
                        "error": (
                            "merchantId is required; a user id is only unique "
                            "within one merchant"
                        )
                    },
                )
                return QUERY_REFUSED
            return None
        try:
            return normalize_merchant_id(raw)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return QUERY_REFUSED

    def _tenant_view(self, record: dict[str, Any]) -> dict[str, Any]:
        """Public shape of a tenant. The summary is private: it is equivalent to a validator for credentials."""
        return {
            "merchantId": record["merchant_id"],
            "userId": record["user_id"],
            "maxDeployments": int(record["max_deployments"]),
            "maxPublicRoutes": int(record["max_public_routes"]),
            "createdAt": iso_timestamp(record.get("created_at")),
            "disabledAt": iso_timestamp(record.get("disabled_at")),
        }

    def _create_tenant(self) -> None:
        if not self._require_admin():
            return
        try:
            payload = self._read_body()
            merchant_id = normalize_merchant_id(str(payload.get("merchantId", "")))
            user_id = normalize_user_id(str(payload.get("userId", "")))
            unknown = set(payload) - {
                "merchantId",
                "userId",
                "maxDeployments",
                "maxPublicRoutes",
            }
            if unknown:
                raise ValidationError(
                    "unsupported tenant fields: " + ", ".join(sorted(unknown))
                )
            max_deployments = positive_int(
                payload.get("maxDeployments", DEFAULT_MAX_DEPLOYMENTS),
                "maxDeployments",
            )
            max_public_routes = positive_int(
                payload.get(
                    "maxPublicRoutes",
                    bounded_public_route_default(DEFAULT_MAX_PUBLIC_ROUTES),
                ),
                "maxPublicRoutes",
                minimum=0,
            )
            reject_over_capacity(max_public_routes)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return

        merchant = self._existing_merchant(merchant_id)
        if merchant is None:
            return
        # max_tenants must be judged on both CCB paths: only on the JIT path, and on the admin console.
        # It becomes a backdoor to bypass merchant quotas.
        try:
            existing = self.store.count_tenants(merchant_id)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        if existing >= int(merchant["max_tenants"]):
            self._json(
                429,
                {
                    "error": (
                        f"merchant '{merchant_id}' may hold at most "
                        f"{int(merchant['max_tenants'])} tenants"
                    ),
                    "code": "merchant_tenant_quota_exceeded",
                },
            )
            return

        token = new_tenant_token()
        try:
            self.store.create_tenant(
                merchant_id,
                user_id,
                token_digest(token),
                max_deployments=max_deployments,
                max_public_routes=max_public_routes,
            )
        except StorageConflictError as exc:
            # The name is still occupied by the deactivated tenant. Just say which way to go, otherwise the caller will just
            # Repeatedly retrying a creation that never succeeds.
            self._json(
                409,
                {
                    "error": str(exc),
                    "code": "tenant_exists",
                    "hint": (
                        "POST /v1/tenants/<id>/token?merchantId=<merchant> "
                        "issues a new token and re-enables a disabled tenant"
                    ),
                },
            )
            return
        except StorageError:
            # Database failure is not a conflict: returning 409 will make the caller think that the name is occupied and give up trying again.
            # The troubleshooter is led to investigate a conflict that does not exist.
            self._json(503, {"error": "database unavailable"})
            return
        # The plaintext token only appears at this moment: there is only a digest in the library, and if it is lost, the tenant can only be rebuilt.
        self._json(
            201,
            {
                "merchantId": merchant_id,
                "userId": user_id,
                "token": token,
                "maxDeployments": max_deployments,
                "maxPublicRoutes": max_public_routes,
                "note": "store this token now; it is not recoverable",
            },
        )

    def _list_tenants(self, query: dict[str, str]) -> None:
        if not self._require_admin():
            return
        merchant_id = self._merchant_id_from_query(query, required=False)
        if merchant_id is QUERY_REFUSED:
            return
        try:
            records = self.store.list_tenants(merchant_id=merchant_id)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        self._json(
            200,
            {
                "tenants": [self._tenant_view(record) for record in records],
                "count": len(records),
            },
        )

    def _describe_self(self) -> None:
        """Let a tenant read its own quota without seeing anyone else's."""
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        quota = self._tenant_quota(merchant_id, user_id)
        if quota is None:
            return
        self._json(
            200,
            {
                "merchantId": merchant_id,
                "userId": user_id,
                "maxDeployments": quota["max_deployments"],
                "maxPublicRoutes": quota["max_public_routes"],
                "merchantMaxDeployments": quota["merchant_max_deployments"],
            },
        )

    def _patch_tenant(self, raw_name: str, query: dict[str, str]) -> None:
        """Change one tenant's quota. Only the quota is changed, the credentials and deactivation status are not touched."""
        if not self._require_admin():
            return
        merchant_id = self._merchant_id_from_query(query, required=True)
        if merchant_id is QUERY_REFUSED:
            return
        try:
            user_id = normalize_user_id(raw_name)
            payload = self._read_body()
            unknown = set(payload) - {"maxDeployments", "maxPublicRoutes"}
            if unknown:
                raise ValidationError(
                    "unsupported tenant fields: " + ", ".join(sorted(unknown))
                )
            if not payload:
                # Silent success of an empty body will make the caller think that the change has taken effect.
                raise ValidationError(
                    "maxDeployments or maxPublicRoutes is required"
                )
            max_deployments = (
                positive_int(payload["maxDeployments"], "maxDeployments")
                if "maxDeployments" in payload
                else None
            )
            max_public_routes = (
                positive_int(
                    payload["maxPublicRoutes"], "maxPublicRoutes", minimum=0
                )
                if "maxPublicRoutes" in payload
                else None
            )
            if max_public_routes is not None:
                reject_over_capacity(max_public_routes)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            if self.store.tenant(merchant_id, user_id) is None:
                self._json(404, {"error": "tenant not found"})
                return
            self.store.update_tenant_quota(
                merchant_id,
                user_id,
                max_deployments=max_deployments,
                max_public_routes=max_public_routes,
            )
            record = self.store.tenant(merchant_id, user_id)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        # The quota will only take effect during the next admission, and deployments that are already running will not be recycled - speak up, so as not to
        # After adjusting the size, the administrator thought that the excess part would disappear on its own.
        self._json(
            200,
            {
                **self._tenant_view(record),
                "note": (
                    "the new limits apply to the next admission; existing "
                    "deployments are untouched"
                ),
            },
        )

    def _rotate_tenant_token(self, raw_name: str, query: dict[str, str]) -> None:
        """Issue a new token for a tenant, and bring it back if it was disabled.

        It's intentional to have two things in one action: disable only clears disabled_at while the record is still there, the name will be
        It has always been the only constraint; and the person who will come to exchange the certificate wants this tenant to be able to use it. The old token is in
        Invalid within the same write.
        """
        if not self._require_admin():
            return
        merchant_id = self._merchant_id_from_query(query, required=True)
        if merchant_id is QUERY_REFUSED:
            return
        try:
            user_id = normalize_user_id(raw_name)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            record = self.store.tenant(merchant_id, user_id)
            if record is None:
                self._json(404, {"error": "tenant not found"})
                return
            was_disabled = record.get("disabled_at") is not None
            token = new_tenant_token()
            self.store.rotate_tenant_token(
                merchant_id, user_id, token_digest(token)
            )
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        self._json(
            200,
            {
                "merchantId": merchant_id,
                "userId": user_id,
                "token": token,
                "reenabled": was_disabled,
                "note": (
                    "store this token now; the previous one stopped working"
                ),
            },
        )

    def _disable_tenant(self, raw_name: str, query: dict[str, str]) -> None:
        if not self._require_admin():
            return
        merchant_id = self._merchant_id_from_query(query, required=True)
        if merchant_id is QUERY_REFUSED:
            return
        try:
            user_id = normalize_user_id(raw_name)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            record = self.store.tenant(merchant_id, user_id)
            if record is None:
                self._json(404, {"error": "tenant not found"})
                return
            self.store.disable_tenant(merchant_id, user_id)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        # Only the credential is revoked, but its workloads are not: those are either deleted separately or left to be taken over.
        self._json(
            202,
            {
                "merchantId": merchant_id,
                "userId": user_id,
                "disabled": True,
                "note": "credentials revoked; existing workloads are untouched",
            },
        )
