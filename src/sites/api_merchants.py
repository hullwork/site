"""``/v1/merchants`` endpoints: the Handler merchant mixin.

Methods use host-handler capabilities through ``self`` (for example ``_json``,
``_read_body``, and ``_require_admin``). See the Handler composition in ``api.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sites.k8s_resources import normalize_tenant_quota
from sites.naming import new_merchant_api_key, token_digest
from sites.validation import (
    DEFAULT_MAX_TENANTS,
    DEFAULT_MERCHANT_KEY_TTL_SECONDS,
    DEFAULT_MERCHANT_MAX_DEPLOYMENTS,
    ValidationError,
    normalize_merchant_id,
)
from sites.kube import ApiError
from sites.serializers import display_name, iso_timestamp, positive_int
from sites.admission import COLLECTION_PATH
from sites import identity
from sites import telemetry
from sites.storage import StorageConflictError, StorageError


def _key_expiry(ttl_seconds: int) -> datetime:
    """When a key minted now stops working. Always a value, never None.

    An API key with no expiry outlives every machine it was pasted into, and nothing in the
    system ever forces the rotation that would end it.
    """
    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)


class MerchantsMixin:
    """Merchant management endpoint (all admin) and merchant resource package logging/propagation."""

    def _existing_merchant(self, merchant_id: str) -> dict[str, Any] | None:
        """If the admin console locates a merchant, it will result in 404 if it does not exist. None indicates that a rejection response has been written.

        The difference from identity.active_merchant on the authentication side is that **no filtering and deactivation** here: Admin console
        The deactivated merchant must be visible and able to operate, otherwise deactivation will be equivalent to disappearing from the console and never seen again.
        Restoration entrance.
        """
        outcome = identity.existing_merchant(self.store, merchant_id)
        if isinstance(outcome, identity.Refusal):
            self._json(outcome.status, outcome.payload)
            return None
        return outcome

    def _merchant_view(
        self,
        record: dict[str, Any],
        *,
        tenant_count: int | None = None,
        deployment_count: int | None = None,
    ) -> dict[str, Any]:
        """Public shape of a merchant.

        api_key_sha256 will never be leaked: Just like the digest of the tenant token, the digest itself is the validator -
        The person who gets it can check offline whether the guessed key is correct.
        """
        view: dict[str, Any] = {
            "merchantId": record["merchant_id"],
            "displayName": record["display_name"],
            "maxTenants": int(record["max_tenants"]),
            "maxDeployments": int(record["max_deployments"]),
            "createdAt": iso_timestamp(record.get("created_at")),
            "disabledAt": iso_timestamp(record.get("disabled_at")),
            # Both are properties of the credential, and the console has to show them: an
            # expiry nobody can see is an outage scheduled for a random Tuesday, and an
            # impersonation grant nobody can see is the audit finding.
            "keyExpiresAt": iso_timestamp(record.get("key_expires_at")),
            "mayActAsSubjects": bool(record.get("may_act_as_subjects")),
            # The resource package is echoed with the merchant: The console must be able to see the current gear before it can be changed.
            # Merchants who have not been configured are given deployment-level default values - the frontend does not need to distinguish between "not configured" and "equipped"
            # "Default value", what it wants to display is "how much this merchant can actually use".
            "tenantQuota": self._merchant_quota(record["merchant_id"]),
        }
        if tenant_count is not None:
            view["tenantCount"] = tenant_count
        if deployment_count is not None:
            view["deploymentCount"] = deployment_count
        return view

    def _list_merchants(self) -> None:
        if not self._require_admin():
            return
        try:
            records = self.store.list_merchants()
            tenants = self.store.list_tenants()
            usage = self.store.count_deployments_by_merchant()
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        tenant_counts: dict[str, int] = {}
        for tenant in tenants:
            key = str(tenant["merchant_id"])
            tenant_counts[key] = tenant_counts.get(key, 0) + 1
        merchants = [
            self._merchant_view(
                record,
                tenant_count=tenant_counts.get(str(record["merchant_id"]), 0),
                deployment_count=usage.get(str(record["merchant_id"]), 0),
            )
            for record in records
        ]
        self._json(200, {"merchants": merchants, "count": len(merchants)})

    def _create_merchant(self) -> None:
        if not self._require_admin():
            return
        try:
            payload = self._read_body()
            merchant_id = normalize_merchant_id(str(payload.get("merchantId", "")))
            unknown = set(payload) - {
                "merchantId",
                "displayName",
                "maxTenants",
                "maxDeployments",
                "mayActAsSubjects",
                "keyTtlSeconds",
            }
            if unknown:
                raise ValidationError(
                    "unsupported merchant fields: " + ", ".join(sorted(unknown))
                )
            display_name_value = display_name(
                payload.get("displayName", merchant_id)
            )
            max_tenants = positive_int(
                payload.get("maxTenants", DEFAULT_MAX_TENANTS), "maxTenants"
            )
            max_deployments = positive_int(
                payload.get("maxDeployments", DEFAULT_MERCHANT_MAX_DEPLOYMENTS),
                "maxDeployments",
            )
            may_act_as_subjects = _boolean_field(
                payload, "mayActAsSubjects", False
            )
            key_ttl_seconds = positive_int(
                payload.get("keyTtlSeconds", DEFAULT_MERCHANT_KEY_TTL_SECONDS),
                "keyTtlSeconds",
            )
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        api_key = new_merchant_api_key()
        try:
            self.store.create_merchant(
                merchant_id,
                display_name_value,
                token_digest(api_key),
                max_tenants,
                max_deployments,
                may_act_as_subjects=may_act_as_subjects,
                key_expires_at=_key_expiry(key_ttl_seconds),
            )
            record = self.store.merchant(merchant_id)
        except StorageConflictError as exc:
            self._json(409, {"error": str(exc), "code": "merchant_exists"})
            return
        except StorageError:
            # Database failure is not a conflict, see _post_tenant for the same reason for diversion.
            self._json(503, {"error": "database unavailable"})
            return
        # The plaintext key appears only at this moment. apiKeyShownOnce is an explicit signal to the frontend: let "only show
        # This time, the "warning" is asserted by the server, rather than relying on the frontend to remember this rule.
        self._json(
            201,
            {
                **self._merchant_view(record, tenant_count=0, deployment_count=0),
                "apiKey": api_key,
                "apiKeyShownOnce": True,
                "note": "store this key now; it is not recoverable",
            },
        )

    def _describe_merchant(self, raw_id: str) -> None:
        if not self._require_admin():
            return
        try:
            merchant_id = normalize_merchant_id(raw_id)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        record = self._existing_merchant(merchant_id)
        if record is None:
            return
        try:
            tenants = self.store.list_tenants(merchant_id=merchant_id)
            usage = self.store.count_deployments_by_merchant()
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        self._json(
            200,
            {
                **self._merchant_view(
                    record,
                    tenant_count=len(tenants),
                    deployment_count=usage.get(merchant_id, 0),
                ),
                "tenants": [self._tenant_view(tenant) for tenant in tenants],
            },
        )

    def _merchant_quota(self, merchant_id: str) -> dict[str, str]:
        """The resource package of this merchant is the deployment-level default value if it is not configured.

        When the library cannot be read, it will fall back to the default value instead of letting the deployment fail: this layer is the upper limit of "how many resources can be used".
        It is reasonable to give a default file ratio and refuse the entire deployment when it cannot be obtained - but the real force occurs in Kubernetes
        There, if it exceeds the limit, the Pod still cannot be built.
        """
        try:
            configured = self.store.merchant_resources(merchant_id)
        except StorageError:
            configured = None
        return normalize_tenant_quota(configured)

    def _propagate_tenant_quota(
        self, merchant_id: str, quota: dict[str, str]
    ) -> int:
        """Write the new package into all existing CRs of this merchant.

        🔴 If not propagated, the quota will jump between two numbers: ResourceQuota is Namespace level
        One copy, and every time the operator processes a site, it writes it once according to the spec of that site - the old CR belt
        When the old value and new CR have new values, they overwrite each other every round.

        Patch one by one instead of one transaction: Kubernetes does not have cross-object transactions, and what is left after failure in the middle is
        "Part of it is already a new value", and the convergence will continue the next time the quota is changed or the next deployment is performed. Return the number of changes provided
        Caller logs.
        """
        try:
            payload = self.kube.get(COLLECTION_PATH) or {}
        except (ApiError, RuntimeError):
            return 0
        changed = 0
        for item in payload.get("items") or []:
            spec = item.get("spec") or {}
            if str(spec.get("merchantID") or "") != merchant_id:
                continue
            if normalize_tenant_quota(spec.get("tenantQuota")) == quota:
                continue
            name = str((item.get("metadata") or {}).get("name") or "")
            if not name:
                continue
            try:
                self.kube.patch(
                    f"{COLLECTION_PATH}/{name}", {"spec": {"tenantQuota": quota}}
                )
            except (ApiError, RuntimeError):
                continue
            changed += 1
        return changed

    def _patch_merchant(self, raw_id: str) -> None:
        if not self._require_admin():
            return
        try:
            merchant_id = normalize_merchant_id(raw_id)
            payload = self._read_body()
            unknown = set(payload) - {
                "displayName",
                "maxTenants",
                "maxDeployments",
                "tenantQuota",
                "mayActAsSubjects",
            }
            if unknown:
                raise ValidationError(
                    "unsupported merchant fields: " + ", ".join(sorted(unknown))
                )
            if not payload:
                raise ValidationError(
                    "displayName, maxTenants, maxDeployments, mayActAsSubjects "
                    "or tenantQuota is required"
                )
            may_act_as_subjects = (
                _boolean_field(payload, "mayActAsSubjects", False)
                if "mayActAsSubjects" in payload
                else None
            )
            display_name_value = (
                display_name(payload["displayName"])
                if "displayName" in payload
                else None
            )
            max_tenants = (
                positive_int(payload["maxTenants"], "maxTenants")
                if "maxTenants" in payload
                else None
            )
            max_deployments = (
                positive_int(payload["maxDeployments"], "maxDeployments")
                if "maxDeployments" in payload
                else None
            )
            tenant_quota = None
            if "tenantQuota" in payload:
                if not isinstance(payload["tenantQuota"], dict):
                    raise ValidationError("tenantQuota must be an object")
                # Combine it with the current value and write it as a whole: when only giving half of it, the other half will keep the merchant’s current stall.
                # Not falling back to deployment defaults - which would quietly roll back the amount already raised.
                tenant_quota = normalize_tenant_quota(
                    {**self._merchant_quota(merchant_id), **payload["tenantQuota"]}
                )
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        if self._existing_merchant(merchant_id) is None:
            return
        # Changing the quota and changing the name are two independent statements in the data layer, so there are two PATCHs to change both at the same time.
        # In business, failure in the middle will inevitably leave partial updates. Quota first, name later: Quotas have mandatory consequences (it determines whether
        # cannot build deployment), the name is purely for display - among the two partial updates, "the name has not been changed after the quota takes effect" is harmless
        # That one.
        applied: list[str] = []
        try:
            if tenant_quota is not None:
                # Drop the database first and then propagate: On the other hand, successful propagation but failure to drop the database will result in new values in the cluster.
                # The old value is in the library - the next deployment will write the old value back to CR, and the quota will change back by itself.
                self.store.set_merchant_resources(merchant_id, tenant_quota)
                applied.append("tenantQuota")
                changed = self._propagate_tenant_quota(merchant_id, tenant_quota)
                telemetry.log(
                    "tenant_quota_updated",
                    merchant_id=merchant_id,
                    propagated_crs=changed,
                    **tenant_quota,
                )
            # Quota and rename are combined into one UPDATE (single statement natural atomic): calling it twice was
            # One of the sources of the "half-changed" status.
            if (
                max_tenants is not None
                or max_deployments is not None
                or display_name_value is not None
                or may_act_as_subjects is not None
            ):
                self.store.update_merchant(
                    merchant_id,
                    max_tenants=max_tenants,
                    max_deployments=max_deployments,
                    display_name=display_name_value,
                    may_act_as_subjects=may_act_as_subjects,
                )
                applied.extend(
                    field
                    for field, value in (
                        ("maxTenants", max_tenants),
                        ("maxDeployments", max_deployments),
                        ("displayName", display_name_value),
                        ("mayActAsSubjects", may_act_as_subjects),
                    )
                    if value is not None
                )
            record = self.store.merchant(merchant_id)
        except StorageError:
            if applied:
                # The part that has been dropped cannot be reported as "nothing changed": the administrator should act according to that conclusion.
                # During other operations, Curry has actually changed to half of his state. PATCH is idempotent, so
                # It can be converged by resending it as it is.
                self._json(
                    503,
                    {
                        "error": (
                            "the change was applied only in part; resend the "
                            "same request or reload to confirm"
                        ),
                        "code": "partial_update",
                        "applied": applied,
                    },
                )
                return
            self._json(503, {"error": "database unavailable"})
            return
        self._json(
            200,
            {
                **self._merchant_view(record),
                "note": (
                    "the new limits apply to the next admission; existing "
                    "deployments are untouched"
                ),
            },
        )

    def _rotate_merchant_key(self, raw_id: str) -> None:
        """Issue a new API key, and bring the merchant back if it was disabled.

        The same reason as _rotate_tenant_token: someone comes to sign a new certificate for this merchant, indicating that it should
        is alive; without this path, deactivation is irreversible.
        """
        if not self._require_admin():
            return
        try:
            merchant_id = normalize_merchant_id(raw_id)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        record = self._existing_merchant(merchant_id)
        if record is None:
            return
        was_disabled = record.get("disabled_at") is not None
        api_key = new_merchant_api_key()
        expires_at = _key_expiry(DEFAULT_MERCHANT_KEY_TTL_SECONDS)
        try:
            self.store.rotate_merchant_key(
                merchant_id, token_digest(api_key), expires_at
            )
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        self._json(
            200,
            {
                "merchantId": merchant_id,
                "apiKey": api_key,
                "apiKeyShownOnce": True,
                "reenabled": was_disabled,
                # Rotation is where the lifetime restarts, so the answer has to state it:
                # otherwise the only way to learn when the new key dies is to read the row.
                "keyExpiresAt": iso_timestamp(expires_at),
                "note": "store this key now; the previous one stopped working",
            },
        )

    def _disable_merchant(self, raw_id: str) -> None:
        if not self._require_admin():
            return
        try:
            merchant_id = normalize_merchant_id(raw_id)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        if self._existing_merchant(merchant_id) is None:
            return
        try:
            self.store.disable_merchant(merchant_id)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        # Disable and close two paths: the merchant key cannot be found immediately (SQL filtering), and the tenant's own
        # The token was also rejected (identity._identity_from_tenant_token check this table again).
        # Without deleting a single row of data, it can be restored by rotating the key.
        self._json(
            202,
            {
                "merchantId": merchant_id,
                "disabled": True,
                "note": (
                    "the merchant key and every tenant token under it stopped "
                    "working; existing workloads are untouched"
                ),
            },
        )


def _boolean_field(payload: dict[str, Any], name: str, default: bool) -> bool:
    """Read a strict boolean. A string "false" is a request error, not a truthy value."""
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be true or false")
    return value
