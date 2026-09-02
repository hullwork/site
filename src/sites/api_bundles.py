"""``/v1/bundles`` endpoints: the Handler multi-component bundle mixin.

Methods use host-handler capabilities through ``self`` (for example ``_json``,
``_read_body``, authentication, quota, and port admission). See the Handler composition in
``api.py``; mutation exceptions map through ``_MUTATION_ERROR_RESPONSES``.
"""
from __future__ import annotations

import time
from typing import Any

from sites.api_errors import MUTATION_ERROR_RESPONSES as _MUTATION_ERROR_RESPONSES
from sites.k8s_resources import bundle_resources
from sites.validation import ValidationError, dns_label
from sites.kube import ApiError
from sites.admission import (
    BUILD_COLLECTION_PATH,
    COLLECTION_PATH,
    CONTROL_NAMESPACE,
    ControlPlaneBusy,
    ServiceNameConflict,
    acquire_mutation_lock,
    owned_by as _owned_by,
)
from sites.serializers import bundle_response as _bundle_response
from sites.storage import StorageError


class BundlesMixin:
    """bundle endpoint; see _reject_foreign_component_names for a check on unique ownership of component names."""

    def _bundle_objects(
        self,
        merchant_id: str,
        user_id: str,
        bundle_name: str,
    ) -> list[dict[str, Any]]:
        collection = self.kube.get(COLLECTION_PATH)
        items = collection.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError("Kubernetes SiteDeployment collection is invalid")
        return [
            item
            for item in items
            if _owned_by(item.get("spec") or {}, merchant_id, user_id)
            and (item.get("spec") or {}).get("bundleName") == bundle_name
        ]

    def _get_bundle(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            bundle_name = dns_label(raw_name)
            objects = self._bundle_objects(
                merchant_id, user_id, bundle_name
            )
            if not objects:
                self._json(404, {"error": "bundle not found"})
                return
            for obj in objects:
                self.store.upsert_site_deployment(obj)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        except (ApiError, RuntimeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(200, _bundle_response(bundle_name, objects))

    def _reject_foreign_component_names(
        self,
        desired_resources: list[dict[str, Any]],
        bundle_name: str,
    ) -> None:
        """Make sure that these component names are not occupied by other owners.

        Constraint: The caller must already hold mutation_lock - this is a "check before write", check and
        No other writes can be inserted between subsequent create_or_patch.

        Bundles with the same name are allowed: repeated submission of the same bundle is an idempotent update, not a conflict.
        """
        for desired in desired_resources:
            name = desired["metadata"]["name"]
            service_name = desired["spec"]["serviceName"]
            try:
                existing = self.kube.get(f"{COLLECTION_PATH}/{name}")
            except ApiError as exc:
                if exc.status != 404:
                    raise
            else:
                metadata = existing.get("metadata") or {}
                owner = str((existing.get("spec") or {}).get("bundleName") or "")
                if not metadata.get("deletionTimestamp") and owner != bundle_name:
                    held_by = (
                        f"bundle {owner!r}" if owner else "a standalone deployment"
                    )
                    raise ServiceNameConflict(
                        f"component {service_name!r} is already held by "
                        f"{held_by}; delete it there before claiming the name"
                    )
            try:
                build = self.kube.get(f"{BUILD_COLLECTION_PATH}/{name}")
            except ApiError as exc:
                if exc.status != 404:
                    raise
            else:
                if not (build.get("metadata") or {}).get("deletionTimestamp"):
                    raise ServiceNameConflict(
                        f"component {service_name!r} is already held by a "
                        f"source build; delete it through /v1/builds/"
                        f"{service_name} before claiming the name"
                    )

    def _post_bundle(self) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        quota = self._tenant_quota(merchant_id, user_id)
        if quota is None:
            return
        try:
            payload = self._read_body()
            bundle_name = dns_label(str(payload.get("name", "")))
            unknown = set(payload) - {"name", "components"}
            if unknown:
                raise ValidationError(
                    "unsupported bundle fields: "
                    + ", ".join(sorted(unknown))
                )
            desired_resources = bundle_resources(
                bundle_name,
                payload.get("components"),
                merchant_id,
                user_id,
                namespace=CONTROL_NAMESPACE,
            )
            revision_base = str(time.time_ns())
            objects: list[dict[str, Any]] = []
            with acquire_mutation_lock(self.mutation_lock, self.mutation_lock_timeout):
                # Component names are mapped to CR names, so a component name can only belong to one owner at a time.
                # Without this check, submitting a component with the same name as another bundle will not result in an error, but
                # Rewrite the bundleName and tag of that CR to your own - the old bundle because
                # _bundle_objects are filtered by bundleName and completely disappear from the API (404),
                # But its workload is still running, still occupying quota and NodePort, and even DELETE is recycled
                # No. The names occupied by independent deployments and source code builds are the same: taking over them silently will only get
                # Two owners repeatedly overwrite each other for the same CR.
                self._reject_foreign_component_names(
                    desired_resources, bundle_name
                )
                self._admit_and_assign_ports(
                    merchant_id, user_id, desired_resources, quota
                )
                for index, desired in enumerate(desired_resources):
                    name = desired["metadata"]["name"]
                    resource_path = f"{COLLECTION_PATH}/{name}"
                    desired["metadata"]["annotations"] = {
                        "sites.local/updated-at": str(int(time.time())),
                        "sites.local/bundle": bundle_name,
                    }
                    desired["spec"]["revision"] = (
                        f"{revision_base}-{index}"
                    )
                    self.store.upsert_site_deployment(
                        desired,
                        phase="Pending",
                        message=f"Accepted as {bundle_name} component",
                    )
                    obj = self.kube.create_or_patch(
                        COLLECTION_PATH, resource_path, desired
                    )
                    try:
                        self.kube.patch(
                            f"{resource_path}/status",
                            {
                                "status": {
                                    "phase": "Pending",
                                    "message": (
                                        f"Accepted as {bundle_name} component"
                                    ),
                                }
                            },
                        )
                    except ApiError as exc:
                        if exc.status not in (404, 409):
                            raise
                    objects.append(obj)
        except Exception as exc:
            # The semantics of the ladder (same table as POST /v1/deployments) converge at
            # _MUTATION_ERROR_RESPONSES.
            if not self._respond_with_error(exc, _MUTATION_ERROR_RESPONSES):
                raise
            return
        self._json(
            202,
            _bundle_response(bundle_name, objects, force_pending=True),
        )

    def _delete_bundle(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            bundle_name = dns_label(raw_name)
            objects = self._bundle_objects(merchant_id, user_id, bundle_name)
            with acquire_mutation_lock(self.mutation_lock, self.mutation_lock_timeout):
                for obj in objects:
                    spec = obj.get("spec") or {}
                    name = str((obj.get("metadata") or {}).get("name", ""))
                    if not name:
                        continue
                    try:
                        self.kube.delete(f"{COLLECTION_PATH}/{name}")
                    except ApiError as exc:
                        if exc.status != 404:
                            raise
                    self.store.set_status(
                        merchant_id,
                        user_id,
                        str(spec.get("serviceName", "")),
                        "Deleting",
                        "Bundle deletion requested through sites-api",
                    )
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        except ControlPlaneBusy as exc:
            # Before RuntimeError, of which it is a subclass: a lock timeout is a
            # retryable 503, not a 502 blamed on Kubernetes.
            self._respond_with_error(exc, _MUTATION_ERROR_RESPONSES)
            return
        except (ApiError, RuntimeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(
            202,
            {
                "merchantId": merchant_id,
                "name": bundle_name,
                "phase": "Deleting",
                "components": len(objects),
            },
        )
