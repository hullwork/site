"""``/v1/builds`` endpoints: the Handler source-build mixin.

Methods use host-handler capabilities through ``self`` (for example ``_json``,
``_read_body``, ``_authenticate``, and quota helpers). See the Handler composition in
``api.py``; build exceptions map through ``_BUILD_ERROR_RESPONSES``.
"""
from __future__ import annotations

import time

from sites.admission import (
    BUILD_COLLECTION_PATH,
    COLLECTION_PATH,
    CONTROL_NAMESPACE,
    MAX_ACTIVE_BUILDS,
    BuildCapacityExceeded,
    BuildNameExists,
    ControlPlaneBusy,
    ServiceNameConflict,
    acquire_mutation_lock,
    list_items,
)
from sites.admission import (
    active_build_count as _active_build_count,
)
from sites.admission import (
    owned_by as _owned_by,
)
from sites.api_errors import BUILD_ERROR_RESPONSES as _BUILD_ERROR_RESPONSES
from sites.builds import (
    SOURCE_REQUEST_MAX_BYTES,
    normalize_source_payload,
    persist_source,
    remove_source,
    site_build_resource,
    site_build_response,
)
from sites.kube import ApiError
from sites.naming import cr_name_for
from sites.validation import ValidationError, dns_label


class BuildsMixin:
    """SiteBuild's three tenant endpoints; see each method comment for name ownership invariants."""

    def _get_build(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            service_name = dns_label(raw_name)
            name = cr_name_for(merchant_id, user_id, service_name)
            build = self.kube.get(f"{BUILD_COLLECTION_PATH}/{name}")
            if not _owned_by(build.get("spec") or {}, merchant_id, user_id):
                self._json(404, {"error": "build not found"})
                return
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        except ApiError as exc:
            if exc.status == 404:
                self._json(404, {"error": "build not found"})
            else:
                self._json(502, {"error": str(exc)})
            return
        except RuntimeError as exc:
            # kube.py throws **bare RuntimeError** for URLError/TimeoutError, and
            # ApiError is its subclass - the parent class cannot catch it, just write except ApiError
            # If kube-apiserver is unreachable, an uncaught exception will be thrown, BaseHTTPRequestHandler
            # If the connection is closed without writing a response, the caller will get an Empty reply instead of 502.
            # client.py will classify it as sites_unreachable and lose the retryable semantics.
            self._json(502, {"error": str(exc)})
            return
        self._json(200, site_build_response(build))

    def _post_build(self) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        quota = self._tenant_quota(merchant_id, user_id)
        if quota is None:
            return
        source_path = ""
        created = False
        try:
            bundle = normalize_source_payload(
                self._read_body(SOURCE_REQUEST_MAX_BYTES), merchant_id, user_id
            )
            name = cr_name_for(merchant_id, user_id, bundle.service_name)
            desired_route = {
                "metadata": {"name": name},
                "spec": {"exposure": "public"},
            }
            with acquire_mutation_lock(self.mutation_lock, self.mutation_lock_timeout):
                # Determine the name's ownership first and then its capacity: a name occupied by another object is a more fundamental rejection.
                # Reporting insufficient quotas or ports will lead the caller to the wrong path of deleting other deployments.
                builds = list_items(self.kube, BUILD_COLLECTION_PATH)
                existing = next(
                    (
                        item
                        for item in builds
                        if str((item.get("metadata") or {}).get("name", "")) == name
                    ),
                    None,
                )
                if existing is not None:
                    if not (existing.get("metadata") or {}).get("deletionTimestamp"):
                        raise BuildNameExists(
                            "a source build with this service name already exists; "
                            "delete it before submitting a replacement"
                        )
                elif not bundle.build_only:
                    # No SiteBuild owns this name, so a SiteDeployment still carrying
                    # it was created through POST /v1/deployments. Both objects
                    # are keyed by cr_name_for(user, service), so accepting the
                    # build here would silently hand that workload over on the
                    # operator's next pass.
                    try:
                        occupied = self.kube.get(f"{COLLECTION_PATH}/{name}")
                    except ApiError as exc:
                        if exc.status != 404:
                            raise
                    else:
                        if not (occupied.get("metadata") or {}).get(
                            "deletionTimestamp"
                        ):
                            raise ServiceNameConflict(
                                "a deployment already uses this service name; "
                                "delete it through /v1/deployments before "
                                "building a service with the same name"
                            )
                if not bundle.build_only:
                    self._admit_and_assign_ports(
                        merchant_id, user_id, [desired_route], quota
                    )
                active = _active_build_count(builds)
                if active >= MAX_ACTIVE_BUILDS:
                    raise BuildCapacityExceeded(
                        f"{active} builds are still running and the local build "
                        f"plane accepts {MAX_ACTIVE_BUILDS}; retry once one of "
                        "them finishes"
                    )
                source_path = persist_source(bundle)
                desired = site_build_resource(
                    bundle,
                    source_path,
                    namespace=CONTROL_NAMESPACE,
                    revision=str(time.time_ns()),
                    node_port=(
                        int(desired_route["spec"]["nodePort"])
                        if not bundle.build_only
                        and "nodePort" in desired_route["spec"]
                        else None
                    ),
                )
                desired["metadata"]["annotations"] = {
                    "sites.local/updated-at": str(int(time.time()))
                }
                build = self.kube.create(BUILD_COLLECTION_PATH, desired)
                created = True
                try:
                    self.kube.patch(
                        f"{BUILD_COLLECTION_PATH}/{name}/status",
                        {
                            "status": {
                                "phase": "Pending",
                                "message": "Accepted by sites-api",
                                "ready": False,
                            }
                        },
                    )
                except ApiError as exc:
                    if exc.status not in (404, 409):
                        raise
        except OSError as exc:
            # str(exc) carries the absolute path of the sources PVC, and a
            # context the caller built wrong is a 4xx: a 5xx tells clients the
            # server broke and invites a retry that will fail identically.
            # (Catch it here before RuntimeError, because it needs to write a log first and then return to the fixed copy.)
            self.log_message("source materialization failed: %s", exc)
            self._json(
                400,
                {"error": "source context could not be written to the build volume"},
            )
            return
        except Exception as exc:
            # The semantics of the ladder converge at _BUILD_ERROR_RESPONSES (the one with /v1/deployments
            # For differences in tables, see the notes in their definitions).
            if not self._respond_with_error(exc, _BUILD_ERROR_RESPONSES):
                raise
            return
        finally:
            # The failed path uniformly recycles the source code directory that has been downloaded, and no longer copies it in each except.
            # The condition "clean only if not created successfully" has hard semantics: created=True after CR has been assigned
            # This directory needs to be read when building the operator - deleting it at this time is equivalent to tearing down the one just taken over.
            # Build; when the successful path reaches this point, created must be True, which is a natural no-op.
            if source_path and not created:
                remove_source(source_path)
        response = site_build_response(build)
        response.update({"phase": "Pending", "message": "Accepted by sites-api"})
        self._json(202, response)

    def _delete_build(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        name = ""
        try:
            service_name = dns_label(raw_name)
            name = cr_name_for(merchant_id, user_id, service_name)
            build = self.kube.get(f"{BUILD_COLLECTION_PATH}/{name}")
            if not _owned_by(build.get("spec") or {}, merchant_id, user_id):
                self._json(404, {"error": "build not found"})
                return
            with acquire_mutation_lock(self.mutation_lock, self.mutation_lock_timeout):
                self.kube.delete(f"{BUILD_COLLECTION_PATH}/{name}")
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        except ControlPlaneBusy as exc:
            # Before RuntimeError, of which it is a subclass: a lock timeout is a
            # retryable 503, not a 502 blamed on Kubernetes.
            self._respond_with_error(exc, _BUILD_ERROR_RESPONSES)
            return
        except ApiError as exc:
            if exc.status != 404:
                self._json(502, {"error": str(exc)})
                return
        except RuntimeError as exc:
            self._json(502, {"error": str(exc)})
            return
        # The same reason as _delete_deployment: agui's when serviceName is missing
        # The deployment_changed projection silently discards this deletion.
        self._json(
            202,
            {
                "name": name or cr_name_for(merchant_id, user_id, raw_name),
                "serviceName": service_name,
                "merchantId": merchant_id,
                "userId": user_id,
                "phase": "Deleting",
            },
        )
