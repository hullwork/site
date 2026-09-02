"""Database snapshot synchronization and dependency-health signals.

Both classes continuously observe Kubernetes from background threads. Snapshot sync makes
real API calls, writes the database snapshot, and doubles as a dependency heartbeat; health
state is derived from those observations rather than unrelated process liveness.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sites.admission import COLLECTION_PATH
from sites.kube import KubeClient
from sites.static_artifacts import static_source_path_from_uri
from sites.storage import Store
from sites.validation import STATIC_IMAGE
from sites import telemetry
# This module is the same as the endpoint mixin, and is used after being assembled by sites.api; metric singleton
# (SYNC_TOTAL/SYNC_AGE/DEPENDENCY_UP/KUBERNETES_HEALTH) registered in api.py,
# Here, module attribute access is used instead of from-import, ensuring patch("sites.api.*")
# Neither the index registration order nor the index registration order will be affected by the split. First import sites.sync and then import sites.api
# Will fail due to assembly order - api is the composition root, don't get around it.
from sites import api


class DependencyHealth:
    """Record the success or failure of the latest **actual use** of a downstream dependency.

    Deliberately not doing active detection: The detection takes a path that only probes can take. If it passes, it does not mean business.
    path (and vice versa), and readinessProbe will give apiserver a blank every 2 seconds
    Add a load. The signal here comes from the real K8s call of each round of the synchronization thread - with a stable heartbeat,
    And it reflects the path that the business really depends on.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._ok: bool | None = None
        self._at: float | None = None
        self._error: str | None = None
        api.DEPENDENCY_UP.set(0.0, name)

    def record_success(self) -> None:
        with self._lock:
            self._ok, self._at, self._error = True, time.monotonic(), None
        api.DEPENDENCY_UP.set(1.0, self.name)

    def record_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._ok = False
            self._at = time.monotonic()
            self._error = f"{type(exc).__name__}: {exc}"[:200]
        api.DEPENDENCY_UP.set(0.0, self.name)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ok, at, error = self._ok, self._at, self._error
        payload: dict[str, Any] = {"ok": bool(ok)}
        if ok is None:
            # Never used ≠ broken. This is the state between startup and the first round of synchronization.
            # Reporting ok:false will make the newly started process appear to be faulty.
            payload["observed"] = False
            return payload
        payload["observed"] = True
        payload["ageSeconds"] = round(max(0.0, time.monotonic() - (at or 0.0)), 3)
        if error:
            payload["error"] = error
        return payload


class DatabaseSynchronizer:
    def __init__(
        self,
        kube: KubeClient,
        store: Store,
        mutation_lock: threading.Lock,
        *,
        interval: float = 2.0,
    ):
        self.kube = kube
        self.store = store
        self.mutation_lock = mutation_lock
        self.interval = interval
        self._clock_lock = threading.Lock()
        self._last_success: float | None = None

    def snapshot_age_seconds(self) -> float | None:
        """Seconds since the database snapshot last matched Kubernetes.

        None means no sync has ever completed. Callers use this to tell a
        frozen snapshot from an idle one; see the run() comment below.
        """
        with self._clock_lock:
            last = self._last_success
        if last is None:
            return None
        return round(max(0.0, time.monotonic() - last), 1)

    def sync_once(self) -> None:
        # This line is the only stable heartbeat source of K8s availability (once per round). Separate records of success and failure because
        # The troubleshooting directions of "apiserver unreachable" and "database cannot be written" are completely opposite.
        # The except at the run() level classifies both into one snapshot_sync_failed.
        with self.mutation_lock:
            # Read and apply one Kubernetes snapshot under the same fence used by
            # API mutations. Fetching before the lock allowed a stale verified v2
            # snapshot to overwrite a concurrent manual rollback to v1.
            try:
                collection = self.kube.get(COLLECTION_PATH)
            except Exception as exc:
                api.KUBERNETES_HEALTH.record_failure(exc)
                raise
            api.KUBERNETES_HEALTH.record_success()
            items = collection.get("items") or []
            if not isinstance(items, list):
                raise RuntimeError("Kubernetes SiteDeployment collection is invalid")
            result = self.store.sync_snapshot(items)
            promoted = self.store.promote_verified_site_versions(items)
            rollbacks = self.store.failed_site_version_rollbacks(items)
        if promoted:
            telemetry.log("site_versions_promoted", count=promoted)
        for target in rollbacks:
            patch_spec: dict[str, Any] = {
                "siteVersion": target["version"],
                "revision": str(time.time_ns()),
            }
            if target.get("site_type") == "static":
                try:
                    source_path = static_source_path_from_uri(target["artifact_uri"])
                except Exception as exc:
                    telemetry.log_exception(
                        "site_version_rollback_skipped", exc,
                        name=target["cr_name"], version=target["version"],
                    )
                    continue
                patch_spec.update(
                    {
                        "image": STATIC_IMAGE,
                        "staticArtifact": {
                            "sourcePath": source_path,
                            "sha256": target["content_sha256"],
                        },
                    }
                )
            else:
                patch_spec["image"] = target["image"]
            self.kube.patch(
                f"{COLLECTION_PATH}/{target['cr_name']}",
                {"spec": patch_spec},
            )
            telemetry.log(
                "site_version_rollback_requested",
                name=target["cr_name"],
                version=target["version"],
            )
        # Only when the entire batch matches is the "snapshot consistent with the cluster". sync_snapshot will now skip reading
        # A single CR with a name or that cannot be entered instead of rolling back the entire batch - that is correct, but each skipped CR
        # means that the snapshot is still drifting, which is exactly what snapshotAgeSeconds reports. unconditionally
        # Refreshing will display "N items skipped" as "Just synchronized", canceling out the only purpose of this field:
        # Database failures will throw a StorageError logged by run(), and this drift is silent except
        # This clock is nowhere to be seen.
        if result.skipped or not result.reclaimed:
            return
        with self._clock_lock:
            self._last_success = time.monotonic()

    def run(self) -> None:
        while True:
            try:
                self.sync_once()
            except Exception as exc:
                # Any escaping exception would kill this thread and leave the
                # CR/database snapshot silently drifting, which /readyz cannot
                # see because it only pings the database. The drift is instead
                # reported as snapshotAgeSeconds on GET /v1/deployments.
                api.SYNC_TOTAL.inc("failure")
                telemetry.log_exception("snapshot_sync_failed", exc)
            else:
                api.SYNC_TOTAL.inc("success")
            # Reuse the existing _last_success clock (snapshot_age_seconds) without creating a new one——
            # Sooner or later, the two clocks will diverge, and GET /v1/deployments returns the one to the caller.
            age = self.snapshot_age_seconds()
            if age is not None:
                api.SYNC_AGE.set(age)
            time.sleep(self.interval)
