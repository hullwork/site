"""``/v1/deployments`` endpoints: the Handler deployment mixin.

The inline GET/POST/DELETE branches previously in ``api.py`` live here as methods. They use
host-handler capabilities through ``self`` and map mutation exceptions through
``_MUTATION_ERROR_RESPONSES`` in ``api.py``.
"""
from __future__ import annotations

import time

from sites.api_errors import MUTATION_ERROR_RESPONSES as _MUTATION_ERROR_RESPONSES
from sites.k8s_resources import site_deployment_resource
from sites.naming import cr_name_for
from sites.validation import ValidationError, dns_label
from sites.kube import ApiError
from sites.admission import (
    BUILD_COLLECTION_PATH,
    COLLECTION_PATH,
    CONTROL_NAMESPACE,
    ControlPlaneBusy,
    ServiceNameConflict,
    acquire_mutation_lock,
)
from sites.serializers import (
    deployment_record_response as _deployment_record_response,
    deployment_response as _deployment_response,
)
from sites.storage import StorageError


class DeploymentsMixin:
    """List/details/submit/delete of SiteDeployment (existing entry points outside of image and bundle components)."""

    def _list_deployments(self) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            records = self.store.list_deployments(
                merchant_id, user_id, limit=100
            )
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        deployments = [
            _deployment_record_response(record) for record in records
        ]
        self._json(
            200,
            {
                "merchantId": merchant_id,
                "deployments": deployments,
                "count": len(deployments),
                "snapshotAgeSeconds": (
                    self.synchronizer.snapshot_age_seconds()
                    if self.synchronizer is not None
                    else None
                ),
            },
        )

    def _get_deployment(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            service_name = dns_label(raw_name)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            record = self.store.get_deployment(
                merchant_id, user_id, service_name
            )
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        if record is None or record["deleted_at"] is not None:
            self._json(404, {"error": "deployment not found"})
            return
        try:
            name = str(record["cr_name"])
            obj = self.kube.get(f"{COLLECTION_PATH}/{name}")
        except ApiError as exc:
            if exc.status == 404:
                try:
                    self.store.set_status(
                        merchant_id,
                        user_id,
                        service_name,
                        "Deleted",
                        "SiteDeployment no longer exists",
                    )
                except StorageError:
                    pass
                self._json(404, {"error": "deployment not found"})
            else:
                self._json(502, {"error": str(exc)})
            return
        except RuntimeError as exc:
            # See /v1/builds/{name} endpoint (sites/api_builds.py) for the same treatment: Transmission failure
            # Take the bare RuntimeError.
            self._json(502, {"error": str(exc)})
            return
        try:
            self.store.upsert_site_deployment(obj)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        self._json(200, _deployment_response(obj))

    def _post_deployment(self) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        quota = self._tenant_quota(merchant_id, user_id)
        if quota is None:
            return
        try:
            desired = site_deployment_resource(
                self._read_body(),
                merchant_id,
                user_id,
                namespace=CONTROL_NAMESPACE,
            )
            self._bind_dynamic_site_version(desired)
            name = desired["metadata"]["name"]
            resource_path = f"{COLLECTION_PATH}/{name}"
            desired["metadata"]["annotations"] = {
                "sites.local/updated-at": str(int(time.time()))
            }
            desired["spec"]["revision"] = str(time.time_ns())
            # The resource package of this merchant follows CR: the operator is not connected to the database and is only known here.
            desired["spec"]["tenantQuota"] = self._merchant_quota(merchant_id)
            with acquire_mutation_lock(self.mutation_lock, self.mutation_lock_timeout):
                # SiteBuild and the SiteDeployment it produces share the same CR name. This
                # Invariants originally guarded three directions - POST /v1/builds rejected and has been built
                # Occupied names, names already occupied by deployment are also rejected, DELETE rejects directly
                # Delete the deployment generated by build - only this direction is missing.
                # The consequence of omission is not "overwriting" but "see-sawing": create_or_patch changes the CR's
                # image/nodePort is changed to the caller's, and operator is pressed again in the next round of reconcile.
                # The results of the build are patched back, the two values flip every two seconds, and the public URL is intermittent.
                # Unavailable, and the DELETE on both sides will pass the buck when the caller wants to clean up.
                try:
                    owner = self.kube.get(f"{BUILD_COLLECTION_PATH}/{name}")
                except ApiError as exc:
                    if exc.status != 404:
                        raise
                else:
                    if not (owner.get("metadata") or {}).get("deletionTimestamp"):
                        raise ServiceNameConflict(
                            "a source build already uses this service name; "
                            "delete it through /v1/builds/"
                            f"{desired['spec']['serviceName']} before deploying "
                            "an image under the same name"
                        )
                self._admit_and_assign_ports(
                    merchant_id, user_id, [desired], quota
                )
                self.store.upsert_site_deployment(
                    desired,
                    phase="Pending",
                    message="Accepted by sites-api",
                )
                try:
                    obj = self.kube.create_or_patch(
                        COLLECTION_PATH, resource_path, desired
                    )
                    try:
                        self.kube.patch(
                            f"{resource_path}/status",
                            {
                                "status": {
                                    "phase": "Pending",
                                    "message": "Accepted by sites-api",
                                }
                            },
                        )
                    except ApiError as exc:
                        if exc.status not in (404, 409):
                            raise
                except ApiError as exc:
                    self.store.set_status(
                        desired["spec"]["merchantID"],
                        desired["spec"]["userID"],
                        desired["spec"]["serviceName"],
                        "Failed",
                        "Kubernetes SiteDeployment write failed",
                    )
                    raise exc
        except Exception as exc:
            # The semantics of the ladder (status code/code/response body) converge at _MUTATION_ERROR_RESPONSES,
            # The order in which subclasses precede parent classes is encoded in the table sequence.
            if not self._respond_with_error(exc, _MUTATION_ERROR_RESPONSES):
                raise
            return

        response = _deployment_response(obj)
        response["phase"] = "Pending"
        response["message"] = "Accepted by sites-api"
        self._json(202, response)

    def _delete_deployment(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            service_name = dns_label(raw_name)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            record = self.store.get_deployment(
                merchant_id, user_id, service_name
            )
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        if record is None or record["deleted_at"] is not None:
            self._json(
                202,
                {
                    "name": cr_name_for(merchant_id, user_id, service_name),
                    "phase": "Deleting",
                },
            )
            return
        name = str(record["cr_name"])
        try:
            with acquire_mutation_lock(self.mutation_lock, self.mutation_lock_timeout):
                # A source build owns the SiteDeployment of the same name and
                # recreates it on every reconcile, so deleting it here would
                # bring the workload back within two seconds while the database
                # record flipped from Deleting back to Pending with no
                # explanation to the caller.
                owner = self.kube.get(f"{BUILD_COLLECTION_PATH}/{name}")
                if not (owner.get("metadata") or {}).get("deletionTimestamp"):
                    self._json(
                        409,
                        {
                            "error": (
                                "this service was created by a source build; "
                                f"delete it through /v1/builds/{service_name}"
                            ),
                            "code": "service_name_conflict",
                        },
                    )
                    return
        except ControlPlaneBusy as exc:
            # Before RuntimeError, of which it is a subclass: a lock timeout is a
            # retryable 503, not a 502 blamed on Kubernetes.
            self._respond_with_error(exc, _MUTATION_ERROR_RESPONSES)
            return
        except ApiError as exc:
            if exc.status != 404:
                self._json(502, {"error": str(exc)})
                return
        except RuntimeError as exc:
            self._json(502, {"error": str(exc)})
            return
        try:
            with acquire_mutation_lock(self.mutation_lock, self.mutation_lock_timeout):
                self.kube.delete(f"{COLLECTION_PATH}/{name}")
                self.store.set_status(
                    merchant_id,
                    user_id,
                    service_name,
                    "Deleting",
                    "Deletion requested through sites-api",
                )
        except ControlPlaneBusy as exc:
            self._respond_with_error(exc, _MUTATION_ERROR_RESPONSES)
            return
        except ApiError as exc:
            if exc.status != 404:
                self._json(502, {"error": str(exc)})
                return
            try:
                self.store.set_status(
                    merchant_id,
                    user_id,
                    service_name,
                    "Deleted",
                    "SiteDeployment no longer exists",
                )
            except StorageError:
                self._json(503, {"error": "database unavailable"})
                return
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        except RuntimeError as exc:
            # Must come after StorageError: StorageError is also a subclass of RuntimeError,
            # Putting it in front will suppress "Database Unavailable" from 503 to 502.
            self._json(502, {"error": str(exc)})
            return
        # serviceName is not a decoration: deployment_changed projection of server/agui.py cannot be obtained
        # It will directly return {} and be ignored by the upstream. The deletion action has been registered but no event will ever be generated - performance
        # After the agent deletes the site, the Work UI card is not updated, and no errors are reported throughout the process.
        # Bring the two pieces of identity together so that the caller does not have to go elsewhere to spell the primary key.
        self._json(
            202,
            {
                "name": name,
                "serviceName": service_name,
                "merchantId": merchant_id,
                "userId": user_id,
                "phase": "Deleting",
            },
        )
