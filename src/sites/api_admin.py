"""``/v1/admin/*`` endpoints: the Handler admin mixin.

This mixin provides the cross-merchant read-only overview and health APIs. Every endpoint
requires a platform admin token; ``_require_admin`` is intentionally the first statement
in each method. See individual methods for the authorization rationale.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

from sites.builds import registry_manifest_digest, site_build_response
from sites.k8s_resources import CONTROL_PLANE_PROBE_NAME
from sites.validation import ValidationError
from sites.http_kit import QUERY_REFUSED as _QUERY_REFUSED
from sites.kube import ApiError
from sites.serializers import deployment_record_response, parse_iso, positive_int
from sites.admission import (
    BUILD_COLLECTION_PATH,
    COLLECTION_PATH,
    CONTROL_NAMESPACE,
    list_items,
)
from sites.registry_client import (
    REGISTRY_REPOSITORY_RE,
    REGISTRY_TAG_RE,
    registry_get,
)
from sites.storage import StorageError
from sites.monitoring import application_metrics, cluster_metrics
from sites import grafana_proxy


# /v1/admin/images can send up to this many list queries to the registry at a time, and the entire query has a wall clock
# upper limit. The number of repositorys and tags are both determined by the registry. If you only block the number of times but not the time, you will still be blocked by a slow registry.
# Drag it into an admin page that doesn't return for a few minutes - and the administrator comes to see it when it's slow.
_IMAGE_DIGEST_LOOKUPS = 100
_IMAGE_DIGEST_DEADLINE_SECONDS = 5.0


class AdminMixin:
    """Cross-merchant deployment overview, control plane health page and image list."""

    def _admin_deployments(self, query: dict[str, str]) -> None:
        """Overview of cross-merchant and cross-tenant deployment.

        🔴 This is the only call point of list_all_deployments in the entire process, and it is the only one without
        Tenant filtered query. Once it leaks into any processor reachable by the tenant, it will bring the full platform deployment list to
        Any tenant - so _require_admin must be the first statement of this method.
        """
        if not self._require_admin():
            return
        merchant_id = self._merchant_id_from_query(query, required=False)
        if merchant_id is _QUERY_REFUSED:
            return
        phase = query.get("phase", "").strip() or None
        try:
            limit = positive_int(query.get("limit", 200), "limit")
            if limit > 200:
                raise ValidationError("limit cannot exceed 200")
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            records = self.store.list_all_deployments(
                merchant_id=merchant_id, phase=phase, limit=limit
            )
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        deployments = [
            deployment_record_response(record) for record in records
        ]
        self._json(
            200,
            {
                "deployments": deployments,
                "count": len(deployments),
                "snapshotAgeSeconds": (
                    self.synchronizer.snapshot_age_seconds()
                    if self.synchronizer is not None
                    else None
                ),
            },
        )

    def _admin_health(self) -> None:
        """Physical examination of the control plane itself.

        Failure of any one of them cannot make the entire response become 5xx: This is what the administrator looks at when there is a problem
        Page, if one detection fails, the entire 500 is equivalent to closing the only diagnostic entrance. Each way detects its own
        Avoid errors and shorten the timeout, lest a stuck dependency drag this endpoint into a timeout.
        """
        if not self._require_admin():
            return
        self._json(
            200,
            {
                "database": self._probe_database(),
                "operator": self._probe_operator(),
                "registry": self._probe_registry(),
                "kubernetes": self._probe_kubernetes(),
                # Whether an embedded observability panel exists. It rides along
                # on the admin health call the console already makes, rather
                # than adding a route: the tab is an operator view (the metrics
                # behind it have no tenant dimension) and this response is
                # already admin-only. `enabled: false` is the normal case for a
                # deployment without Grafana, and the console hides the tab.
                "grafana": grafana_proxy.capabilities(
                    grafana_proxy.load_config()
                ),
            },
        )

    def _admin_cluster_metrics(self, query: dict[str, str]) -> None:
        if not self._require_admin():
            return
        try:
            response = cluster_metrics(query.get("range"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, response)

    def _admin_application_metrics(self, query: dict[str, str]) -> None:
        if not self._require_admin():
            return
        try:
            response = application_metrics(
                query.get("merchantId", ""),
                query.get("userId", ""),
                query.get("serviceName", ""),
                query.get("range"),
            )
        except (ValidationError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, response)

    def _probe_database(self) -> dict[str, Any]:
        try:
            self.store.ping()
        except StorageError as exc:
            return {"reachable": False, "error": str(exc)}
        return {
            "reachable": True,
            "backend": self.store.backend,
            "snapshotAgeSeconds": (
                self.synchronizer.snapshot_age_seconds()
                if self.synchronizer is not None
                else None
            ),
        }

    def _probe_operator(self) -> dict[str, Any]:
        """Whether the operator is up, and how long ago it last wrote a status.

        reachable from operator's own Deployment(sites-api) Role added for this
        apps/v1 deployments), not deduced from the CR timestamp - deduced
        The meaning of reachable will drift to "I can read a certain signal", and when there is no CR in the cluster, it will be followed by
        "Everything is OK" is indistinguishable on the page.

        lastReconcileSeconds is still given by the timestamp written by the operator on CR, because
        Deployment only says that the process is alive, not that it is still converging. It is null when there is no CR: that is
        "No observable convergence" is not "convergence normal".
        """
        try:
            deployment = self.kube.get(
                f"/apis/apps/v1/namespaces/{CONTROL_NAMESPACE}"
                f"/deployments/{CONTROL_PLANE_PROBE_NAME}"
            )
        except (ApiError, RuntimeError) as exc:
            return {"reachable": False, "error": str(exc)}
        status = deployment.get("status") or {}
        ready = int(status.get("readyReplicas") or 0)
        if ready < 1:
            return {
                "reachable": False,
                "error": (
                    f"{CONTROL_PLANE_PROBE_NAME} has {ready} ready replica(s)"
                ),
            }
        return {
            "reachable": True,
            "readyReplicas": ready,
            "lastReconcileSeconds": self._last_reconcile_seconds(),
        }

    def _last_reconcile_seconds(self) -> float | None:
        """Seconds since the operator last wrote a status onto any CR."""
        try:
            items = list_items(self.kube, COLLECTION_PATH) + list_items(
                self.kube, BUILD_COLLECTION_PATH
            )
        except (ApiError, RuntimeError):
            return None
        newest: dt.datetime | None = None
        for item in items:
            status = item.get("status") or {}
            for key in ("checkedAt", "startedAt"):
                moment = parse_iso(status.get(key))
                if moment is not None and (newest is None or moment > newest):
                    newest = moment
        if newest is None:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        return round(max(0.0, (now - newest).total_seconds()), 1)

    def _probe_registry(self) -> dict[str, Any]:
        try:
            catalog = registry_get("/v2/_catalog")
        except RuntimeError as exc:
            return {"reachable": False, "error": str(exc)}
        repositories = catalog.get("repositories")
        return {
            "reachable": True,
            "repositoryCount": (
                len(repositories) if isinstance(repositories, list) else 0
            ),
        }

    def _probe_kubernetes(self) -> dict[str, Any]:
        try:
            version = self.kube.get("/version")
        except (ApiError, RuntimeError) as exc:
            return {"reachable": False, "error": str(exc)}
        return {
            "reachable": True,
            "version": str(version.get("gitVersion") or ""),
        }

    def _admin_builds(self, query: dict[str, str]) -> None:
        if not self._require_admin():
            return
        # The merchant dropdown in the console keeps sending ?merchantId=, but this method didn’t even accept the query before:
        # The filter is silently ignored on the server side and the form does not move - the administrator will read it as "This merchant is
        # There are these builds". Use the same value path as _admin_deployments.
        merchant_id = self._merchant_id_from_query(query, required=False)
        if merchant_id is _QUERY_REFUSED:
            return
        try:
            items = list_items(self.kube, BUILD_COLLECTION_PATH)
        except (ApiError, RuntimeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        builds = []
        for item in sorted(
            items, key=lambda build: str((build.get("metadata") or {}).get("name", ""))
        ):
            spec = item.get("spec") or {}
            if merchant_id is not None and spec.get("merchantID") != merchant_id:
                continue
            # The two identities are now given together by site_build_response, and are no longer filled in manually here.
            # userId - Having two copies of the same fact is where it starts to rot.
            builds.append(site_build_response(item))
        self._json(200, {"builds": builds, "count": len(builds)})

    def _admin_images(self) -> None:
        if not self._require_admin():
            return
        try:
            catalog = registry_get("/v2/_catalog")
        except RuntimeError as exc:
            # Registry unreachability should not cause the entire page to fail. The console uses Promise.all to concurrently pull this page
            # There are several data sources. Returning 503 here will cause the entire Promise chain to reject, and the original normal
            # The build list is cleared together. Contract (AdminImageListResponse of console/src/types.ts)
            # What is written is "repositories are empty, the reason is explained by the registry field", and 200 is returned accordingly.
            self._json(
                200,
                {
                    "repositories": [],
                    "count": 0,
                    "registry": {"reachable": False, "error": str(exc)},
                },
            )
            return
        repositories = sorted(
            name
            for name in (catalog.get("repositories") or [])
            if isinstance(name, str) and REGISTRY_REPOSITORY_RE.fullmatch(name)
        )
        budget = _IMAGE_DIGEST_LOOKUPS
        deadline = time.monotonic() + _IMAGE_DIGEST_DEADLINE_SECONDS
        truncated = False
        # The field names are based on the frontend contract: the outer layer is repositories, and the name field of each item is name.
        # The server-side internal variable is called repositories (the original field name of the registry catalog) and is sent externally.
        # images, is the reason why this endpoint is always empty on the console - both ends are self-consistent, but they just don't match up.
        views: list[dict[str, Any]] = []
        for repository in repositories:
            entry: dict[str, Any] = {"name": repository, "tags": []}
            # Repository traversal is also subject to wall clock constraints. Each tags/list is executed with a timeout of 3 seconds.
            # For requests, only the number of blocks for summary queries cannot stop "many slow repositorys" - that's exactly what this endpoint says
            # The form that Deadline wanted to defend against only blocked the second half.
            if time.monotonic() >= deadline:
                truncated = True
                entry["error"] = (
                    "registry listing stopped: query budget exhausted"
                )
                views.append(entry)
                continue
            try:
                listed = registry_get(f"/v2/{repository}/tags/list")
            except RuntimeError as exc:
                entry["error"] = str(exc)
                views.append(entry)
                continue
            tags = sorted(
                tag
                for tag in (listed.get("tags") or [])
                if isinstance(tag, str) and REGISTRY_TAG_RE.fullmatch(tag)
            )
            for tag in tags:
                digest = None
                if budget > 0 and time.monotonic() < deadline:
                    budget -= 1
                    try:
                        digest = registry_manifest_digest(repository, tag)
                    except RuntimeError:
                        digest = None
                else:
                    truncated = True
                entry["tags"].append({"tag": tag, "digest": digest})
            views.append(entry)
        self._json(
            200,
            {
                "repositories": views,
                "count": len(views),
                # The number of repositorys and tags is determined by the registry, and there is an upper limit for summary query. Cut it off and say it,
                # Otherwise a null digest will be read as "this tag is broken".
                "digestsTruncated": truncated,
            },
        )
