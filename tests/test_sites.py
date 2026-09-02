from __future__ import annotations

import ast
import datetime as dt
import hashlib
import io
import ipaddress
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

import yaml

from tests import chart
from tests.test_support import postgres_connection, postgres_store

from sites import exposure as _exposure
from sites import k8s_resources
from sites import topology
from sites.k8s_resources import (
    CLUSTER_SERVICE_CIDR,
    cluster_pod_cidr,
    cluster_service_cidr,
    workload_egress_except_cidrs,
    MAX_BUNDLE_COMPONENTS,
    site_deployment_resource,
    bundle_resources,
    deployment_resource,
    network_policy_resources,
    resource_quota_resource,
    service_resource,
    site_database_secret_resource,
    static_artifact_secret_name,
)
from sites.naming import (
    token_digest,
    cr_name_for,
    namespace_for_tenant,
)
from sites.admission import (
    BUILD_COLLECTION_PATH,
    COLLECTION_PATH,
    MAX_ACTIVE_BUILDS,
    MerchantQuotaExceeded,
    PublicRouteConflict,
    QuotaExceeded,
    active_public_ports as _active_public_ports,
    merchant_deployment_count as _merchant_deployment_count,
    tenant_deployment_count as _tenant_deployment_count,
    tenant_public_count as _tenant_public_count,
)
from sites.api_errors import (
    BUILD_ERROR_RESPONSES as _BUILD_ERROR_RESPONSES,
    MUTATION_ERROR_RESPONSES as _MUTATION_ERROR_RESPONSES,
)
from sites.validation import (
    DEPLOY_FIELDS,
    SITES_REQUEST_MAX_BYTES,
    DEFAULT_MAX_DEPLOYMENTS,
    DEFAULT_MAX_PUBLIC_ROUTES,
    DEFAULT_MERCHANT_ID,
    INLINE_ARTIFACT_MAX_FILES,
    INLINE_ARTIFACT_MAX_TOTAL_BYTES,
    MAX_ENV_VARS,
    STATIC_IMAGE,
    STATIC_SITE_ROOT,
    ValidationError,
    normalize_artifact,
    normalize_deploy_payload,
    normalize_env,
    normalize_secret_mounts,
)
from sites.api import (
    DatabaseSynchronizer,
    Handler,
    _capabilities_response,
    _render_metrics,
)
from sites.http_kit import CONSOLE_CSP
from sites.identity import DEFAULT_USER_ID
from sites.serializers import (
    bundle_response as _bundle_response,
    deployment_record_response as _deployment_record_response,
    deployment_response as _deployment_response,
)
from sites.builds import (
    SOURCE_MAX_FILES,
    SOURCE_MAX_TOTAL_BYTES,
    SOURCE_REQUEST_MAX_BYTES,
)
from sites.exposure import NODE_PORT_EXCLUDED, NODE_PORT_RANGE
from sites.kube import ApiError
from sites.operator import (
    FINALIZER,
    VERIFY_TIMEOUT_SECONDS,
    Operator,
    _deployment_ready,
    _NoRedirect,
    _parse_time,
)
from sites.storage import (
    DatabaseConfig,
    Store,
    StorageError,
    SyncSnapshotResult,
    site_deployment_values,
)
from sites import storage as sites_storage
# The bundle is the second place that configures this gateway, so the contract
# is only provable by asking the gateway itself whether that env is enough.


# The source of the workload release control plane. Without it, the operator's forensic detection would have been written by itself
# NetworkPolicy blocks the door - the operator is also in the Pod network segment.
_CONTROL_PLANE_SOURCE = {
    "namespaceSelector": {
        "matchLabels": {"kubernetes.io/metadata.name": "sites-local"}
    },
    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "sites-operator"}},
}


def _selector_matches(selector: dict, labels: dict[str, str]) -> bool:
    return all(
        labels.get(key) == value
        for key, value in (selector or {}).get("matchLabels", {}).items()
    )


def _peer_matches(
    peer: dict,
    *,
    ip: str,
    namespace: str,
    pod_labels: dict[str, str],
    own_namespace: str,
) -> bool:
    block = peer.get("ipBlock")
    if block is not None:
        address = ipaddress.ip_address(ip)
        if address not in ipaddress.ip_network(block["cidr"]):
            return False
        return not any(
            address in ipaddress.ip_network(entry)
            for entry in block.get("except", [])
        )
    namespace_selector = peer.get("namespaceSelector")
    pod_selector = peer.get("podSelector")
    if not namespace:
        # An address outside the cluster does not belong to any Namespace, and peers of the selector class can never reach it.
        return False
    if namespace_selector is not None:
        if not _selector_matches(
            namespace_selector, {"kubernetes.io/metadata.name": namespace}
        ):
            return False
    elif pod_selector is not None:
        # A podSelector without a namespaceSelector only covers the Namespace where the policy itself is located.
        if namespace != own_namespace:
            return False
    if pod_selector is not None and not _selector_matches(
        pod_selector, pod_labels
    ):
        return False
    return namespace_selector is not None or pod_selector is not None


def _egress_permits(
    policies: list[dict],
    *,
    ip: str,
    port: int,
    own_namespace: str,
    namespace: str = "",
    pod_labels: dict[str, str] | None = None,
    protocol: str = "TCP",
) -> bool:
    """Determine whether a target is reachable according to the outbound semantics of NetworkPolicy.

    Just asserting that "there is a certain ipBlock in the rule" cannot prove anything: the port is written incorrectly and policyTypes is missing.
    The Egress and except network segments are filled in reversely, and the shape assertions are all green. This follows the semantics of Kubernetes
    Make a true judgment once, and match each negative assertion with a positive comparison.

    The first sentence is the key: **When there is no policy statement for Egress, the semantics of Kubernetes is to allow everything**.
    According to the implementation, once policyTypes falls back to Ingress only, all the following "out of reach" assertions will
    Go red immediately - otherwise they will continue to be all green when the outbound direction is fully open again.
    """
    if not any(
        "Egress" in policy["spec"]["policyTypes"] for policy in policies
    ):
        return True
    for policy in policies:
        if "Egress" not in policy["spec"]["policyTypes"]:
            continue
        for rule in policy["spec"].get("egress", []):
            ports = rule.get("ports")
            if ports is not None and not any(
                entry.get("protocol", "TCP") == protocol
                and entry["port"] == port
                for entry in ports
            ):
                continue
            peers = rule.get("to")
            if peers is None:
                return True
            if any(
                _peer_matches(
                    peer,
                    ip=ip,
                    namespace=namespace,
                    pod_labels=pod_labels or {},
                    own_namespace=own_namespace,
                )
                for peer in peers
            ):
                return True
    return False



# Port pools are a concept exclusive to the NodePort backend. Host under Gateway backend is derived from serviceName.
# There are neither pools nor port conflicts, so the following use cases are semantically inapplicable - they would end with
# KeyError: 'nodePort', "PublicRouteConflict not raised", or "publicRoutes from 8
# The form of "turning into None" failed, and these three things are exactly the desired results of the transformation.
#
# The corresponding assertions under the Gateway backend are in sites/test_exposure.py:
#   Service Shape → ServiceShapeTest
#   NetworkPolicy inbound → NetworkPolicyTest
#   Capacity and Quota → CapacityTest / GatewayQuotaEnforcementTests
# So skip here does not mean giving up the coverage, it just changes the location. Explicitly skip instead of making them red:
# After cutting the backend, a large area of CI will turn red, which will be read as "I changed it".
_POOL_ONLY = unittest.skipUnless(
    _exposure.backend().allocates_ports, "The port pool use case only applies to the NodePort backend"
)


class CommonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "name": "demo-app",
            "image": "example.invalid/demo:v1",
            "port": 8086,
            "healthPath": "/api/agents",
        }
        self.spec = normalize_deploy_payload(
            self.payload, DEFAULT_MERCHANT_ID, "local"
        )

    def test_cr_names_cannot_collide_across_tenants(self) -> None:
        # Both sides of the hyphen between name fields can be contributed by the name itself: dns_label(f"{u}-{s}")
        # This will cause the following two groups to collide with the same CR name, and the CR name is the only one in Kubernetes -
        # That's a cross-tenant coverage path.
        self.assertNotEqual(
            cr_name_for(DEFAULT_MERCHANT_ID, "acme-corp", "web"),
            cr_name_for(DEFAULT_MERCHANT_ID, "acme", "corp-web"),
        )
        self.assertNotEqual(
            cr_name_for(DEFAULT_MERCHANT_ID, "a-b", "c"),
            cr_name_for(DEFAULT_MERCHANT_ID, "a", "b-c"),
        )
        # The merchant section cannot be moved either: services with the same name under two merchants must be two CRs.
        self.assertNotEqual(
            cr_name_for("m-a", "b", "web"), cr_name_for("m", "a-b", "web")
        )
        # The same set of inputs must be stable, otherwise new objects will be created each time reconcile is performed.
        self.assertEqual(
            cr_name_for(DEFAULT_MERCHANT_ID, "acme", "web"),
            cr_name_for(DEFAULT_MERCHANT_ID, "acme", "web"),
        )
        # The result is still a legitimate DNS label.
        name = cr_name_for("m" * 31, "t" * 60, "s" * 60)
        self.assertLessEqual(len(name), 63)
        self.assertTrue(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name))

    @_POOL_ONLY
    def test_site_deployment_contract(self) -> None:
        resource = site_deployment_resource(
            self.payload, DEFAULT_MERCHANT_ID, "local"
        )
        self.assertEqual(
            resource["metadata"]["name"],
            cr_name_for(DEFAULT_MERCHANT_ID, "local", "demo-app"),
        )
        self.assertEqual(resource["spec"]["merchantID"], DEFAULT_MERCHANT_ID)
        self.assertEqual(resource["spec"]["nodePort"], 30080)
        self.assertEqual(resource["spec"]["replicas"], 1)

    @_POOL_ONLY
    def test_capabilities_publish_the_real_inline_contract(self) -> None:
        capabilities = _capabilities_response(DEFAULT_MERCHANT_ID, "local")
        self.assertEqual(capabilities["merchantId"], DEFAULT_MERCHANT_ID)
        self.assertEqual(capabilities["userId"], "local")
        self.assertEqual(capabilities["metadataDatabase"], "postgresql")
        self.assertEqual(
            capabilities["limits"],
            {
                "maxRequestBytes": SITES_REQUEST_MAX_BYTES,
                "maxInlineArtifactBytes": INLINE_ARTIFACT_MAX_TOTAL_BYTES,
                "maxInlineArtifactFiles": INLINE_ARTIFACT_MAX_FILES,
                "maxSourceRequestBytes": SOURCE_REQUEST_MAX_BYTES,
                "maxSourceArtifactBytes": SOURCE_MAX_TOTAL_BYTES,
                "maxSourceFiles": SOURCE_MAX_FILES,
                "maxMigrationBytes": 48 * 1024,
                "maxMigrationStatements": 32,
                "publicRoutes": len(NODE_PORT_RANGE),
            },
        )
        self.assertTrue(capabilities["deploymentModes"]["staticInline"]["enabled"])
        self.assertTrue(capabilities["features"]["sourceBuild"])
        self.assertTrue(capabilities["features"]["registry"])
        self.assertTrue(
            capabilities["deploymentModes"]["dockerfileSource"]["enabled"]
        )
        self.assertFalse(capabilities["features"]["requestSecrets"])

    def test_inline_artifact_rejects_content_over_the_http_safe_budget(self) -> None:
        with self.assertRaisesRegex(ValidationError, "exceeds 61440 bytes"):
            normalize_artifact(
                {
                    "files": {
                        "index.html": "x" * (INLINE_ARTIFACT_MAX_TOTAL_BYTES + 1),
                    }
                }
            )

    def test_image_references_use_docker_grammar(self) -> None:
        valid = [
            "nginx",
            "nginx:1.29-alpine",
            "library/nginx:1",
            "example.invalid/api:v1",
            "localhost:5000/local/web@sha256:"
            + "a" * 64,
        ]
        invalid = [
            "",
            ":tag",
            "example.invalid/api:",
            "example.invalid/api:@sha256:" + "a" * 64,
            "example.invalid/api:v1@not-a-digest",
            "example.invalid:99999/api:v1",
            "example.invalid/api/v1@sha256:" + "a" * 64 + "@sha256:" + "a" * 64,
            "Example.Invalid/API:v1",
            "example.invalid/-bad:v1",
        ]
        for image in valid:
            with self.subTest(image=image):
                spec = normalize_deploy_payload(
                    {"name": "web", "image": image},
                    DEFAULT_MERCHANT_ID,
                    "alice",
                )
                self.assertEqual(spec["image"], image)
        for image in invalid:
            with self.subTest(image=image), self.assertRaises(ValidationError):
                normalize_deploy_payload(
                    {"name": "web", "image": image},
                    DEFAULT_MERCHANT_ID,
                    "alice",
                )

    def test_http_body_rejects_content_length_over_the_published_limit(self) -> None:
        handler = object.__new__(Handler)
        handler.headers = {"Content-Length": str(SITES_REQUEST_MAX_BYTES + 1)}
        with self.assertRaisesRegex(ValidationError, str(SITES_REQUEST_MAX_BYTES)):
            handler._read_body()

    def test_http_body_rejects_transfer_encoding(self) -> None:
        handler = object.__new__(Handler)
        handler.headers = {"Transfer-Encoding": "chunked"}
        with self.assertRaisesRegex(
            ValidationError, "single Content-Length"
        ):
            handler._read_body()

    def test_http_body_rejects_duplicate_content_length(self) -> None:
        class Headers:
            def get_all(self, name: str, default: list[str] | None = None):
                if name == "Content-Length":
                    return ["1", "2"]
                return default or []

        handler = object.__new__(Handler)
        handler.headers = Headers()
        with self.assertRaisesRegex(
            ValidationError, "one Content-Length"
        ):
            handler._read_body()

    def test_deployment_response_exposes_resource_state(self) -> None:
        response = _deployment_response(
            {
                "metadata": {"name": "local-web"},
                "spec": {
                    "serviceName": "web",
                    "revision": "revision-7",
                    "exposure": "internal",
                },
                "status": {
                    "phase": "Running",
                    "ready": True,
                    "url": "http://127.0.0.1:18090",
                },
            }
        )
        self.assertEqual(response["name"], "local-web")
        self.assertEqual(response["revision"], "revision-7")
        self.assertEqual(response["exposure"], "internal")
        self.assertTrue(response["ready"])

    def test_deployment_record_response_serializes_durable_state(self) -> None:
        updated_at = dt.datetime(2026, 8, 12, 8, 30, tzinfo=dt.UTC)
        response = _deployment_record_response(
            {
                "cr_name": "local-web",
                "service_name": "web",
                "image": "nginx:1.27-alpine",
                "port": 8080,
                "health_path": "/",
                "revision": "revision-8",
                "exposure": "internal",
                "scale_to_zero": True,
                "observed_replicas": 0,
                "phase": "Running",
                "message": "Deployment is available",
                "url": "http://127.0.0.1:18090",
                "created_at": updated_at,
                "updated_at": updated_at,
                "deletion_requested_at": None,
            }
        )
        self.assertEqual(response["serviceName"], "web")
        self.assertEqual(response["updatedAt"], "2026-08-12T08:30:00+00:00")
        self.assertEqual(response["healthPath"], "/")
        self.assertEqual(response["exposure"], "internal")
        self.assertTrue(response["ready"])
        self.assertTrue(response["scaleToZero"])
        self.assertEqual(response["observedReplicas"], 0)
        self.assertEqual(response["runtimeState"], "Dormant")

    def test_rejects_invalid_image_and_user(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_deploy_payload(
                {**self.payload, "image": "bad image"},
                DEFAULT_MERCHANT_ID,
                "local",
            )
        with self.assertRaises(ValidationError):
            normalize_deploy_payload(
                self.payload, DEFAULT_MERCHANT_ID, "../prod"
            )
        with self.assertRaises(ValidationError):
            normalize_deploy_payload(self.payload, "../prod", "local")

    def test_an_unregistered_field_is_refused_not_dropped(self) -> None:
        # 🔴 The filter below this check used to be the whole story: anything
        # outside DEPLOY_FIELDS was quietly removed and the request answered
        # 200.  `scaleToZeroo` deployed a site with scale-to-zero off and said
        # nothing, which is the failure this API documents itself as not having
        # ("refused with 403, not ignored" -- README.md, docs/AUTH.md).  Every
        # sibling normalizer already refuses unknown keys.
        for typo in ("scaleToZeroo", "memory_limit", "replicas"):
            with self.subTest(typo=typo):
                with self.assertRaisesRegex(ValidationError, typo):
                    normalize_deploy_payload(
                        {**self.payload, typo: True},
                        DEFAULT_MERCHANT_ID,
                        "local",
                    )
        # The error has to name every offender, not just the first one: a
        # caller who fixes one key and resubmits should not have to discover
        # the next one by another round trip.
        with self.assertRaisesRegex(ValidationError, "aaa, zzz"):
            normalize_deploy_payload(
                {**self.payload, "zzz": 1, "aaa": 2}, DEFAULT_MERCHANT_ID, "local"
            )
        # The other direction: every registered field, plus the `artifact` mode
        # selector, must still be accepted.  A guard written as "reject anything
        # that is not name/image" would pass the loop above and reject the
        # entire real contract.
        accepted = {
            "name": "ok-name", "image": "example.com/x:1", "port": 8080,
            "healthPath": "/", "livenessPath": "/", "exposure": "internal",
            "env": [{"name": "A", "value": "b"}], "secretMounts": [],
            "runAsUser": 10001,
            "scaleToZero": False, "memoryLimit": "512Mi", "siteVersion": 1,
        }
        self.assertEqual(sorted(accepted), sorted(DEPLOY_FIELDS))
        normalized = normalize_deploy_payload(
            accepted, DEFAULT_MERCHANT_ID, "local"
        )
        self.assertEqual("ok-name", normalized["serviceName"])

    def test_service_name_is_strict_not_silently_normalized(self) -> None:
        # The name submitted by the user must be the deployment name: capitals/underscores/spaces are silently transcribed
        # MY_CARD and my-card point to the same deployment (the latter overrides the former), and is not stated in the response.
        # Conversion has occurred - user cannot find their service by submission name. All other names are 400,
        # Normalization is left only to control plane internal derivation (cr_name, etc.).
        for bad in ("MY_BAD_NAME", "bad name", "bad_name", "!!!", ""):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValidationError, "service name"):
                    normalize_deploy_payload(
                        {**self.payload, "name": bad},
                        DEFAULT_MERCHANT_ID,
                        "local",
                    )
        # The legal name is retained as it is and is no longer transcribed by dns_label.
        ok = normalize_deploy_payload(
            {**self.payload, "name": "my-card-2"},
            DEFAULT_MERCHANT_ID,
            "local",
        )
        self.assertEqual(ok["serviceName"], "my-card-2")

    def test_tenant_namespace_is_local_and_stable(self) -> None:
        namespace = namespace_for_tenant(DEFAULT_MERCHANT_ID, "local")
        self.assertTrue(namespace.endswith("-local"))
        self.assertLessEqual(len(namespace), 63)
        self.assertEqual(
            namespace, namespace_for_tenant(DEFAULT_MERCHANT_ID, "local")
        )

    def test_distinct_tenants_never_share_a_namespace_or_cr_name(self) -> None:
        users = ["ab", "a-b", "a-b-1", "b-a", "x" * 63, "x" * 62 + "y"]
        tenants = [(DEFAULT_MERCHANT_ID, user) for user in users]
        # The same user_id is two tenants under two merchants and must fall in two Namespaces——
        # The namespace is the isolation boundary itself.
        tenants += [("other", user) for user in users]
        self.assertEqual(
            len({namespace_for_tenant(*tenant) for tenant in tenants}),
            len(tenants),
        )
        self.assertEqual(
            len({cr_name_for(*tenant, "web") for tenant in tenants}),
            len(tenants),
        )

    def test_identifier_ranges_match_the_dns_label_range(self) -> None:
        # Dots, underscores and uppercase used to fold into hyphens, so a.b,
        # a_b and A.B all landed in the same namespace as a-b.
        for folded in ("a.b", "a_b", "A.B", "A-B"):
            with self.assertRaises(ValidationError):
                namespace_for_tenant(DEFAULT_MERCHANT_ID, folded)
            with self.assertRaises(ValidationError):
                namespace_for_tenant(folded, "local")
        # The merchant ID is shorter than the user ID: the two segments must be added together to fit into one DNS label.
        with self.assertRaises(ValidationError):
            namespace_for_tenant("m" * 32, "local")

    def test_pull_policy_follows_the_image_reference_form(self) -> None:
        """The mutable tag must be Always, and the digest reference must be IfNotPresent.

        Always is for local stack images where "tags are fixed and content changes with each build"——
        The old cache of the node under IfNotPresent will win, and redeployment will fail silently. But this builder also serves
        digest reference (it is used to build artifacts), digest is content-addressed and cannot be obtained by Always
        Anything new, just adds a registry round trip for each Pod startup.
        """

        def policy_for(image: str) -> str:
            deployment = deployment_resource(
                {**self.spec, "image": image}, "ulocal-local"
            )
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual(container["image"], image)
            return container["imagePullPolicy"]

        self.assertEqual(policy_for("registry.convee.local:5000/demo:sites-local"), "Always")
        self.assertEqual(
            policy_for(
                "registry.convee.local:5000/demo@sha256:"
                + "0" * 64
            ),
            "IfNotPresent",
        )

    def test_workload_is_restricted(self) -> None:
        deployment = deployment_resource(
            {**self.spec, "revision": "revision-2"}, "ulocal-local"
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        self.assertEqual(
            deployment["spec"]["template"]["metadata"]["annotations"][
                "sites.local/revision"
            ],
            "revision-2",
        )
        self.assertFalse(pod_spec["automountServiceAccountToken"])
        self.assertFalse(pod_spec["enableServiceLinks"])
        self.assertTrue(pod_spec["securityContext"]["runAsNonRoot"])
        self.assertEqual(
            pod_spec["securityContext"]["seccompProfile"]["type"],
            "RuntimeDefault",
        )
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(
            container["securityContext"]["capabilities"]["drop"], ["ALL"]
        )
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/api/agents")

    def test_versioned_dynamic_workload_uses_tenant_database_secret(self) -> None:
        database = {
            "schema": "site_" + "a" * 32,
            "controlSecretName": "site-db-" + "a" * 24,
            "secretName": "site-db-" + "a" * 24,
        }
        spec = {**self.spec, "database": database}
        deployment = deployment_resource(spec, "ulocal-local")
        env = {
            item["name"]: item
            for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(
            env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["name"],
            database["secretName"],
        )
        self.assertEqual(
            deployment["spec"]["template"]["metadata"]["labels"][
                "sites.local/database-access"
            ],
            "true",
        )
        self.assertNotIn(
            "sites.local/database-access",
            deployment["spec"]["selector"]["matchLabels"],
        )
        encoded = {key: "YQ==" for key in (
            "runtime-host", "runtime-port", "runtime-database", "runtime-user",
            "runtime-password", "runtime-sslmode", "runtime-schema",
        )}
        secret = site_database_secret_resource(
            spec, "ulocal-local", {"data": encoded}
        )
        self.assertEqual(secret["metadata"]["namespace"], "ulocal-local")
        self.assertNotIn("reader-password", secret["data"])
        self.assertEqual(secret["data"]["PGPASSWORD"], "YQ==")

    @_POOL_ONLY
    def test_service_and_network_policy_use_only_app_port(self) -> None:
        service = service_resource(self.spec, "ulocal-local")
        self.assertEqual(service["spec"]["ports"][0]["nodePort"], 30080)
        policies = network_policy_resources(self.spec, "ulocal-local")
        self.assertNotIn("ingress", policies[0]["spec"])
        self.assertEqual(
            policies[1]["spec"]["ingress"][0]["ports"][0]["port"],
            8086,
        )

    @_POOL_ONLY
    def test_public_deployment_excludes_cross_tenant_pod_traffic(self) -> None:
        sources = network_policy_resources(self.spec, "ulocal-local")[1]["spec"][
            "ingress"
        ][0]["from"]
        self.assertIn({"podSelector": {}}, sources)
        self.assertIn(
            {"ipBlock": {"cidr": "0.0.0.0/0", "except": [cluster_pod_cidr()]}},
            sources,
        )
        self.assertNotIn({"ipBlock": {"cidr": "0.0.0.0/0"}}, sources)

    def _sample_components(self) -> list[dict]:
        # A two-component stack that is independent of the control plane. A control plane should have no presuppositions about its content—
        # How components discover each other and who wants what key is all written in the spec submitted by the caller.
        return [
            {
                "name": "api",
                "image": "example.invalid/api:v1",
                "port": 8080,
                "healthPath": "/healthz",
                "exposure": "internal",
                "runAsUser": 10005,
                "env": [
                    {"name": "MODE", "value": "prod"},
                    {"name": "PORT", "value": "9999"},
                    {
                        "name": "TOKEN",
                        "secretKeyRef": {"name": "app-keys", "key": "token"},
                    },
                ],
                "secretMounts": [
                    {
                        "secretName": "app-keys",
                        "mountPath": "/var/run/keys",
                        "items": [{"key": "token", "path": "token"}],
                    }
                ],
            },
            {
                "name": "web",
                "image": "example.invalid/web:v1",
                "port": 8081,
                "healthPath": "/healthz",
                "exposure": "public",
            },
        ]

    def test_bundle_labels_components_without_knowing_what_they_are(self) -> None:
        resources = bundle_resources(
            "demo-stack",
            self._sample_components(),
            DEFAULT_MERCHANT_ID,
            "local",
        )
        self.assertEqual(len(resources), 2)
        self.assertTrue(
            all(
                resource["spec"]["bundleName"] == "demo-stack"
                and resource["metadata"]["labels"]["sites.local/bundle"]
                == "demo-stack"
                for resource in resources
            )
        )
        self.assertEqual(
            sum(
                resource["spec"]["exposure"] == "public"
                for resource in resources
            ),
            1,
        )
        api = next(
            resource
            for resource in resources
            if resource["spec"]["serviceName"] == "api"
        )
        # The uid declared by the caller is transmitted transparently regardless of the role and is no longer inferred from the component name.
        self.assertEqual(api["spec"]["runAsUser"], 10005)
        self.assertEqual(api["spec"]["componentRole"], "app")

    def test_submitted_env_and_secret_mounts_reach_the_container(self) -> None:
        resources = bundle_resources(
            "demo-stack",
            self._sample_components(),
            DEFAULT_MERCHANT_ID,
            "local",
        )
        api = next(
            resource
            for resource in resources
            if resource["spec"]["serviceName"] == "api"
        )
        pod = deployment_resource(api["spec"], "ulocal-local")["spec"]["template"][
            "spec"
        ]
        container = pod["containers"][0]
        env = {item["name"]: item for item in container["env"]}

        self.assertEqual(env["MODE"]["value"], "prod")
        self.assertEqual(
            env["TOKEN"]["valueFrom"]["secretKeyRef"],
            {"name": "app-keys", "key": "token"},
        )
        # The built-in PORT is just the default value: the caller takes precedence when it has the same name, and it only appears once.
        self.assertEqual(env["PORT"]["value"], "9999")
        self.assertEqual(
            [item["name"] for item in container["env"]].count("PORT"), 1
        )
        # Common assumptions that were not covered remain.
        self.assertEqual(env["HOME"]["value"], "/data")

        mount = next(
            item
            for item in container["volumeMounts"]
            if item["mountPath"] == "/var/run/keys"
        )
        self.assertTrue(mount["readOnly"])
        volume = next(
            item for item in pod["volumes"] if item["name"] == mount["name"]
        )
        self.assertEqual(volume["secret"]["secretName"], "app-keys")
        self.assertEqual(volume["secret"]["defaultMode"], 0o440)
        self.assertEqual(
            volume["secret"]["items"], [{"key": "token", "path": "token"}]
        )
        self.assertEqual(pod["securityContext"]["runAsUser"], 10005)

    def test_internal_component_admits_only_its_own_bundle(self) -> None:
        resources = bundle_resources(
            "demo-stack",
            self._sample_components(),
            DEFAULT_MERCHANT_ID,
            "local",
        )
        api = next(
            resource
            for resource in resources
            if resource["spec"]["serviceName"] == "api"
        )
        sources = network_policy_resources(api["spec"], "ulocal-local")[1]["spec"][
            "ingress"
        ][0]["from"]
        self.assertEqual(
            sources,
            [
                {
                    "podSelector": {
                        "matchLabels": {"sites.local/bundle": "demo-stack"}
                    }
                },
                _CONTROL_PLANE_SOURCE,
            ],
        )
        # The above selector is only valid if the Pod actually has this label.
        deployment = deployment_resource(api["spec"], "ulocal-local")
        self.assertEqual(
            deployment["spec"]["template"]["metadata"]["labels"][
                "sites.local/bundle"
            ],
            "demo-stack",
        )
        self.assertEqual(
            deployment["metadata"]["labels"]["sites.local/bundle"],
            "demo-stack",
        )
        # But the selector cannot be changed accordingly: Deployment.spec.selector is immutable.
        self.assertNotIn(
            "sites.local/bundle", deployment["spec"]["selector"]["matchLabels"]
        )

    def test_internal_service_without_a_bundle_stays_namespace_local(self) -> None:
        spec = normalize_deploy_payload(
            {**self.payload, "exposure": "internal"}, DEFAULT_MERCHANT_ID, "local"
        )
        sources = network_policy_resources(spec, "ulocal-local")[1]["spec"][
            "ingress"
        ][0]["from"]
        self.assertEqual(sources, [{"podSelector": {}}, _CONTROL_PLANE_SOURCE])
        self.assertEqual(
            service_resource(spec, "ulocal-local")["spec"]["type"], "ClusterIP"
        )

    def test_every_exposure_lets_the_control_plane_probe(self) -> None:
        # All three forms must release the control plane, otherwise status.verification will always be
        # "Can't connect" - and it looks exactly like the app is actually down.
        internal_bundled = bundle_resources(
            "demo-stack",
            self._sample_components(),
            DEFAULT_MERCHANT_ID,
            "local",
        )[0]["spec"]
        internal_standalone = normalize_deploy_payload(
            {**self.payload, "exposure": "internal"}, DEFAULT_MERCHANT_ID, "local"
        )
        public = self.spec
        for spec in (internal_bundled, internal_standalone, public):
            sources = network_policy_resources(spec, "ulocal-local")[1]["spec"][
                "ingress"
            ][0]["from"]
            self.assertIn(_CONTROL_PLANE_SOURCE, sources)

    # ----Outbound ----------------------------------------------------------------
    # The output of the workload was completely open before (the two policies only declared Ingress). The following set of use cases determines
    # Real semantics rather than regular shapes, see the docstring of _egress_permits for criteria.

    def _egress_to(self, spec: dict, **destination) -> bool:
        return _egress_permits(
            network_policy_resources(spec, "ulocal-local"),
            own_namespace="ulocal-local",
            **destination,
        )

    def test_workload_egress_allows_dns_own_namespace_and_the_internet(
        self,
    ) -> None:
        # There is no point in blocking the external network platform: it is normal for tenant applications to call external APIs and pull data.
        # requirements, so there is no port restriction outside the cluster. These are the positive comparisons of all the negative assertions below -
        # Without them, an empty strategy of "deny nothing" would make negative assertions all green.
        for protocol in ("UDP", "TCP"):
            with self.subTest(dns=protocol):
                self.assertTrue(
                    self._egress_to(
                        self.spec,
                        ip="10.201.0.5",
                        namespace="kube-system",
                        pod_labels={"k8s-app": "kube-dns"},
                        port=53,
                        protocol=protocol,
                    )
                )
        # Only put 53 in the DNS entry: the resolution is allowed, not the entire kube-system.
        self.assertFalse(
            self._egress_to(
                self.spec,
                ip="10.201.0.5",
                namespace="kube-system",
                pod_labels={"k8s-app": "kube-dns"},
                port=8080,
            )
        )
        # Another service of the same tenant's own.
        self.assertTrue(
            self._egress_to(
                self.spec,
                ip="10.201.1.7",
                namespace="ulocal-local",
                pod_labels={"app.kubernetes.io/name": "worker"},
                port=8080,
            )
        )
        # Outside the cluster, no port restrictions - 5432. This pin is "80/443 white names that do not copy the building surface"
        # "Single": This will cause the application connected to the externally hosted database to hang up in the form of connection timeout, and in the single test
        # If the fake payload does not send a packet, this defect will be all green.
        for port in (80, 443, 5432):
            with self.subTest(internet=port):
                self.assertTrue(
                    self._egress_to(self.spec, ip="93.184.216.34", port=port)
                )

    def test_workload_egress_blocks_every_in_cluster_target(self) -> None:
        blocked = {
            # Control plane's own library.
            "postgres": {
                "ip": "10.201.0.30",
                "namespace": "sites-local",
                "pod_labels": {"app.kubernetes.io/name": "sites-postgres"},
                "port": 5432,
            },
            # Pods in other tenant namespaces.
            "other-tenant": {
                "ip": "10.201.2.3",
                "namespace": "uother-local",
                "pod_labels": {"app.kubernetes.io/name": "web"},
                "port": 8080,
            },
            # ClusterIP segment: apiserver and any other Service.
            "apiserver-clusterip": {"ip": "10.221.0.1", "port": 443},
            "service-clusterip": {"ip": "10.221.12.9", "port": 5432},
        }
        for name, destination in blocked.items():
            with self.subTest(target=name):
                self.assertFalse(self._egress_to(self.spec, **destination))
        # Forward comparison: Under the same criterion, outside the cluster is still allowed, otherwise the above four conditions are just "all rejected".
        self.assertTrue(
            self._egress_to(self.spec, ip="93.184.216.34", port=443)
        )

    def test_dynamic_site_egress_allows_only_control_plane_postgres(self) -> None:
        dynamic = {
            **self.spec,
            "database": {"secretName": "site-db-abc"},
        }
        self.assertTrue(
            self._egress_to(
                dynamic,
                ip="10.201.0.30",
                namespace="sites-local",
                pod_labels={"app.kubernetes.io/name": "sites-postgres"},
                port=5432,
            )
        )
        self.assertFalse(
            self._egress_to(
                dynamic,
                ip="10.201.0.30",
                namespace="sites-local",
                pod_labels={"app.kubernetes.io/name": "sites-postgres"},
                port=8080,
            )
        )
        self.assertFalse(
            self._egress_to(
                dynamic,
                ip="10.201.0.31",
                namespace="sites-local",
                pod_labels={"app.kubernetes.io/name": "sites-registry"},
                port=5432,
            )
        )

    def test_workload_cannot_reach_the_node_hostports(self) -> None:
        # The registry has opened hostPort 5000, so <nodeIP>:5000 can bypass the registry.
        # Use your own NetworkPolicy to enumerate /v2/_catalog or delete other manifests. block it
        # is the node address segment in except, not the port whitelist. kubeadm node is lima
        # VM in the `kube` network segment (192.168.6.0/24, see kubeadm_profile.py).
        for name, port in (("registry", 5000), ("kubelet", 10250), ("api", 6443)):
            with self.subTest(node_port=name):
                self.assertFalse(
                    self._egress_to(self.spec, ip="192.168.6.12", port=port)
                )
        # The 172.16.0.0/12 one is also pinned: it will also be blocked when switching to any RFC1918 node segment.
        self.assertFalse(self._egress_to(self.spec, ip="172.18.0.2", port=5000))
        # Forward comparison: What is blocked is the node address segment rather than the port numbers themselves.
        self.assertTrue(self._egress_to(self.spec, ip="93.184.216.34", port=5000))
        # 🔴 The premise for the above statement to be true is that the node address falls in RFC1918. Bare metal of public IP
        # If the node is not in except, hostPort 5000 will be reachable again - the code cannot tell.
        # Only the contents of except are pinned here.
        excluded = workload_egress_except_cidrs()
        self.assertIn("172.16.0.0/12", excluded)
        self.assertIn("192.168.0.0/16", excluded)
        self.assertIn(cluster_pod_cidr(), excluded)
        self.assertIn(CLUSTER_SERVICE_CIDR, excluded)

    def test_bundle_components_can_still_reach_each_other(self) -> None:
        # The semantics of bundles are that components discover each other through Service DNS. It is easiest to converge in the outward direction here
        # The problem is broken, and the broken performance is the connection timeout between applications - the inbound policy looks completely normal.
        resources = bundle_resources(
            "demo-stack", self._sample_components(), DEFAULT_MERCHANT_ID, "local"
        )
        specs = {
            resource["spec"]["serviceName"]: resource["spec"]
            for resource in resources
        }
        api, web = specs["api"], specs["web"]
        sibling_labels = {"sites.local/bundle": "demo-stack"}
        self.assertTrue(
            self._egress_to(
                api,
                ip="10.201.1.9",
                namespace="ulocal-local",
                pod_labels={**sibling_labels, "app.kubernetes.io/name": "web"},
                port=int(web["port"]),
            )
        )
        self.assertTrue(
            self._egress_to(
                web,
                ip="10.201.1.8",
                namespace="ulocal-local",
                pod_labels={**sibling_labels, "app.kubernetes.io/name": "api"},
                port=int(api["port"]),
            )
        )
        # The bundle tag is not a cross-namespace pass: other tenants use the same
        # The bundle name is also not in the release range.
        self.assertFalse(
            self._egress_to(
                api,
                ip="10.201.2.9",
                namespace="uother-local",
                pod_labels={**sibling_labels, "app.kubernetes.io/name": "web"},
                port=int(web["port"]),
            )
        )

    def test_every_exposure_restricts_egress(self) -> None:
        # The outbound rule hangs on the second policy, but both must declare Egress: the first one is missing,
        # Targets not covered by the rules are not rejected but allowed.
        internal_bundled = bundle_resources(
            "demo-stack", self._sample_components(), DEFAULT_MERCHANT_ID, "local"
        )[0]["spec"]
        internal_standalone = normalize_deploy_payload(
            {**self.payload, "exposure": "internal"}, DEFAULT_MERCHANT_ID, "local"
        )
        for spec in (internal_bundled, internal_standalone, self.spec):
            with self.subTest(exposure=spec.get("exposure")):
                deny, allow = network_policy_resources(spec, "ulocal-local")
                self.assertEqual(
                    deny["spec"]["policyTypes"], ["Ingress", "Egress"]
                )
                self.assertEqual(
                    allow["spec"]["policyTypes"], ["Ingress", "Egress"]
                )
                self.assertNotIn("egress", deny["spec"])
                self.assertTrue(allow["spec"]["egress"])

    def test_bundle_rejects_malformed_submissions(self) -> None:
        components = self._sample_components()
        with self.assertRaises(ValidationError):
            bundle_resources("demo-stack", [], DEFAULT_MERCHANT_ID, "local")
        with self.assertRaises(ValidationError):
            bundle_resources(
                "demo-stack",
                [components[0], components[0]],
                DEFAULT_MERCHANT_ID,
                "local",
            )
        with self.assertRaises(ValidationError):
            bundle_resources(
                "demo-stack",
                [
                    {**components[1], "name": f"web-{index}"}
                    for index in range(MAX_BUNDLE_COMPONENTS + 1)
                ],
                DEFAULT_MERCHANT_ID,
                "local",
            )

    def test_env_requires_exactly_one_source_per_name(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_env([{"name": "A"}])
        with self.assertRaises(ValidationError):
            normalize_env(
                [
                    {
                        "name": "A",
                        "value": "x",
                        "secretKeyRef": {"name": "s", "key": "k"},
                    }
                ]
            )
        with self.assertRaises(ValidationError):
            normalize_env([{"name": "A", "value": "1"}, {"name": "A", "value": "2"}])
        with self.assertRaises(ValidationError):
            normalize_env([{"name": "not a name", "value": "1"}])
        with self.assertRaises(ValidationError):
            normalize_env(
                [{"name": f"A{index}", "value": "1"} for index in range(MAX_ENV_VARS + 1)]
            )

    def test_secret_mounts_refuse_reserved_and_duplicate_paths(self) -> None:
        for mount_path in ("/data", "/tmp", "/", STATIC_SITE_ROOT, "/x/../y"):
            with self.assertRaises(ValidationError):
                normalize_secret_mounts(
                    [{"secretName": "s", "mountPath": mount_path}]
                )
        with self.assertRaises(ValidationError):
            normalize_secret_mounts(
                [
                    {"secretName": "s", "mountPath": "/var/run/keys"},
                    {"secretName": "t", "mountPath": "/var/run/keys/"},
                ]
            )

    def test_platform_managed_secrets_cannot_be_referenced_by_a_tenant(self) -> None:
        """A tenant may not name the control plane's own Secrets.

        🔴 The operator copies the platform object-store credentials into the
        tenant's Namespace so the init container can download the artifact, and
        the Secret name is a pure function of merchant/user/site - values the
        tenant already knows. Without this rule a tenant could mount that Secret
        and read or overwrite every other tenant's published site and build
        context, because there is one bucket and one key pair for all of them."""
        leaked = static_artifact_secret_name(
            {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": "alice",
                "serviceName": "my-static-site",
            }
        )
        self.assertTrue(leaked.startswith("site-oss-"))
        for secret_name in (leaked, "site-db-abcdef0123456789"):
            with self.assertRaises(ValidationError):
                normalize_env(
                    [
                        {
                            "name": "LEAK",
                            "secretKeyRef": {"name": secret_name, "key": "k"},
                        }
                    ]
                )
            with self.assertRaises(ValidationError):
                normalize_secret_mounts(
                    [{"secretName": secret_name, "mountPath": "/leak"}]
                )
        #The tenant's own Secrets stay usable: this is a denylist on two reserved
        #prefixes, not a lockdown of the feature.
        self.assertEqual(
            normalize_env(
                [{"name": "OK", "secretKeyRef": {"name": "app-keys", "key": "k"}}]
            )[0]["valueFrom"]["secretKeyRef"]["name"],
            "app-keys",
        )
        self.assertEqual(
            normalize_secret_mounts(
                [{"secretName": "my-certs", "mountPath": "/certs"}]
            )[0]["secretName"],
            "my-certs",
        )

    def test_run_as_user_may_not_be_root(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_deploy_payload(
                {**self.payload, "runAsUser": 0}, DEFAULT_MERCHANT_ID, "local"
            )
        # The runtime uid of direct investment is determined by the platform, and nothing given by the caller will count.
        spec = normalize_deploy_payload(
            {
                **self.payload,
                "runAsUser": 10005,
                "artifact": {"files": {"index.html": "<h1>hi</h1>"}},
            },
            DEFAULT_MERCHANT_ID,
            "local",
        )
        self.assertEqual(spec["componentRole"], "static")
        self.assertEqual(spec["runAsUser"], 101)

    def test_legacy_component_roles_fail_loudly(self) -> None:
        # The old version has injected configuration according to backend/gateway/web. Those components must be resubmitted after upgrading.
        # Silent fallback will only result in "deployment successful but application cannot be started".
        with self.assertRaises(ValidationError) as caught:
            deployment_resource(
                {**self.spec, "componentRole": "backend"}, "ulocal-local"
            )
        self.assertIn("resubmit", str(caught.exception))

    def test_bundle_response_reports_the_submitted_name(self) -> None:
        objects = bundle_resources(
            "demo-stack",
            self._sample_components(),
            DEFAULT_MERCHANT_ID,
            "local",
        )
        for resource in objects:
            resource["metadata"]["generation"] = 2
            resource["status"] = {
                "phase": "Running",
                "observedGeneration": 2,
                "url": (
                    "http://127.0.0.1:18090"
                    if resource["spec"]["exposure"] == "public"
                    else None
                ),
            }
        response = _bundle_response("demo-stack", objects)
        self.assertEqual(response["name"], "demo-stack")
        self.assertEqual(response["phase"], "Running")
        self.assertEqual(response["url"], "http://127.0.0.1:18090")
        self.assertEqual(response["status_url"], "/v1/bundles/demo-stack")
        objects[0]["status"]["observedGeneration"] = 1
        self.assertEqual(
            _bundle_response("demo-stack", objects)["phase"], "Deploying"
        )

def _crd_spec_properties() -> set[str]:
    """Read the SiteDeployment spec fields the CRD actually declares.

    Deliberately not referencing pyyaml: it is not included in the runtime dependencies of this repository, and this analysis only targets the same repository.
    Format stable list. Failure to parse will cause the following assertion to hang directly and will not be silently let go.
    """
    manifest = chart.template("00-platform.yaml")
    start = manifest.index("            spec:\n              type: object\n")
    end = manifest.index("            status:\n", start)
    return set(
        re.findall(
            r"^ {16}([a-zA-Z][a-zA-Z0-9]*):$",
            manifest[start:end],
            re.MULTILINE,
        )
    )


class VerificationTests(unittest.TestCase):
    """Evidence detected by the control plane itself, rather than the agent's self-report."""

    def setUp(self) -> None:
        self.spec = normalize_deploy_payload(
            {
                "name": "demo",
                "image": "example.invalid/demo:v1",
                "port": 8080,
                "healthPath": "/healthz",
            },
            DEFAULT_MERCHANT_ID,
            "local",
        )
        self.spec["revision"] = "rev-1"
        self.operator = Operator(FakeKube())

    def _probe(self, handler):
        # For forensics, use module-level _VERIFY_OPENER (an opener that refuses to follow redirects), no longer
        # urllib.request.urlopen. If the patch is in the wrong place, an error will not be reported, but the use case will actually do DNS.
        # Parse, and then disguise "Name or service not known" as "workload unreachable".
        with patch("sites.operator._VERIFY_OPENER.open", handler):
            return self.operator._verify_workload(self.spec, "ulocal-local")

    def test_a_passing_probe_records_the_response_digest(self) -> None:
        body = b"ok"

        class Response:
            status = 200

            def __init__(self) -> None:
                self._sent = False

            def read(self, _size):
                # The response is now read in chunks until the upper limit is reached or empty, and the same piece of content is always returned.
                # The pile will be read continuously, pushing bodyBytes to the upper limit of 64KiB.
                if self._sent:
                    return b""
                self._sent = True
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        captured = {}

        def urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return Response()

        evidence = self._probe(urlopen)
        self.assertEqual(
            captured["url"], "http://demo.ulocal-local.svc:8080/healthz"
        )
        self.assertEqual(captured["timeout"], VERIFY_TIMEOUT_SECONDS)
        self.assertTrue(evidence["ok"])
        self.assertIsNone(evidence["error"])
        self.assertEqual(evidence["httpStatus"], 200)
        self.assertEqual(evidence["bodyBytes"], 2)
        self.assertEqual(
            evidence["bodySha256"], hashlib.sha256(body).hexdigest()
        )
        self.assertEqual(evidence["revision"], "rev-1")

    def test_an_unreachable_workload_is_recorded_not_swallowed(self) -> None:
        def urlopen(_request, timeout):
            raise OSError("connection refused")

        evidence = self._probe(urlopen)
        self.assertFalse(evidence["ok"])
        self.assertIn("connection refused", evidence["error"])
        # Without probing, there should be no traces of "matching content" left behind.
        self.assertNotIn("bodySha256", evidence)

    def test_an_http_error_is_still_evidence(self) -> None:
        def urlopen(_request, timeout):
            raise urllib.error.HTTPError(
                "http://demo", 503, "unavailable", {}, io.BytesIO(b"down")
            )

        evidence = self._probe(urlopen)
        self.assertFalse(evidence["ok"])
        self.assertEqual(evidence["httpStatus"], 503)
        self.assertEqual(
            evidence["bodySha256"], hashlib.sha256(b"down").hexdigest()
        )

    def test_a_redirect_is_not_evidence_of_a_healthy_site(self) -> None:
        # 3xx passes once: let healthPath always return to 304, you can get ok=true and add an empty response body.
        # The hash is cached by revision and never retrieved later. Followed redirects are even worse -
        # operator is in the netpol release list of the registry, one pointing to /v2/_catalog
        # Location will cause the control plane to use other people's repo list hashes as evidence for this site.
        def handler(_request, timeout=None):
            raise urllib.error.HTTPError(
                "http://demo", 304, "not modified", {}, io.BytesIO(b"")
            )

        evidence = self._probe(handler)
        self.assertFalse(evidence["ok"])
        self.assertEqual(evidence["httpStatus"], 304)

    def test_a_2xx_other_than_200_still_passes(self) -> None:
        # Positive comparison. Without this clause, the excessive narrowing of "only recognize 200" would also allow the above clause to pass.
        # And that would fail a health path that normally returns 204.
        class Response:
            status = 204

            def read(self, _size):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        evidence = self._probe(lambda _request, timeout=None: Response())
        self.assertTrue(evidence["ok"])
        self.assertEqual(evidence["httpStatus"], 204)

    def test_the_verify_opener_refuses_to_follow_redirects(self) -> None:
        # Directly validate the mechanism itself rather than its effect: returning None from redirect_request will cause urllib to
        # Throw the 3xx as HTTPError as is, and the second address is blocked before being sent.
        self.assertIsNone(
            _NoRedirect().redirect_request(
                None, None, 302, "found", {}, "http://elsewhere.invalid"
            )
        )

    def test_a_passing_check_is_reused_until_the_revision_changes(self) -> None:
        passed = {
            "ok": True,
            "revision": "rev-1",
            "bodySha256": "a" * 64,
        }
        cr = {"status": {"verification": passed}}

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("should not re-probe a passing revision")

        with patch.object(Operator, "_verify_workload", fail_if_called):
            self.assertEqual(
                self.operator._verification_for(cr, self.spec, "ulocal-local"),
                passed,
            )

        # If you change the revision, you have to obtain evidence again - the old evidence cannot endorse the new deployment.
        self.spec["revision"] = "rev-2"
        with patch.object(
            Operator, "_verify_workload", lambda *_a, **_k: {"ok": True}
        ):
            self.assertEqual(
                self.operator._verification_for(cr, self.spec, "ulocal-local"),
                {"ok": True, "consecutiveFailures": 0},
            )

    def test_a_failed_check_is_retried_on_the_next_pass(self) -> None:
        cr = {"status": {"verification": {"ok": False, "revision": "rev-1"}}}
        with patch.object(
            Operator, "_verify_workload", lambda *_a, **_k: {"ok": True}
        ):
            self.assertEqual(
                self.operator._verification_for(cr, self.spec, "ulocal-local"),
                {"ok": True, "consecutiveFailures": 0},
            )


class ReadinessReportsTheRealBackendTests(unittest.TestCase):
    """/readyz must report the actual backend.

    The probe must report the configured backend instead of a hard-coded value.
    Check the database - this kind of "external signals say something different from what is actually running" is the hardest to react to.
    """

    def _probe(self, backend: str) -> dict:
        responses: list = []
        handler = object.__new__(Handler)
        handler.path = "/readyz"
        handler.store = _FakeReadyStore(backend)
        handler._json = lambda status, payload: responses.append(
            (status, payload)
        )
        handler.do_GET()
        return responses[-1][1]

    def test_readyz_names_the_backend_actually_in_use(self) -> None:
        self.assertEqual(self._probe("postgresql")["database"], "postgresql")
        self.assertEqual(self._probe("postgresql")["database"], "postgresql")


class MetricsEndpointTests(unittest.TestCase):
    def test_metrics_expose_the_sites_control_plane_identity(self) -> None:
        payload = _render_metrics()
        self.assertIn("sites_api_up 1", payload)
        self.assertIn("sites_api_uptime_seconds ", payload)
        self.assertNotIn("appforge", payload.lower())

    def test_metrics_are_public_and_prometheus_compatible(self) -> None:
        responses: list[tuple[int, str, str]] = []
        handler = object.__new__(Handler)
        handler.path = "/metrics"
        handler._text = lambda status, payload, content_type: responses.append(
            (status, payload, content_type)
        )
        handler.do_GET()
        self.assertEqual(responses[0][0], 200)
        self.assertIn("version=0.0.4", responses[0][2])
        self.assertIn("# TYPE sites_api_up gauge", responses[0][1])


class TenantResourceQuotaTests(unittest.TestCase):
    """The resource total amount gate of the tenant Namespace.

    The quota for the number of deployments cannot stop the volume: three deployments can each require one core and half a G. This layer is handed over to
    Kubernetes enforces rather than calculates on the API side - it manages the actual created Pods, and there is no way around it.
    """

    @staticmethod
    def _spec(merchant=DEFAULT_MERCHANT_ID, user="acme", quota=None):
        spec = {"merchantID": merchant, "userID": user}
        if quota is not None:
            spec["tenantQuota"] = quota
        return spec

    def test_quota_lands_in_the_tenants_own_namespace(self) -> None:
        quota = resource_quota_resource(self._spec())
        self.assertEqual(
            quota["metadata"]["namespace"],
            namespace_for_tenant(DEFAULT_MERCHANT_ID, "acme"),
        )
        self.assertNotEqual(
            resource_quota_resource(self._spec(user="globex"))["metadata"]["namespace"],
            quota["metadata"]["namespace"],
        )
        # Tenants with the same name under two merchants also each have a quota and do not share the same Namespace.
        self.assertNotEqual(
            resource_quota_resource(self._spec(merchant="other"))["metadata"]["namespace"],
            quota["metadata"]["namespace"],
        )

    def test_quota_caps_cpu_memory_and_pods(self) -> None:
        hard = resource_quota_resource(self._spec())["spec"]["hard"]
        self.assertEqual(set(hard), {"limits.cpu", "limits.memory", "pods"})

    def test_quota_comes_from_the_cr_not_from_this_process(self) -> None:
        """🔴 Value must be taken from CR spec.

        The operator is not connected to the database (the entire file refers to Store at zero points), and the value classified by merchant is only sites-api.
        Know. If taken from the environment variables of the process, all tenants will always get the same upper limit - paying merchants and free prostitutes
        The resource ceilings of merchants are exactly the same, and that is what should be graded most.
        """
        hard = resource_quota_resource(
            self._spec(quota={"cpu": "16", "memory": "32Gi", "pods": "64"})
        )["spec"]["hard"]
        self.assertEqual(
            hard,
            {"limits.cpu": "16", "limits.memory": "32Gi", "pods": "64"},
        )

    def test_missing_or_partial_quota_falls_back_to_defaults(self) -> None:
        """Merchants that have not been configured and are only given half of the specifications will fall to the deployment-level default value.

        Fall back instead of reporting an error: This field is added later, it does not exist in the existing CR, and reconcile
        It is used every two seconds - throwing an exception here is equivalent to stopping all old sites from converging.
        """
        from sites.k8s_resources import default_tenant_quota

        defaults = default_tenant_quota()
        for value in (None, {}, {"cpu": "8"}, "not-a-dict"):
            with self.subTest(value=value):
                hard = resource_quota_resource(self._spec(quota=value))["spec"]["hard"]
                self.assertEqual(hard["limits.memory"], defaults["memory"])
                self.assertEqual(hard["pods"], defaults["pods"])
        # The given half must take effect and cannot be overwritten by the default value.
        hard = resource_quota_resource(self._spec(quota={"cpu": "8"}))["spec"]["hard"]
        self.assertEqual(hard["limits.cpu"], "8")

    def test_the_operator_applies_it_alongside_the_namespace(self) -> None:
        # Without this step, the quota list will not take effect no matter how well it is written.
        kube = FakeKube()
        spec = normalize_deploy_payload(
            {"name": "web", "image": "x/y:1", "port": 8080, "healthPath": "/"},
            DEFAULT_MERCHANT_ID,
            "acme",
        )
        Operator(kube)._apply_workload(
            spec, namespace_for_tenant(DEFAULT_MERCHANT_ID, "acme")
        )
        applied = [
            path for _collection, path, _body in kube.created
            if "resourcequotas" in path
        ]
        self.assertTrue(
            applied, [path for _c, path, _b in kube.created]
        )
        self.assertIn(
            namespace_for_tenant(DEFAULT_MERCHANT_ID, "acme"), applied[0]
        )


class PortMappingContractTests(unittest.TestCase):
    """The independent repository topology and the formula for calculating the URL of the control plane must be consistent.

    These two places are two ways of writing the same fact: port pool and URL formula (NODE_PORT_MIN/MAX/
    EXCLUDED, PUBLIC_URL_HOST, HOST_PORT_BASE) in sites/exposure.py, host mapping in
    PORT_FORWARDS of topology.py. The performance of drifting is difficult to check: the deployment is normal and the control plane is collected.
    The detection also passed (it used the address within the cluster), and it was only discovered when the user took the returned URL to type.
    Can't open.
    """

    def _profile(self) -> dict:
        from sites import topology

        return {
            "port_forwards": topology.PORT_FORWARDS,
        }

    def _mappings(self) -> dict[int, int]:
        forwards = self._profile()["port_forwards"]
        return {int(entry["guest"]): int(entry["host"]) for entry in forwards}

    def test_every_pool_port_is_mapped_to_the_host(self) -> None:
        mappings = self._mappings()
        missing = [port for port in NODE_PORT_RANGE if port not in mappings]
        self.assertEqual(missing, [], "The port in the pool does not have a host portForward mapping")

    def test_pool_ports_have_unique_host_mappings(self) -> None:
        # The local kubeadm topology expresses node ownership differently than a production deployment; the independent contract only requires
        # Each port in the pool has a unique host mapping, and the specific node is determined by the deployment implementation.
        forwards = self._profile()["port_forwards"]
        hosts = [entry["host"] for entry in forwards if entry["guest"] in NODE_PORT_RANGE]
        self.assertEqual(len(hosts), len(set(hosts)))

    def test_the_mapping_matches_the_url_formula(self) -> None:
        # The only source of truth for URL formulas is in sites/exposure.py - just take the public_url from the backend
        # Reconciling port_forwards, what is nailed is the real invariant of "exposure constant ↔ host mapping".
        mappings = self._mappings()
        for port in NODE_PORT_RANGE:
            self.assertEqual(
                _exposure.NodePortExposure().public_url({"nodePort": port}),
                f"http://127.0.0.1:{mappings[port]}",
                f"nodePort {port} The URL does not match the portForward mapping",
            )

    def test_there_is_exactly_one_source_for_the_pod_cidr(self) -> None:
        # 🔴 Successor to test_cluster_cidrs_match_the_profile, which asserted
        # that k8s_resources.CLUSTER_POD_CIDR equalled topology.POD_CIDR -- two
        # literals in this repository -- while its comment claimed it pinned
        # them against a `kubeadm_profile.py` that lives in another repository
        # and is on no test's path. It could only ever have caught someone
        # editing one of the two copies, and it read as if it caught much more.
        #
        # There are no copies now, so the thing to hold is that: topology must
        # re-export the one loader rather than acquire a second one. Red if a
        # literal reappears in either module.
        self.assertIs(topology.cluster_pod_cidr, cluster_pod_cidr)
        self.assertIs(topology.cluster_service_cidr, cluster_service_cidr)
        for module in (topology, k8s_resources):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                code = "\n".join(
                    line for line in source.splitlines()
                    if not line.lstrip().startswith("#")
                )
                self.assertNotRegex(
                    code,
                    r"""=\s*["'][0-9]+(\.[0-9]+){3}/[0-9]+["']""",
                    f"{module.__name__} assigns a CIDR literal again",
                )

    def test_the_control_plane_port_is_not_in_the_pool(self) -> None:
        # The control plane API occupies a NodePort itself. If the pool does not skip it, it will be the first to be publicly deployed
        # It will knock off the control plane.
        manifest = chart.template(
            "10-control-plane.yaml",
            "--set-string", "service.type=NodePort",
            "--set", f"service.nodePort={sorted(NODE_PORT_EXCLUDED)[0]}",
        )
        declared = int(re.search(r"nodePort: (\d+)", manifest).group(1))
        self.assertIn(declared, NODE_PORT_EXCLUDED)
        self.assertNotIn(declared, NODE_PORT_RANGE)

    def test_the_single_node_registry_uses_recreate(self) -> None:
        manifest = chart.template("07-build-plane.yaml")
        registry = manifest[manifest.index("kind: Deployment") :]
        self.assertRegex(registry, r"strategy:\n\s+type: Recreate")


def _open_ipblock_peers(*overrides: str) -> list[tuple[str, str, dict]]:
    """Every ``ipBlock`` peer the chart renders, as (policy, direction, block)."""
    peers = []
    for document in yaml.safe_load_all(chart.render(*overrides)):
        if not document or document.get("kind") != "NetworkPolicy":
            continue
        name = document["metadata"]["name"]
        for direction, key in (("ingress", "from"), ("egress", "to")):
            for rule in document.get("spec", {}).get(direction) or []:
                for peer in rule.get(key) or []:
                    if "ipBlock" in peer:
                        peers.append((name, direction, peer["ipBlock"]))
    return peers


class ChartPodCidrExclusionTests(unittest.TestCase):
    """Every open ipBlock in the chart must exclude the *configured* Pod CIDR.

    🔴 Three NetworkPolicy rules are "allow 0.0.0.0/0 except the Pod CIDR", and
    the exclusion is their entire content: sites-registry keeps ordinary
    workloads off the anonymous read-only registry port that bypasses the
    authentication plane, sites-activator keeps them off :9090 whose
    /scale-metrics answers are per site, and sites-builder is the tenant-code
    egress list.  A Pod CIDR that matches no Pod excludes nothing, and the
    NetworkPolicy still parses, still applies and still looks right in
    `kubectl get netpol`.  The failure looks like a working cluster.

    🔴 The value is *rendered twice, with two different CIDRs*, and that is the
    point.  Pinning one known value cannot tell a rendered `{{ .Values... }}`
    from a hardcoded literal that happens to equal it -- which is exactly the
    state this chart was in, and an earlier version of this test would have
    stayed green through the whole fix.  A literal left behind follows neither
    render; a templated one follows both.
    """

    OTHER_CIDR = "10.77.0.0/16"

    def setUp(self) -> None:
        self.assertNotEqual(
            chart.TEST_POD_CIDR,
            self.OTHER_CIDR,
            "the two renders must differ or this class proves nothing",
        )

    def _peers_for(self, cidr: str) -> list[tuple[str, str, dict]]:
        return _open_ipblock_peers("--set-string", f"clusterNetwork.podCIDR={cidr}")

    def test_every_open_ipblock_follows_the_configured_pod_cidr(self) -> None:
        for cidr in (chart.TEST_POD_CIDR, self.OTHER_CIDR):
            peers = self._peers_for(cidr)
            # Not a count for its own sake: it is the other half of the
            # assertion. If a policy is deleted or a rule loses its ipBlock the
            # loop below has nothing left to check and would report green over
            # an empty list.
            self.assertEqual(
                3, len(peers),
                f"the chart's ipBlock rules changed shape; re-read them: {peers}",
            )
            for name, direction, block in peers:
                with self.subTest(cidr=cidr, policy=name, direction=direction):
                    self.assertEqual("0.0.0.0/0", block.get("cidr"))
                    self.assertIn(
                        cidr, block.get("except") or [],
                        f"{name} {direction} opens 0.0.0.0/0 without excluding "
                        f"the configured Pod CIDR {cidr}",
                    )

    def test_no_rule_keeps_the_old_literal_when_another_cidr_is_configured(self) -> None:
        # The direction the assertion above cannot cover on its own: a rule that
        # excluded *both* the configured value and a leftover literal would
        # satisfy it while still carrying the stale copy.
        for _name, _direction, block in self._peers_for(self.OTHER_CIDR):
            self.assertEqual([self.OTHER_CIDR], [
                entry for entry in (block.get("except") or [])
                if entry not in {
                    cluster_service_cidr(),
                    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                }
            ])

    def test_the_chart_refuses_to_render_without_a_pod_cidr(self) -> None:
        # No default anywhere: an unconfigured install must fail at render time,
        # not produce policies whose exclusions match nothing. Red if a default
        # is reintroduced in values.yaml or the `required` call is dropped.
        result = subprocess.run(
            ["helm", "template", "site", str(chart.CHART), "--namespace", "sites-local"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(0, result.returncode, result.stdout[:400])
        self.assertIn("clusterNetwork.podCIDR", result.stdout + result.stderr)

    def test_the_three_rules_are_the_ones_this_test_believes_they_are(self) -> None:
        # Names, so that "3 peers" cannot be satisfied by three unrelated rules
        # after a refactor moves the real ones somewhere this sweep misses.
        self.assertEqual(
            {("sites-activator", "ingress"),
             ("sites-builder", "egress"),
             ("sites-registry", "ingress")},
            {(name, direction) for name, direction, _block in _open_ipblock_peers()},
        )


class ManifestContractTests(unittest.TestCase):
    """The CRD schema and spec contract cannot drift.

    SiteDeployment's CRD is not enabled x-kubernetes-preserve-unknown-fields, API server
    Fields not declared in the schema will be cut directly. That kind of failure is all green in local rendering and single testing, only in
    On a real cluster, the performance is "Deployment is successful but the workload is missing configuration", so there must be an assertion to pin it.
    """

    def test_every_spec_field_is_declared_in_the_crd(self) -> None:
        declared = _crd_spec_properties()
        self.assertIn("serviceName", declared)
        self.assertGreater(len(declared), 10)

        spec = normalize_deploy_payload(
            {
                "name": "demo",
                "image": "example.invalid/demo:v1",
                "port": 8080,
                "healthPath": "/readyz",
                "livenessPath": "/healthz",
                "exposure": "internal",
                "runAsUser": 10005,
                "env": [
                    {"name": "MODE", "value": "prod"},
                    {
                        "name": "TOKEN",
                        "secretKeyRef": {"name": "keys", "key": "token"},
                    },
                ],
                "secretMounts": [
                    {"secretName": "keys", "mountPath": "/var/run/keys"}
                ],
            },
            DEFAULT_MERCHANT_ID,
            "local",
        )
        self.assertEqual(set(spec) - declared, set())

        static_spec = normalize_deploy_payload(
            {
                "name": "demo",
                "image": "example.invalid/demo:v1",
                "artifact": {"files": {"index.html": "<h1>hi</h1>"}},
            },
            DEFAULT_MERCHANT_ID,
            "local",
        )
        self.assertEqual(set(static_spec) - declared, set())

    def test_crd_component_roles_match_the_implementation(self) -> None:
        manifest = chart.template("00-platform.yaml")
        block = manifest[manifest.index("                componentRole:") :][:200]
        self.assertIn("- app", block)
        self.assertIn("- static", block)
        # The old three roles must disappear from the schema together, otherwise CR will accept it but the implementation will reject it.
        for legacy in ("- backend", "- gateway", "- web"):
            self.assertNotIn(legacy, block)


class FakeKube:
    """Records the writes an Operator makes, without a cluster."""

    def __init__(self, deployment: dict | None = None) -> None:
        self.deployment = deployment or {"metadata": {"generation": 1}, "status": {}}
        self.deleted: list[str] = []
        self.patched: list[tuple[str, dict]] = []
        self.created: list[tuple[str, str, dict]] = []

    def get(self, path: str) -> dict:
        return self.deployment

    def delete(self, path: str) -> None:
        self.deleted.append(path)

    def patch(self, path: str, body: dict) -> dict:
        self.patched.append((path, body))
        return body

    def create_or_patch(self, collection: str, path: str, body: dict) -> dict:
        self.created.append((collection, path, body))
        return body


def _site_deployment_cr(**status: object) -> dict:
    return {
        "metadata": {
            "name": cr_name_for(DEFAULT_MERCHANT_ID, "local", "web"),
            "generation": 1,
            "finalizers": [FINALIZER],
        },
        "spec": {
            "merchantID": DEFAULT_MERCHANT_ID,
            "userID": "local",
            "serviceName": "web",
            "image": "web:sites-local",
            "port": 8086,
            "healthPath": "/",
            "componentRole": "app",
            "exposure": "public",
            "nodePort": 30080,
        },
        "status": {"observedGeneration": 1, **status},
    }


class OperatorTests(unittest.TestCase):
    def _last_status(self, kube: FakeKube) -> dict:
        return [
            body["status"]
            for path, body in kube.patched
            if path.endswith("/status")
        ][-1]

    def test_cleanup_removes_only_its_own_finalizer(self) -> None:
        kube = FakeKube()
        cr = _site_deployment_cr()
        cr["metadata"]["finalizers"] = ["other.example/keep", FINALIZER]
        Operator(kube)._cleanup(cr)
        finalizers = [
            body["metadata"]["finalizers"]
            for _, body in kube.patched
            if "finalizers" in (body.get("metadata") or {})
        ]
        self.assertEqual(finalizers[-1], ["other.example/keep"])

    def test_unparseable_started_at_still_reaches_the_deploy_timeout(self) -> None:
        kube = FakeKube()
        Operator(kube).reconcile(_site_deployment_cr(startedAt="not-a-timestamp"))
        status = self._last_status(kube)
        self.assertEqual(status["phase"], "Deploying")
        # The repaired value is what lets the next pass compute a real elapsed.
        self.assertIsNotNone(_parse_time(status["startedAt"]))

    def test_stale_started_at_fails_the_deployment(self) -> None:
        kube = FakeKube()
        Operator(kube).reconcile(
            _site_deployment_cr(startedAt="2020-01-01T00:00:00+00:00")
        )
        self.assertEqual(self._last_status(kube)["phase"], "Failed")

    def test_running_requires_completed_rollout(self) -> None:
        ready = {
            "metadata": {"generation": 2},
            "status": {
                "observedGeneration": 2,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        }
        self.assertTrue(_deployment_ready(ready))
        self.assertFalse(
            _deployment_ready(
                {
                    "metadata": {"generation": 2},
                    "status": {
                        "observedGeneration": 1,
                        "updatedReplicas": 1,
                        "availableReplicas": 1,
                    },
                }
            )
        )

    def test_service_is_validated_before_workload_resources(self) -> None:
        kube = FakeKube()
        Operator(kube)._apply_workload(_site_deployment_cr()["spec"], "ulocal-local")
        kinds = [body["kind"] for _, _, body in kube.created]
        self.assertLess(kinds.index("Service"), kinds.index("Deployment"))


class PublicRouteCapacityTests(unittest.TestCase):
    """Port pools and quota counts. The authoritative source is the CR in Kubernetes, not the database snapshot."""

    def _cr(
        self,
        name: str,
        user: str,
        *,
        port: int,
        exposure="public",
        merchant: str = DEFAULT_MERCHANT_ID,
    ) -> dict:
        cr = _site_deployment_cr()
        cr["metadata"]["name"] = name
        cr["spec"]["merchantID"] = merchant
        cr["spec"]["userID"] = user
        cr["spec"]["serviceName"] = name
        cr["spec"]["exposure"] = exposure
        cr["spec"]["nodePort"] = port
        return cr

    def test_taken_ports_map_to_their_holders(self) -> None:
        items = [
            self._cr("a-web", "acme", port=30080),
            self._cr("g-web", "globex", port=30081),
            self._cr("g-api", "globex", port=0, exposure="internal"),
        ]
        self.assertEqual(
            _active_public_ports(items, set()),
            {30080: "a-web", 30081: "g-web"},
        )

    def test_a_redeploy_frees_its_own_port(self) -> None:
        # Redeployment of the same service should not be blocked by the port occupied by its previous version.
        items = [self._cr("a-web", "acme", port=30080)]
        self.assertEqual(_active_public_ports(items, {"a-web"}), {})

    def test_a_deleting_deployment_no_longer_holds_its_port(self) -> None:
        items = [self._cr("a-web", "acme", port=30080)]
        items[0]["metadata"]["deletionTimestamp"] = "2026-08-13T00:00:00Z"
        self.assertEqual(_active_public_ports(items, set()), {})

    def test_counts_are_per_tenant(self) -> None:
        items = [
            self._cr("a-web", "acme", port=30080),
            self._cr("a-api", "acme", port=0, exposure="internal"),
            self._cr("g-web", "globex", port=30081),
        ]
        self.assertEqual(
            _tenant_deployment_count(items, DEFAULT_MERCHANT_ID, "acme", set()), 2
        )
        self.assertEqual(
            _tenant_public_count(items, DEFAULT_MERCHANT_ID, "acme", set()), 1
        )
        self.assertEqual(
            _tenant_deployment_count(items, DEFAULT_MERCHANT_ID, "globex", set()),
            1,
        )
        # A non-existent tenant sees nothing, not someone else's number.
        self.assertEqual(
            _tenant_deployment_count(items, DEFAULT_MERCHANT_ID, "nobody", set()),
            0,
        )

    def test_counts_do_not_leak_across_merchants(self) -> None:
        # Just counting the number of userIDs will cause tenants with the same name under two merchants to occupy each other's quota, and it is bidirectional:
        # If one side deploys one more, the other side will have one less quota, and neither side can see the reason.
        items = [
            self._cr("a-web", "alice", port=30080),
            self._cr("b-web", "alice", port=30082, merchant="other"),
        ]
        self.assertEqual(
            _tenant_deployment_count(items, DEFAULT_MERCHANT_ID, "alice", set()),
            1,
        )
        self.assertEqual(
            _tenant_deployment_count(items, "other", "alice", set()), 1
        )
        self.assertEqual(
            _merchant_deployment_count(items, DEFAULT_MERCHANT_ID, set()), 1
        )
        self.assertEqual(_merchant_deployment_count(items, "other", set()), 1)

    def test_the_pool_is_non_empty_and_skips_reserved_ports(self) -> None:
        self.assertGreater(len(NODE_PORT_RANGE), 1)
        self.assertEqual(len(set(NODE_PORT_RANGE)), len(NODE_PORT_RANGE))
        self.assertFalse(set(NODE_PORT_RANGE) & NODE_PORT_EXCLUDED)


class _FakeReadyStore:
    def __init__(self, backend: str) -> None:
        self.backend = backend

    def ping(self) -> None:
        return None


def _merchant_row(merchant_id: str = DEFAULT_MERCHANT_ID, **overrides) -> dict:
    return {
        "merchant_id": merchant_id,
        "display_name": merchant_id.title(),
        "api_key_sha256": token_digest(f"sitem_{merchant_id}"),
        "max_tenants": 100,
        "max_deployments": 100,
        "created_at": None,
        "disabled_at": None,
        # Acting for a subject is off unless the row says otherwise, exactly as the column
        # defaults. A fixture that granted it by default would make the "unauthorized key
        # is refused" case the only one anybody remembers to write.
        "may_act_as_subjects": False,
        "key_expires_at": None,
        **overrides,
    }


class _FakeTenantStore:
    """Minimal stand-in for tenant table and merchant table.

    Both tables must be answered: the authentication side press contract must check both (cannot rely on the certificate prefix to divert), and only implement
    One will make "the other path has not been walked at all" behave exactly like "cannot be found".
    The key of ``tokens`` can be user_id (falling under the default merchant) or (merchant_id, user_id).
    """

    def merchant_resources(self, merchant_id: str):
        """No resource package configured = use deployment-level default values.

        Put it on the base class instead of filling in substitutes one by one: When the real Store adds methods, the substitutes that lack methods will be replaced by
        AttributeError explodes on the spot - that's a good thing, but if each subclass has to fix it again, it will leak.
        """
        return None

    def __init__(
        self,
        tokens: dict,
        *,
        merchants: list[dict] | None = None,
        merchant_keys: dict | None = None,
    ) -> None:
        self.merchants = {
            record["merchant_id"]: record
            for record in (merchants or [_merchant_row()])
        }
        for merchant_id, key in (merchant_keys or {}).items():
            self.merchants[merchant_id]["api_key_sha256"] = token_digest(key)
        self._by_digest = {}
        for key, token in tokens.items():
            merchant_id, user_id = (
                key if isinstance(key, tuple) else (DEFAULT_MERCHANT_ID, key)
            )
            self._by_digest[token_digest(token)] = {
                "merchant_id": merchant_id,
                "user_id": user_id,
                "max_deployments": 3,
                "max_public_routes": 1,
                "disabled_at": None,
            }
        self.created: list[tuple[str, str]] = []

    # --- tenants ---
    def tenant_by_token(self, digest: str):
        record = self._by_digest.get(digest)
        # The actual implementation filters out deactivated tenants in SQL, and the substitutes must be isomorphic.
        if record is None or record.get("disabled_at") is not None:
            return None
        return record

    def tenant(self, merchant_id: str, user_id: str):
        for record in self._by_digest.values():
            if (record["merchant_id"], record["user_id"]) == (
                merchant_id,
                user_id,
            ):
                return record
        return None

    def count_tenants(self, merchant_id: str) -> int:
        return sum(
            1
            for record in self._by_digest.values()
            if record["merchant_id"] == merchant_id
        )

    def create_tenant(
        self,
        merchant_id: str,
        user_id: str,
        token_sha256: str,
        *,
        max_deployments: int,
        max_public_routes: int,
    ) -> None:
        self.created.append((merchant_id, user_id))
        self._by_digest[token_sha256] = {
            "merchant_id": merchant_id,
            "user_id": user_id,
            "max_deployments": max_deployments,
            "max_public_routes": max_public_routes,
            "disabled_at": None,
        }

    # --- merchants ---
    def merchant(self, merchant_id: str):
        return self.merchants.get(merchant_id)

    def merchant_by_api_key(self, digest: str):
        for record in self.merchants.values():
            if record["api_key_sha256"] != digest:
                continue
            # Deactivated merchants cannot be found by key: the certificate itself should be invalid.
            if record["disabled_at"] is not None:
                return None
            # Isomorphic with the SQL: an expired key resolves to nothing, so it lands in
            # the same 401 as an unknown one instead of announcing that it once existed.
            expires_at = record.get("key_expires_at")
            if expires_at is not None and expires_at <= dt.datetime.now(dt.timezone.utc):
                return None
            return record
        return None


class FakeApiKube:
    """Serves the two collections sites-api reads before it mutates."""

    def __init__(
        self,
        deployments: list[dict] | None = None,
        builds: list[dict] | None = None,
    ) -> None:
        self.deployments = deployments if deployments is not None else []
        self.builds = builds if builds is not None else []
        self.deleted: list[tuple[str, dict | None]] = []
        self.created: list[tuple[str, dict]] = []
        self.patched: list[tuple[str, dict]] = []

    def _named(self, items: list[dict], path: str) -> dict:
        name = path.rsplit("/", 1)[-1]
        for item in items:
            if (item.get("metadata") or {}).get("name") == name:
                return item
        raise ApiError(404, "not found")

    def get(self, path: str) -> dict:
        if path == COLLECTION_PATH:
            return {"items": self.deployments}
        if path == BUILD_COLLECTION_PATH:
            return {"items": self.builds}
        if path.startswith(f"{COLLECTION_PATH}/"):
            return self._named(self.deployments, path)
        if path.startswith(f"{BUILD_COLLECTION_PATH}/"):
            return self._named(self.builds, path)
        raise ApiError(404, "not found")

    def delete(self, path: str, body: dict | None = None) -> dict:
        self.deleted.append((path, body))
        return {}

    def create(self, path: str, body: dict) -> dict:
        self.created.append((path, body))
        if path == BUILD_COLLECTION_PATH:
            self.builds.append(body)
        elif path == COLLECTION_PATH:
            self.deployments.append(body)
        return body

    def patch(self, path: str, body: dict) -> dict:
        self.patched.append((path, body))
        return body

    def create_or_patch(self, collection: str, path: str, body: dict) -> dict:
        # The same semantics as kube.KubeClient.create_or_patch: create it if it does not exist, and modify it if it exists.
        # The lack of this method in the fixture will make the entire successful path of POST /v1/deployments unreachable——
        # Only testing results in rejection, but not release, and "Always reject" can also make rejection cases all green.
        items = self.deployments if collection == COLLECTION_PATH else self.builds
        try:
            existing = self._named(items, path)
        except ApiError:
            return self.create(collection, body)
        existing.update(body)
        self.patched.append((path, body))
        return existing


class _RecordingStore:
    def __init__(self) -> None:
        self.statuses: list[tuple] = []
        self.upserts: list[tuple] = []
        # The authentication side needs to check the merchant table, so even a stand-in that "only remembers set_status" must be able to answer the question.
        self._identity = _FakeTenantStore({})

    def merchant(self, merchant_id: str):
        return self._identity.merchant(merchant_id)

    def merchant_resources(self, merchant_id: str):
        # No resource package configured = use deployment-level default values. The avatar follows the real interface: when a method is missing
        # AttributeError will explode on the spot, which is better than quietly falling back to another behavior.
        return None

    def merchant_by_api_key(self, digest: str):
        return self._identity.merchant_by_api_key(digest)

    def tenant(self, merchant_id: str, user_id: str):
        return self._identity.tenant(merchant_id, user_id)

    def get_deployment(
        self, merchant_id: str, user_id: str, service_name: str
    ) -> dict:
        return {
            "cr_name": cr_name_for(merchant_id, user_id, service_name),
            "deleted_at": None,
        }

    def set_status(self, *args: object) -> None:
        self.statuses.append(args)

    def upsert_site_deployment(self, *args: object, **kwargs: object) -> None:
        self.upserts.append(args)


class ServiceNameOwnershipTests(unittest.TestCase):
    """An SiteBuild and the SiteDeployment it produces share one CR name."""

    SERVICE_TOKEN = "s" * 32

    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _handler(
        self,
        kube: FakeApiKube,
        *,
        store: object | None = None,
    ) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.kube = kube
        handler.store = store if store is not None else _FakeTenantStore({})
        handler.mutation_lock = threading.Lock()
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    @staticmethod
    def _source_request() -> dict:
        return {"name": "web", "files": {"Dockerfile": "FROM scratch\n"}}

    @staticmethod
    def _build_cr(name: str, *, deleting: bool = False) -> dict:
        metadata: dict = {"name": name}
        if deleting:
            metadata["deletionTimestamp"] = "2026-08-13T00:00:00Z"
        return {
            "metadata": metadata,
            "spec": {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": DEFAULT_USER_ID,
                "serviceName": name.split("-")[-1],
            },
        }

    def test_a_build_cannot_adopt_an_existing_deployment(self) -> None:
        # Both objects are keyed by cr_name_for(user, service): without this
        # check the build's first reconcile would overwrite the deployment's
        # SiteDeployment and take over its workload.
        kube = FakeApiKube(deployments=[_site_deployment_cr()])
        handler = self._handler(kube)
        handler._read_body = lambda *_args, **_kwargs: self._source_request()
        handler._post_build()
        status, payload = self.responses[-1]
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "service_name_conflict")

    def test_deleting_a_built_service_names_the_builds_endpoint(self) -> None:
        # DELETE /v1/deployments only removes the SiteDeployment, which
        # reconcile_build recreates two seconds later while the database record
        # silently flips back from Deleting to Pending.
        kube = FakeApiKube(
            deployments=[_site_deployment_cr()],
            builds=[
                self._build_cr(cr_name_for(DEFAULT_MERCHANT_ID, "local", "web"))
            ],
        )
        store = _RecordingStore()
        handler = self._handler(kube, store=store)
        handler.path = "/v1/deployments/web"
        handler.do_DELETE()
        status, payload = self.responses[-1]
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "service_name_conflict")
        self.assertIn("/v1/builds/web", payload["error"])
        self.assertEqual(kube.deleted, [])
        self.assertEqual(store.statuses, [])

    def test_a_deployment_cannot_adopt_an_existing_build(self) -> None:
        # Reverse direction. The one above is about "build grabs the deployment name", this one is about
        # "Deployment grabs the name of build" - the same invariant originally only protected three directions.
        # If you omit it, an error will not be reported, but it will become a see-saw: create_or_patch will create CR's image/nodePort
        # If it is changed to the caller's, the operator will be reconciled in the next round and patched back according to the result of the build.
        name = cr_name_for(DEFAULT_MERCHANT_ID, DEFAULT_USER_ID, "web")
        kube = FakeApiKube(builds=[self._build_cr(name)])
        handler = self._handler(kube, store=_RecordingStore())
        handler.path = "/v1/deployments"
        handler._read_body = lambda *_a, **_k: {
            "name": "web",
            "image": "nginx:1",
        }
        handler.do_POST()
        status, payload = self.responses[-1]
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "service_name_conflict")
        self.assertIn("/v1/builds/web", payload["error"])
        # The conflict must occur before any write: rejection but the CR has been changed or the database has been dropped, which is equivalent to no rejection.
        self.assertEqual(handler.store.upserts, [])

    def test_a_deleting_build_no_longer_blocks_a_new_deployment(self) -> None:
        # Positive comparison. Without this one, the above one can be passed by using "reject any build with the same name".
        # And that will make it impossible to build a deployment with the same name after deleting build.
        name = cr_name_for(DEFAULT_MERCHANT_ID, DEFAULT_USER_ID, "web")
        kube = FakeApiKube(builds=[self._build_cr(name, deleting=True)])
        handler = self._handler(kube, store=_RecordingStore())
        handler.path = "/v1/deployments"
        handler._read_body = lambda *_a, **_k: {
            "name": "web",
            "image": "nginx:1",
        }
        handler.do_POST()
        self.assertEqual(self.responses[-1][0], 202)

    def test_a_deleting_build_no_longer_blocks_the_deployment_delete(self) -> None:
        kube = FakeApiKube(
            deployments=[_site_deployment_cr()],
            builds=[
                self._build_cr(
                    cr_name_for(DEFAULT_MERCHANT_ID, "local", "web"),
                    deleting=True,
                )
            ],
        )
        handler = self._handler(kube, store=_RecordingStore())
        handler.path = "/v1/deployments/web"
        handler.do_DELETE()
        self.assertEqual(self.responses[-1][0], 202)
        self.assertEqual(len(kube.deleted), 1)


class AdminConsoleContractTests(unittest.TestCase):
    """The management response must match the field name actually read by the console.

    These two endpoints have not been tested before on the Python side or the console side, and the console mock
    Realizes "more correctness" than the real thing (mock will filter by merchantId, mock returns repositories) - frontend
    It is completely self-consistent with the mock, and defects only exist when the real backend is connected, so it can always survive.
    """

    SERVICE_TOKEN = "s" * 32

    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _handler(self, kube: object) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.kube = kube
        handler.store = _FakeTenantStore({})
        handler.mutation_lock = threading.Lock()
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    @staticmethod
    def _build_cr(name: str, merchant: str, user: str) -> dict:
        return {
            "metadata": {"name": name},
            "spec": {
                "merchantID": merchant,
                "userID": user,
                "serviceName": name,
            },
        }

    def test_admin_builds_honours_the_merchant_filter(self) -> None:
        kube = FakeApiKube(
            builds=[
                self._build_cr("a-web", "acme", "alice"),
                self._build_cr("o-web", "other", "bob"),
            ]
        )
        handler = self._handler(kube)
        # Forward comparison: First prove that the construction of both companies is indeed there, otherwise "filter constant space" will also make the bottom all green.
        handler._admin_builds({})
        self.assertEqual(self.responses[-1][1]["count"], 2)

        handler._admin_builds({"merchantId": "acme"})
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["builds"][0]["merchantId"], "acme")
        # The two identities are indispensable: user_id is only unique within the merchant.
        self.assertEqual(payload["builds"][0]["userId"], "alice")

    def test_admin_images_uses_the_names_the_console_reads(self) -> None:
        def fake_get(path: str) -> dict:
            if path == "/v2/_catalog":
                return {"repositories": ["local/acme/alice/web"]}
            return {"tags": ["v1"]}

        handler = self._handler(FakeApiKube())
        with patch("sites.api_admin.registry_get", side_effect=fake_get), patch(
            "sites.api_admin.registry_manifest_digest",
            return_value="sha256:abc",
        ):
            handler._admin_images()
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        # AdminImageListResponse of console/src/types.ts reads repositories[].name,
        # The server once sent images[].repository - the key name did not match, and the image page was always empty.
        self.assertIn("repositories", payload)
        self.assertNotIn("images", payload)
        entry = payload["repositories"][0]
        self.assertEqual(entry["name"], "local/acme/alice/web")
        self.assertEqual(entry["tags"][0]["tag"], "v1")
        self.assertEqual(entry["tags"][0]["digest"], "sha256:abc")

    def test_an_unreachable_registry_does_not_fail_the_whole_page(self) -> None:
        handler = self._handler(FakeApiKube())
        with patch(
            "sites.api_admin.registry_get",
            side_effect=RuntimeError("connect timeout"),
        ):
            handler._admin_images()
        status, payload = self.responses[-1]
        # Returning 503 will cause Promise.all on the console page to reject as a whole, and the normal build will be
        # The table is cleared together. The contract requires 200 + empty list + registry field to explain the reason.
        self.assertEqual(status, 200)
        self.assertEqual(payload["repositories"], [])
        self.assertFalse(payload["registry"]["reachable"])
        self.assertIn("connect timeout", payload["registry"]["error"])


class TransportFailureTests(unittest.TestCase):
    """When kube-apiserver is unreachable, each route must return 502 instead of losing the connection.

    kube.py throws **naked RuntimeError** for URLError/TimeoutError, while ApiError is
    Its subclasses - the parent class cannot be caught. Routes that only write except ApiError will throw uncaught exceptions.
    BaseHTTPRequestHandler closes the connection without writing a response, and the caller gets an Empty reply.
    """

    SERVICE_TOKEN = "s" * 32

    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    class _UnreachableKube(FakeApiKube):
        def get(self, path: str) -> dict:
            if "/" in path.replace(COLLECTION_PATH, "").replace(
                BUILD_COLLECTION_PATH, ""
            ):
                raise RuntimeError("Kubernetes API unavailable: timed out")
            return super().get(path)

        def delete(self, path: str, body: dict | None = None) -> dict:
            raise RuntimeError("Kubernetes API unavailable: timed out")

    class _UnreachableDeleteKube(FakeApiKube):
        """Reads still answer (the build-ownership lookup 404s); only the delete times out.

        DELETE /v1/deployments has two separate critical sections and two separate
        RuntimeError handlers. A kube that fails every call only ever reaches the
        first one, so the second handler stayed uncovered.
        """

        def delete(self, path: str, body: dict | None = None) -> dict:
            raise RuntimeError("Kubernetes API unavailable: timed out")

    def _handler(self, kube: FakeApiKube | None = None) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.kube = kube if kube is not None else self._UnreachableKube()
        handler.store = _RecordingStore()
        handler.mutation_lock = threading.Lock()
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    def test_getting_one_deployment_reports_502(self) -> None:
        handler = self._handler()
        handler.path = "/v1/deployments/web"
        handler.do_GET()
        self.assertEqual(self.responses[-1][0], 502)

    def test_getting_one_build_reports_502(self) -> None:
        handler = self._handler()
        handler.path = "/v1/builds/web"
        handler.do_GET()
        self.assertEqual(self.responses[-1][0], 502)

    def test_deleting_one_build_reports_502(self) -> None:
        handler = self._handler()
        handler.path = "/v1/builds/web"
        handler.do_DELETE()
        self.assertEqual(self.responses[-1][0], 502)

    def test_deleting_one_deployment_reports_502_when_the_ownership_lookup_fails(
        self,
    ) -> None:
        handler = self._handler()
        handler.path = "/v1/deployments/web"
        handler.do_DELETE()
        self.assertEqual(self.responses[-1][0], 502)
        # Nothing was deleted or marked: the request must stop before any write.
        self.assertEqual(handler.store.statuses, [])

    def test_deleting_one_deployment_reports_502_when_the_delete_fails(
        self,
    ) -> None:
        handler = self._handler(self._UnreachableDeleteKube())
        handler.path = "/v1/deployments/web"
        handler.do_DELETE()
        self.assertEqual(self.responses[-1][0], 502)
        # The database record must not flip to Deleting for a delete that never
        # reached the apiserver.
        self.assertEqual(handler.store.statuses, [])


class ControlPlaneBusyTests(unittest.TestCase):
    """A stalled mutation-lock holder must yield a retryable 503, not a hung request.

    The sync thread takes the same lock every 2s around a database transaction; when
    the database accepts the connection but never answers, that thread holds the lock
    indefinitely. Before the timeout every write path queued behind it forever while
    the client gave up at 15s, so the server kept a thread per abandoned request.
    """

    SERVICE_TOKEN = "s" * 32
    QUOTA = {
        "max_deployments": 10,
        "max_public_routes": 10,
        "merchant_max_deployments": 100,
    }

    def setUp(self) -> None:
        self.lock = threading.Lock()
        # Stand-in for a holder that never returns (e.g. sync_snapshot wedged in DB).
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        self.responses: list[tuple[int, dict]] = []

    def _handler(self, kube: FakeApiKube, body: dict) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.kube = kube
        handler.store = _RecordingStore()
        handler.mutation_lock = self.lock
        handler.mutation_lock_timeout = 0.1
        handler._tenant_quota = lambda _merchant_id, _user_id: dict(self.QUOTA)
        handler._read_body = lambda *_args, **_kwargs: body
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    def _run(self, target) -> None:
        # The handler runs on its own thread so a regression back to a blocking
        # acquire shows up as a failed assertion instead of a test that never ends.
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=3.0)
        self.assertFalse(
            thread.is_alive(),
            "the handler must give up on the mutation lock instead of waiting forever",
        )

    def _assert_busy(self, kube: FakeApiKube, handler: Handler) -> None:
        status, payload = self.responses[-1]
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "control_plane_busy")
        # Busy is answered before the critical section: nothing may have been written.
        self.assertEqual(kube.created, [])
        self.assertEqual(kube.deleted, [])
        self.assertEqual(handler.store.upserts, [])
        self.assertEqual(handler.store.statuses, [])

    def test_post_deployment_reports_busy(self) -> None:
        kube = FakeApiKube()
        handler = self._handler(kube, {"name": "web", "image": "nginx:1"})
        self._run(handler._post_deployment)
        self._assert_busy(kube, handler)

    def test_post_build_reports_busy(self) -> None:
        kube = FakeApiKube()
        handler = self._handler(
            kube, {"name": "web", "files": {"Dockerfile": "FROM scratch\n"}}
        )
        self._run(handler._post_build)
        self._assert_busy(kube, handler)

    def test_post_bundle_reports_busy(self) -> None:
        kube = FakeApiKube()
        handler = self._handler(
            kube,
            {"name": "shop", "components": [{"name": "web", "image": "nginx:1"}]},
        )
        self._run(handler._post_bundle)
        self._assert_busy(kube, handler)

    def test_delete_deployment_reports_busy(self) -> None:
        # The delete path has its own except ladder rather than the shared table, so
        # a ControlPlaneBusy caught by its RuntimeError clause would come back as 502.
        kube = FakeApiKube(deployments=[_site_deployment_cr()])
        handler = self._handler(kube, {})
        handler.path = "/v1/deployments/web"
        self._run(handler.do_DELETE)
        self._assert_busy(kube, handler)

    def test_delete_build_reports_busy(self) -> None:
        name = cr_name_for(DEFAULT_MERCHANT_ID, DEFAULT_USER_ID, "web")
        build = {
            "metadata": {"name": name},
            "spec": {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": DEFAULT_USER_ID,
                "serviceName": "web",
            },
        }
        kube = FakeApiKube(builds=[build])
        handler = self._handler(kube, {})
        handler.path = "/v1/builds/web"
        self._run(handler.do_DELETE)
        self._assert_busy(kube, handler)

    def test_delete_bundle_reports_busy(self) -> None:
        kube = FakeApiKube()
        handler = self._handler(kube, {})
        handler._bundle_objects = lambda *_args: [_site_deployment_cr()]
        handler.path = "/v1/bundles/shop"
        self._run(handler.do_DELETE)
        self._assert_busy(kube, handler)


class _SlowListKube(FakeApiKube):
    """FakeApiKube whose deployment listing snapshots, then stalls inside the critical section.

    The snapshot is copied *before* the sleep. Without the copy the fake would hand
    out the live list, and a concurrent create could leak into a snapshot taken
    earlier - the race would then depend on scheduling instead of on the lock.
    """

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def get(self, path: str) -> dict:
        if path == COLLECTION_PATH:
            snapshot = {"items": list(self.deployments)}
            time.sleep(self.delay)
            return snapshot
        return super().get(path)


class ConcurrentAdmissionTests(unittest.TestCase):
    """Two simultaneous POST /v1/deployments must not be admitted against one snapshot.

    Admission reads the collection, decides, then writes; only the mutation lock
    keeps the second request from deciding on the pre-write listing. No other test
    exercised two handlers at once, so removing the lock left the suite green.
    Here the listing stalls long enough that, unlocked, both requests provably read
    the empty snapshot and claim the same port / the same last quota slot.
    """

    SERVICE_TOKEN = "s" * 32
    LIST_DELAY = 0.5

    def _handler(
        self,
        kube: FakeApiKube,
        lock: threading.Lock,
        store: _RecordingStore,
        name: str,
        quota: dict[str, int],
        responses: list[tuple[int, dict]],
    ) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.kube = kube
        handler.store = store
        handler.mutation_lock = lock
        handler._tenant_quota = lambda _merchant_id, _user_id: dict(quota)
        handler._read_body = lambda *_args, **_kwargs: {
            "name": name,
            "image": "nginx:1",
        }
        handler._json = lambda status, payload: responses.append(
            (status, payload)
        )
        return handler

    def _race(self, kube: FakeApiKube, quota: dict[str, int]) -> list[int]:
        lock = threading.Lock()
        store = _RecordingStore()
        # Released together so both requests are in flight before either can
        # take the lock; the barrier sits outside the critical section, so it
        # cannot deadlock against the lock itself.
        gate = threading.Barrier(2)
        statuses: list[int] = []

        def submit(name: str) -> None:
            responses: list[tuple[int, dict]] = []
            handler = self._handler(kube, lock, store, name, quota, responses)
            gate.wait(timeout=2.0)
            handler._post_deployment()
            statuses.append(responses[-1][0])

        threads = [
            threading.Thread(target=submit, args=(name,), daemon=True)
            for name in ("alpha", "beta")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive(), "a request never finished")
        return sorted(statuses)

    @_POOL_ONLY
    def test_concurrent_deployments_get_distinct_node_ports(self) -> None:
        kube = _SlowListKube(self.LIST_DELAY)
        statuses = self._race(
            kube,
            {
                "max_deployments": 10,
                "max_public_routes": 10,
                "merchant_max_deployments": 100,
            },
        )
        self.assertEqual(statuses, [202, 202])
        ports = [item["spec"]["nodePort"] for item in kube.deployments]
        self.assertEqual(len(ports), 2)
        self.assertEqual(
            len(set(ports)), 2, f"both requests were handed the same port: {ports}"
        )

    def test_concurrent_deployments_cannot_overrun_the_quota(self) -> None:
        kube = _SlowListKube(self.LIST_DELAY)
        statuses = self._race(
            kube,
            {
                "max_deployments": 1,
                "max_public_routes": 10,
                "merchant_max_deployments": 100,
            },
        )
        self.assertEqual(statuses, [202, 429])
        self.assertEqual(len(kube.deployments), 1)


class BuildCapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _handler(self, kube: FakeApiKube) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": "s" * 32}
        handler.service_token = "s" * 32
        handler.proxy_token = ""
        handler.kube = kube
        handler.store = _FakeTenantStore({})
        handler.mutation_lock = threading.Lock()
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        handler._read_body = lambda *_args, **_kwargs: {
            "name": "web",
            "files": {"Dockerfile": "FROM scratch\n"},
        }
        return handler

    def test_gateway_source_build_is_accepted_without_a_node_port(self) -> None:
        kube = FakeApiKube()
        handler = self._handler(kube)
        handler._tenant_quota = lambda _merchant_id, _user_id: {
            "max_deployments": 10,
            "max_public_routes": 10,
            "merchant_max_deployments": 100,
        }
        with (
            patch.dict(os.environ, {"SITES_EXPOSURE_BACKEND": "gateway"}),
            patch(
                "sites.api_builds.persist_source",
                return_value="local/local/source/hash",
            ),
        ):
            handler._post_build()
        self.assertEqual(self.responses[-1][0], 202)
        self.assertNotIn("nodePort", kube.builds[-1]["spec"])

    @_POOL_ONLY
    def test_an_unfinished_build_holds_the_single_public_route(self) -> None:
        # A build's SiteDeployment only appears once the image exists, so counting
        # SiteDeployments alone let two builds run to completion before the second
        # one collided on nodePort 30080 — after paying the full build cost.
        building = {
            "metadata": {"name": "local-first"},
            "spec": {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": DEFAULT_USER_ID,
                "serviceName": "first",
                "nodePort": 30080,
            },
            "status": {"phase": "Building"},
        }
        desired = [{"metadata": {"name": "local-second"}, "spec": {"exposure": "public"}}]
        handler = self._handler(FakeApiKube(builds=[building]))
        # When there is only one public route to the pool, one in-progress construction will occupy it.
        quota = {
            "max_deployments": 10,
            "max_public_routes": 10,
            "merchant_max_deployments": 100,
        }
        with patch("sites.exposure.NODE_PORT_RANGE", (30080,)):
            with self.assertRaises(PublicRouteConflict):
                handler._admit_and_assign_ports(
                    DEFAULT_MERCHANT_ID, DEFAULT_USER_ID, desired, quota
                )
            # A failed build never creates a Service, so it holds nothing.
            building["status"]["phase"] = "Failed"
            handler._admit_and_assign_ports(
                DEFAULT_MERCHANT_ID, DEFAULT_USER_ID, desired, quota
            )
        self.assertEqual(desired[0]["spec"]["nodePort"], 30080)

    @_POOL_ONLY
    def test_a_pending_build_reserves_its_concrete_port(self) -> None:
        building = {
            "metadata": {"name": "local-first"},
            "spec": {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": DEFAULT_USER_ID,
                "serviceName": "first",
                "nodePort": 30080,
            },
            "status": {"phase": "Building"},
        }
        desired = [
            {
                "metadata": {"name": "local-second"},
                "spec": {"exposure": "public"},
            }
        ]
        handler = self._handler(FakeApiKube(builds=[building]))
        handler._admit_and_assign_ports(
            DEFAULT_MERCHANT_ID,
            DEFAULT_USER_ID,
            desired,
            {
                "max_deployments": 10,
                "max_public_routes": 10,
                "merchant_max_deployments": 100,
            },
        )
        self.assertEqual(desired[0]["spec"]["nodePort"], 30082)

    def test_a_pending_build_counts_toward_tenant_quotas(self) -> None:
        building = {
            "metadata": {"name": "local-first"},
            "spec": {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": DEFAULT_USER_ID,
                "serviceName": "first",
                "nodePort": 30080,
            },
            "status": {"phase": "Building"},
        }
        desired = [
            {
                "metadata": {"name": "local-second"},
                "spec": {"exposure": "internal"},
            }
        ]
        handler = self._handler(FakeApiKube(builds=[building]))
        with self.assertRaisesRegex(QuotaExceeded, "at most 1 deployments"):
            handler._admit_and_assign_ports(
                DEFAULT_MERCHANT_ID,
                DEFAULT_USER_ID,
                desired,
                {
                    "max_deployments": 1,
                    "max_public_routes": 10,
                    "merchant_max_deployments": 100,
                },
            )

    def test_the_merchant_ceiling_is_checked_before_the_tenant_quota(self) -> None:
        # When the tenants themselves still have surplus and the total number of merchants has reached their limit, reporting the tenant quota will lead the caller to
        # The path of "delete one of your own deployments" is completely ineffective.
        building = {
            "metadata": {"name": "local-first"},
            "spec": {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": DEFAULT_USER_ID,
                "serviceName": "first",
                "nodePort": 30080,
            },
            "status": {"phase": "Building"},
        }
        desired = [
            {
                "metadata": {"name": "local-second"},
                "spec": {"exposure": "internal"},
            }
        ]
        handler = self._handler(FakeApiKube(builds=[building]))
        with self.assertRaisesRegex(
            MerchantQuotaExceeded, "across all its tenants"
        ):
            handler._admit_and_assign_ports(
                DEFAULT_MERCHANT_ID,
                DEFAULT_USER_ID,
                desired,
                {
                    "max_deployments": 10,
                    "max_public_routes": 10,
                    "merchant_max_deployments": 1,
                },
            )

    @_POOL_ONLY
    def test_a_source_build_persists_the_assigned_non_default_port(self) -> None:
        first = _site_deployment_cr()
        first["metadata"]["name"] = "local-first"
        first["spec"].update(
            {"serviceName": "first", "nodePort": 30080}
        )
        second = _site_deployment_cr()
        second["metadata"]["name"] = "local-second"
        second["spec"].update(
            {"serviceName": "second", "nodePort": 30082}
        )
        kube = FakeApiKube(deployments=[first, second])
        handler = self._handler(kube)
        handler._tenant_quota = lambda _merchant_id, _user_id: {
            "max_deployments": 10,
            "max_public_routes": 10,
            "merchant_max_deployments": 100,
        }
        handler._read_body = lambda *_args, **_kwargs: {
            "name": "source",
            "files": {"Dockerfile": "FROM scratch\n"},
        }
        with patch("sites.api_builds.persist_source", return_value="local/source/hash"):
            handler._post_build()
        self.assertEqual(self.responses[-1][0], 202)
        self.assertEqual(kube.builds[-1]["spec"]["nodePort"], 30083)
        # Contract 4.1: Responses from existing endpoints must include merchantId. This item consists of
        # site_build_response provides, api.py no longer assigns values repeatedly - so the assertion should stay here,
        # Otherwise, if the builds.py side removes it, this contract will be silently lost.
        self.assertEqual(
            self.responses[-1][1]["merchantId"], DEFAULT_MERCHANT_ID
        )
        self.assertEqual(
            kube.builds[-1]["spec"]["merchantID"], DEFAULT_MERCHANT_ID
        )

    @_POOL_ONLY
    def test_a_legacy_build_without_a_port_fails_closed(self) -> None:
        legacy = {
            "metadata": {"name": "local-first"},
            "spec": {
                "merchantID": DEFAULT_MERCHANT_ID,
                "userID": DEFAULT_USER_ID,
                "serviceName": "first",
            },
            "status": {"phase": "Building"},
        }
        desired = [
            {
                "metadata": {"name": "local-second"},
                "spec": {"exposure": "public"},
            }
        ]
        handler = self._handler(FakeApiKube(builds=[legacy]))
        with self.assertRaisesRegex(PublicRouteConflict, "delete and resubmit"):
            handler._admit_and_assign_ports(
                DEFAULT_MERCHANT_ID,
                DEFAULT_USER_ID,
                desired,
                {
                    "max_deployments": 10,
                    "max_public_routes": 10,
                    "merchant_max_deployments": 100,
                },
            )

    @_POOL_ONLY
    def test_conflicting_persisted_build_ports_fail_closed(self) -> None:
        builds = [
            {
                "metadata": {"name": f"local-{name}"},
                "spec": {
                    "merchantID": DEFAULT_MERCHANT_ID,
                    "userID": DEFAULT_USER_ID,
                    "serviceName": name,
                    "nodePort": 30080,
                },
                "status": {"phase": "Building"},
            }
            for name in ("first", "second")
        ]
        desired = [
            {
                "metadata": {"name": "local-third"},
                "spec": {"exposure": "public"},
            }
        ]
        handler = self._handler(FakeApiKube(builds=builds))
        with self.assertRaisesRegex(PublicRouteConflict, "both claim 30080"):
            handler._admit_and_assign_ports(
                DEFAULT_MERCHANT_ID,
                DEFAULT_USER_ID,
                desired,
                {
                    "max_deployments": 10,
                    "max_public_routes": 10,
                    "merchant_max_deployments": 100,
                },
            )

    def test_a_delete_and_recreate_loop_is_capped(self) -> None:
        # Builds already marked for deletion pass the public route check but
        # keep their BuildKit Job until the operator's next pass, so a fast
        # loop could stack up 1 CPU / 1Gi Jobs on the single local node.
        builds = [
            {
                "metadata": {
                    "name": f"local-svc{index}",
                    "deletionTimestamp": "2026-08-13T00:00:00Z",
                },
                "spec": {
                    "merchantID": DEFAULT_MERCHANT_ID,
                    "userID": DEFAULT_USER_ID,
                    "serviceName": f"svc{index}",
                },
                "status": {"phase": "Building"},
            }
            for index in range(MAX_ACTIVE_BUILDS)
        ]
        handler = self._handler(FakeApiKube(builds=builds))
        handler._post_build()
        status, payload = self.responses[-1]
        self.assertEqual(status, 429)
        self.assertEqual(payload["code"], "build_capacity")


class ApiAuthenticationTests(unittest.TestCase):
    SERVICE_TOKEN = "s" * 32

    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _handler(
        self,
        headers: dict,
        *,
        tenants: dict | None = None,
        merchants: list[dict] | None = None,
        merchant_keys: dict | None = None,
        local_login_enabled: bool = True,
    ) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = headers
        handler.service_token = self.SERVICE_TOKEN
        handler.session_key = ""
        handler.local_login_enabled = local_login_enabled
        handler.store = _FakeTenantStore(
            tenants or {},
            merchants=merchants,
            merchant_keys=merchant_keys,
        )
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    def test_a_tenant_token_resolves_to_its_own_identity(self) -> None:
        handler = self._handler(
            {"X-Sites-Service-Token": "site_acme"},
            tenants={"acme": "site_acme"},
        )
        self.assertEqual(handler._authenticate(), (DEFAULT_MERCHANT_ID, "acme"))
        self.assertFalse(handler._is_admin())

    def test_a_tenant_token_cannot_act_as_another_tenant(self) -> None:
        # The entrance to multi-tenant isolation: tokens determine identity, and no one's declaration counts.
        handler = self._handler(
            {
                "X-Sites-Service-Token": "site_acme",
                "X-User-ID": "globex",
            },
            tenants={"acme": "site_acme", "globex": "site_globex"},
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)

    def test_declaring_an_identity_is_refused_even_when_it_agrees(self) -> None:
        """🔴 Contract §3.2: the tenant is never chosen by the caller, agreeing or not.

        The previous rule compared the header against the credential and let a matching
        one through, which kept the header a supported input - and a supported input is
        one somebody eventually forwards from a request. There is no comparison left to
        get wrong, because there is no accepted value.
        """
        handler = self._handler(
            {"X-Sites-Service-Token": "site_acme", "X-User-ID": "acme"},
            tenants={"acme": "site_acme"},
        )
        self.assertIsNone(handler._authenticate())
        status, payload = self.responses[-1]
        self.assertEqual(status, 403)
        self.assertIn("X-User-ID", payload["error"])

    def test_an_unknown_token_is_rejected_like_a_wrong_one(self) -> None:
        # Do not distinguish between "no such tenant" and "incorrect token", otherwise this endpoint will become a tenant name detector.
        handler = self._handler(
            {"X-Sites-Service-Token": "site_nobody"},
            tenants={"acme": "site_acme"},
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 401)
        self.assertEqual(self.responses[-1][1]["error"], "invalid service token")

    def test_no_credential_at_all_is_refused(self) -> None:
        # Contract §5.1. Istio's RequestAuthentication accepts an unauthenticated request
        # and simply gives it no identity; "an authentication component is configured" is
        # not "authentication is enforced". This asserts the enforcement.
        handler = self._handler({}, tenants={"acme": "site_acme"})
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 401)
        self.assertFalse(handler._is_admin())

    def test_the_admin_token_still_pins_the_default_identity(self) -> None:
        handler = self._handler(
            {"X-Sites-Service-Token": self.SERVICE_TOKEN},
            tenants={"acme": "site_acme"},
        )
        self.assertTrue(handler._is_admin())
        self.assertEqual(
            handler._authenticate(), (DEFAULT_MERCHANT_ID, DEFAULT_USER_ID)
        )

    def test_rejects_a_wrong_service_token(self) -> None:
        handler = self._handler({"X-Sites-Service-Token": "wrong"})
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 401)

    def test_a_self_declared_owner_is_refused_for_the_admin_token(self) -> None:
        handler = self._handler(
            {
                "X-Sites-Service-Token": self.SERVICE_TOKEN,
                "X-User-ID": "someone-else",
            }
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)

    def test_a_self_declared_merchant_is_refused_for_the_admin_token(self) -> None:
        # There is no configuration in which this header is honoured any more. The deleted
        # path made "holds the admin token" mean "may be anybody", which under separate
        # operation handed the platform to whoever integrated with it.
        handler = self._handler(
            {
                "X-Sites-Service-Token": self.SERVICE_TOKEN,
                "X-Merchant-ID": "other",
            },
            merchants=[_merchant_row(), _merchant_row("other")],
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)

    def test_the_admin_token_may_not_act_for_a_subject(self) -> None:
        # Acting is a grant carried by a credential (may_act_as_subjects). The admin token
        # is not an API key and has no row to carry one, so it fails closed rather than
        # treating "is admin" as an implicit licence to be anyone.
        handler = self._handler(
            {
                "X-Sites-Service-Token": self.SERVICE_TOKEN,
                "X-Acting-Subject": "0" * 32,
            }
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)

    def test_disabling_local_login_disables_the_admin_token_itself(self) -> None:
        """🔴 Contract §1 decision B: the switch closes the authentication path.

        Not the login form. This drives the credential straight at the API with no console
        involved, which is the only version of the check that could have caught Gitea's
        "the form is hidden but BASIC still works".
        """
        handler = self._handler(
            {"X-Sites-Service-Token": self.SERVICE_TOKEN},
            local_login_enabled=False,
        )
        self.assertFalse(handler._is_admin())
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 401)

    def test_the_pinned_admin_identity_gets_platform_default_quota(self) -> None:
        # It has no tenant row and none is created for it: quota lookup must answer with
        # the platform defaults rather than provisioning a row as a side effect of a read.
        handler = self._handler({"X-Sites-Service-Token": self.SERVICE_TOKEN})
        quota = handler._tenant_quota(DEFAULT_MERCHANT_ID, DEFAULT_USER_ID)
        self.assertEqual(
            quota,
            {
                "max_deployments": DEFAULT_MAX_DEPLOYMENTS,
                "max_public_routes": _exposure.bounded_public_route_default(
                    DEFAULT_MAX_PUBLIC_ROUTES
                ),
                "merchant_max_deployments": 100,
            },
        )
        self.assertEqual(handler.store.created, [])

    def test_deployment_collection_uses_authenticated_owner(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []

            def list_deployments(
                self, merchant_id: str, user_id: str, *, limit: int
            ) -> list[dict]:
                self.calls.append((merchant_id, user_id, limit))
                return [
                    {
                        "merchant_id": merchant_id,
                        "cr_name": "local-web",
                        "service_name": "web",
                        "phase": "Pending",
                    }
                ]

        store = Store()
        handler = self._handler(
            {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        )
        handler.path = "/v1/deployments"
        # Authentication still requires the merchant table, so you have to answer both tables: use a substitute to fill in merchant/tenant.
        store.merchant = _FakeTenantStore({}).merchant
        store.tenant = lambda *_args: None
        handler.store = store
        handler.synchronizer = None
        handler.do_GET()
        self.assertEqual(
            store.calls, [(DEFAULT_MERCHANT_ID, DEFAULT_USER_ID, 100)]
        )
        self.assertEqual(
            self.responses[-1][1]["merchantId"], DEFAULT_MERCHANT_ID
        )
        self.assertEqual(self.responses[-1][0], 200)
        self.assertEqual(self.responses[-1][1]["count"], 1)
        self.assertEqual(
            self.responses[-1][1]["deployments"][0]["serviceName"],
            "web",
        )
        # No synchronizer wired: the snapshot age must be absent rather than
        # fabricated, so a client cannot read "0 seconds old" out of nothing.
        self.assertIsNone(self.responses[-1][1]["snapshotAgeSeconds"])

    def test_deployment_collection_reports_the_snapshot_age(self) -> None:
        class Store:
            def list_deployments(
                self, merchant_id: str, user_id: str, *, limit: int
            ) -> list[dict]:
                return []

        class Synchronizer:
            def snapshot_age_seconds(self) -> float:
                return 74.2

        handler = self._handler(
            {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        )
        handler.path = "/v1/deployments"
        store = Store()
        store.merchant = _FakeTenantStore({}).merchant
        store.tenant = lambda *_args: None
        handler.store = store
        handler.synchronizer = Synchronizer()
        handler.do_GET()
        self.assertEqual(self.responses[-1][1]["snapshotAgeSeconds"], 74.2)


class DatabaseSynchronizerTests(unittest.TestCase):
    class _Store:
        def __init__(self) -> None:
            self.snapshots = 0
            # In the past, this pile could only count and did not have the ability to express "this round is not aligned", so the synchronizer
            # What to do about skipped entries is completely untested. The default is full alignment, and the use case can be changed as needed.
            self.result = SyncSnapshotResult(
                synced=0, skipped=0, soft_deleted=0, reclaimed=True
            )

        def sync_snapshot(self, items: list[dict]) -> SyncSnapshotResult:
            self.snapshots += 1
            return self.result

        def promote_verified_site_versions(self, items: list[dict]) -> int:
            return 0

        def failed_site_version_rollbacks(self, items: list[dict]) -> list[dict]:
            return []

    class _Kube:
        def __init__(self) -> None:
            self.fail = False
            self.patches: list[tuple[str, dict]] = []

        def get(self, path: str) -> dict:
            assert path == COLLECTION_PATH
            if self.fail:
                raise RuntimeError("kubernetes unavailable")
            return {"items": []}

        def patch(self, path: str, body: dict) -> dict:
            self.patches.append((path, body))
            return body

    def _synchronizer(self) -> tuple[DatabaseSynchronizer, _Kube, _Store]:
        kube = self._Kube()
        store = self._Store()
        return (
            DatabaseSynchronizer(kube, store, threading.Lock()),
            kube,
            store,
        )

    def test_age_is_none_until_the_first_sync_succeeds(self) -> None:
        synchronizer, _, _ = self._synchronizer()
        self.assertIsNone(synchronizer.snapshot_age_seconds())

    def test_a_successful_sync_resets_the_age(self) -> None:
        synchronizer, _, store = self._synchronizer()
        synchronizer.sync_once()
        self.assertEqual(store.snapshots, 1)
        age = synchronizer.snapshot_age_seconds()
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)

    def test_kubernetes_snapshot_is_fetched_inside_the_mutation_fence(self) -> None:
        lock = threading.Lock()

        class FencedKube(self._Kube):
            def get(inner_self, path: str) -> dict:
                self.assertTrue(lock.locked())
                return super().get(path)

        synchronizer = DatabaseSynchronizer(FencedKube(), self._Store(), lock)
        synchronizer.sync_once()

    def test_skipping_a_cr_does_not_count_as_a_matched_snapshot(self) -> None:
        """After skipping a bad CR, the snapshot is still drifting and the age cannot be reset to zero.

        sync_snapshot now tolerates faults piece by piece rather than rolling back the entire batch - that's right, but at the cost of "this round
        "Not completely matched" has become a silent state: a database failure will throw a StorageError and be logged by run()
        Down, while drifting away this clock is nowhere to be seen. Unconditional refresh will "skip N items"
        Displayed as "Just synchronized", which offsets the only purpose of snapshotAgeSeconds.
        """
        synchronizer, _, store = self._synchronizer()
        # Forward comparison: First prove that the clock will indeed run when fully aligned.
        synchronizer.sync_once()
        self.assertIsNotNone(synchronizer.snapshot_age_seconds())

        synchronizer, _, store = self._synchronizer()
        store.result = SyncSnapshotResult(
            synced=3, skipped=1, soft_deleted=0, reclaimed=True
        )
        synchronizer.sync_once()
        self.assertEqual(store.snapshots, 1)
        self.assertIsNone(synchronizer.snapshot_age_seconds())

    def test_holding_back_reclamation_does_not_count_either(self) -> None:
        # When there is an entry whose name cannot be read out, the entire sync_snapshot section is not recycled (reclaimed=False).
        # The write was successful, but the soft deletion was not deleted - it is not considered "consistent with the cluster".
        synchronizer, _, store = self._synchronizer()
        store.result = SyncSnapshotResult(
            synced=3, skipped=0, soft_deleted=0, reclaimed=False
        )
        synchronizer.sync_once()
        self.assertIsNone(synchronizer.snapshot_age_seconds())

    def test_a_failing_sync_lets_the_age_keep_growing(self) -> None:
        """A frozen snapshot must not look fresh.

        The sync thread swallows its own exceptions to stay alive, so the age
        is the only signal that the database stopped tracking Kubernetes.
        """
        synchronizer, kube, _ = self._synchronizer()
        synchronizer.sync_once()
        kube.fail = True
        with patch(
            "sites.api.time.monotonic",
            return_value=time.monotonic() + 600,
        ):
            with self.assertRaises(RuntimeError):
                synchronizer.sync_once()
            age = synchronizer.snapshot_age_seconds()
        self.assertGreaterEqual(age, 599)

    def test_static_failure_redeploys_last_verified_artifact(self) -> None:
        synchronizer, kube, store = self._synchronizer()
        digest = "a" * 64
        store.failed_site_version_rollbacks = lambda _items: [
            {
                "cr_name": "site-acme-alice-docs",
                "version": 2,
                "site_type": "static",
                "artifact_uri": (
                    "oss://private-sites/sites/static/acme/alice/docs/"
                    f"{digest}/artifact.json"
                ),
                "content_sha256": digest,
            }
        ]

        synchronizer.sync_once()

        self.assertEqual(len(kube.patches), 1)
        path, body = kube.patches[0]
        self.assertEqual(path, f"{COLLECTION_PATH}/site-acme-alice-docs")
        self.assertEqual(body["spec"]["siteVersion"], 2)
        self.assertEqual(body["spec"]["image"], STATIC_IMAGE)
        self.assertEqual(
            body["spec"]["staticArtifact"],
            {
                "sourcePath": f"acme/alice/docs/{digest}",
                "sha256": digest,
            },
        )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resource = site_deployment_resource(
            {
                "name": "demo-app",
                "image": "example.invalid/demo:v1",
                "port": 8086,
                "healthPath": "/api/agents",
            },
            DEFAULT_MERCHANT_ID,
            "local",
        )
        self.resource["spec"]["revision"] = "revision-3"
        self.resource["status"] = {
            "phase": "Running",
            "message": "Deployment is available",
            "url": "http://127.0.0.1:18090",
        }
        self.resource["spec"]["scaleToZero"] = True
        self.resource["status"]["observedReplicas"] = 0

    def test_site_deployment_maps_to_database_contract(self) -> None:
        values = site_deployment_values(self.resource)
        self.assertEqual(values[0], DEFAULT_MERCHANT_ID)
        self.assertEqual(values[1], "local")
        self.assertEqual(values[2], "demo-app")
        self.assertEqual(
            values[3], cr_name_for(DEFAULT_MERCHANT_ID, "local", "demo-app")
        )
        self.assertEqual(values[7], "revision-3")
        self.assertEqual(values[8], "public")
        self.assertTrue(values[9])
        self.assertEqual(values[10], 0)
        self.assertEqual(values[12], "Running")
        self.assertEqual(values[14], "http://127.0.0.1:18090")

    def test_deletion_timestamp_forces_deleting_phase(self) -> None:
        self.resource["metadata"]["deletionTimestamp"] = "2026-08-08T00:00:00Z"
        values = site_deployment_values(self.resource)
        self.assertEqual(values[12], "Deleting")

    def test_list_deployments_is_bounded_and_returns_records(self) -> None:
        row = (
            DEFAULT_MERCHANT_ID,
            "local",
            "web",
            "local-web",
            "nginx:1.27-alpine",
            8080,
            "/",
            "revision-9",
            "internal",
            False,
            1,
            "Running",
            "Deployment is available",
            "http://127.0.0.1:18090",
            None,
            None,
            None,
            None,
        )

        class Cursor:
            params: tuple[str, int] | None = None
            closed = False

            def execute(self, _sql, params) -> None:
                self.params = params

            def fetchall(self):
                return [row]

            def close(self) -> None:
                self.closed = True

        cursor = Cursor()

        class Connection:
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return cursor

            def commit(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        connection = Connection()

        config = DatabaseConfig("db", 5432, "sites", "user", "password")
        store = Store.postgres(config, connect=lambda **_kwargs: connection)
        records = store.list_deployments(DEFAULT_MERCHANT_ID, "local", limit=25)
        self.assertEqual(cursor.params, (DEFAULT_MERCHANT_ID, "local", 25))
        # The cursor is closed immediately after use; the connection is no longer closed for each operation - PostgreSQL dialect caches by thread
        # Reuse connections (see test_tenancy.PostgresConnectionReuseTests for detection/error deprecation),
        # When the thread exits, it is terminated by threading.local clearing the reference.
        self.assertTrue(cursor.closed)
        self.assertFalse(connection.closed)
        self.assertEqual(records[0]["service_name"], "web")
        self.assertEqual(records[0]["revision"], "revision-9")
        self.assertEqual(records[0]["exposure"], "internal")

    def test_database_tls_never_silently_falls_back_to_plaintext(self) -> None:
        """libpq's negotiating modes are refused, not merely non-default.

        `prefer` connects in plaintext whenever the server declines TLS, so it can
        never produce a failed connection to notice. Leaving it selectable while
        changing the default only moves the same outcome one environment variable
        away. `disable` stays available: it is the same wire, explicitly chosen.
        """
        self.assertEqual(
            "require", DatabaseConfig("db", 5432, "sites", "user", "password").sslmode
        )
        for mode in ("prefer", "allow"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(StorageError, "sslmode"):
                    DatabaseConfig("db", 5432, "sites", "user", "password", sslmode=mode)
        for mode in ("disable", "require", "verify-ca", "verify-full"):
            with self.subTest(mode=mode):
                config = DatabaseConfig(
                    "db", 5432, "sites", "user", "password", sslmode=mode
                )
                self.assertEqual(mode, config.postgres_connect_kwargs()["sslmode"])

    def test_list_deployments_rejects_unbounded_limit(self) -> None:
        config = DatabaseConfig("db", 5432, "sites", "user", "password")
        store = Store.postgres(config, connect=lambda **_kwargs: None)
        for limit in (0, 201):
            with self.assertRaises(ValueError):
                store.list_deployments(DEFAULT_MERCHANT_ID, "local", limit=limit)

    def test_database_config_reads_password_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_path = Path(directory) / "password"
            password_path.write_text("test-password\n")
            with patch.dict(
                os.environ,
                {
                    "SITES_DB_PASSWORD_FILE": str(password_path),
                    "SITES_DB_HOST": "postgres.internal",
                    "SITES_DB_PORT": "5544",
                },
            ):
                config = DatabaseConfig.from_env()
        self.assertEqual(config.host, "postgres.internal")
        self.assertEqual(config.port, 5544)
        self.assertEqual(config.password, "test-password")

    def test_postgres_connect_kwargs_bound_statement_and_socket_time(self) -> None:
        # connect_timeout only covers the handshake. A server that accepts the
        # connection and never answers must be cut off server-side (statement
        # timeout) and, if it silently vanished, by TCP keepalives - otherwise the
        # sync thread parks forever while holding the mutation lock.
        config = DatabaseConfig(
            "db", 5432, "sites", "user", "password", statement_timeout=7
        )
        kwargs = config.postgres_connect_kwargs()
        self.assertEqual(kwargs["options"], "-c statement_timeout=7000")
        self.assertEqual(kwargs["keepalives"], 1)
        self.assertEqual(kwargs["keepalives_idle"], 10)
        self.assertEqual(kwargs["keepalives_interval"], 5)
        self.assertEqual(kwargs["keepalives_count"], 3)
        self.assertEqual(kwargs["connect_timeout"], 5)

    def test_database_config_reads_statement_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_path = Path(directory) / "password"
            password_path.write_text("test-password\n")
            with patch.dict(
                os.environ,
                {
                    "SITES_DB_PASSWORD_FILE": str(password_path),
                    "SITES_DB_STATEMENT_TIMEOUT": "3",
                },
            ):
                config = DatabaseConfig.from_env()
            self.assertEqual(config.statement_timeout, 3)
            with patch.dict(
                os.environ,
                {
                    "SITES_DB_PASSWORD_FILE": str(password_path),
                    "SITES_DB_STATEMENT_TIMEOUT": "0",
                },
            ):
                with self.assertRaises(StorageError):
                    DatabaseConfig.from_env()
        self.assertEqual(
            DatabaseConfig("db", 5432, "sites", "user", "password").statement_timeout,
            10,
        )

    def test_database_config_rejects_empty_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_path = Path(directory) / "password"
            password_path.write_text("")
            with patch.dict(
                os.environ,
                {"SITES_DB_PASSWORD_FILE": str(password_path)},
            ):
                with self.assertRaises(StorageError):
                    DatabaseConfig.from_env()

    def test_postgres_migration_renames_legacy_tables_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = postgres_connection(path)
            try:
                for template in sites_storage._SCHEMA_TEMPLATES:
                    legacy_sql = sites_storage._POSTGRES.render(template)
                    legacy_sql = legacy_sql.replace(
                        "sites_deployments", "appforge_deployments"
                    ).replace("sites_tenants", "appforge_tenants")
                    connection.execute(legacy_sql)
                connection.execute(
                    "INSERT INTO appforge_deployments "
                    "(merchant_id, user_id, service_name, cr_name, image, port, "
                    "health_path, revision, spec, phase, message) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        DEFAULT_MERCHANT_ID,
                        "local",
                        "legacy-site",
                        "local-legacy-site",
                        "example.invalid/site:v1",
                        8080,
                        "/",
                        "1",
                        "{}",
                        "Running",
                        "preserved",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            store = postgres_store(path)
            store.migrate()
            record = store.get_deployment(
                DEFAULT_MERCHANT_ID, "local", "legacy-site"
            )
            self.assertIsNotNone(record)
            self.assertEqual(record["message"], "preserved")
            with postgres_connection(path) as migrated:
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                }
            self.assertIn("sites_deployments", tables)
            self.assertIn("sites_tenants", tables)
            self.assertNotIn("appforge_deployments", tables)
            self.assertNotIn("appforge_tenants", tables)

    def test_database_migration_refuses_ambiguous_old_and_new_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ambiguous.db"
            store = postgres_store(path)
            store.migrate()
            with postgres_connection(path) as connection:
                connection.execute(
                    "CREATE TABLE appforge_deployments "
                    "AS SELECT * FROM sites_deployments"
                )
            with self.assertRaisesRegex(StorageError, "reconcile them"):
                store.migrate()

    def test_database_config_reads_canonical_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_path = Path(directory) / "password"
            password_path.write_text("canonical-password\n")
            with patch.dict(
                os.environ,
                {
                    "SITES_DB_PASSWORD_FILE": str(password_path),
                    "SITES_DB_HOST": "postgres.internal",
                    "SITES_DB_PORT": "5545",
                },
                clear=True,
            ):
                config = DatabaseConfig.from_env()
        self.assertEqual(config.host, "postgres.internal")
        self.assertEqual(config.port, 5545)
        self.assertEqual(config.password, "canonical-password")


class MerchantKeyAuthenticationTests(unittest.TestCase):
    """The path to the merchant API key, and the constraints that come with sharing the same header with the tenant token."""

    SERVICE_TOKEN = "s" * 32
    MERCHANT_KEY = "sitem_acme-key"
    SUBJECT = "0123456789abcdef" * 2

    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _handler(self, headers: dict, **store_kwargs) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = headers
        handler.service_token = self.SERVICE_TOKEN
        handler.session_key = ""
        handler.local_login_enabled = True
        handler.store = _FakeTenantStore(
            store_kwargs.pop("tenants", {}), **store_kwargs
        )
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    def _acme(self, *, may_act: bool = True, **overrides) -> Handler:
        """A key belonging to merchant ``acme``, acting for one subject inside it."""
        headers = {
            "X-Sites-Service-Token": self.MERCHANT_KEY,
            "X-Acting-Subject": self.SUBJECT,
            **overrides.pop("headers", {}),
        }
        merchant_overrides = overrides.pop("merchant_overrides", {})
        return self._handler(
            headers,
            merchants=[
                _merchant_row(),
                _merchant_row(
                    "acme", may_act_as_subjects=may_act, **merchant_overrides
                ),
            ],
            merchant_keys={"acme": self.MERCHANT_KEY},
            **overrides,
        )

    def test_a_merchant_key_acts_for_a_subject_under_that_merchant(self) -> None:
        handler = self._acme(tenants={("acme", "0123456789abcdef0123456789abcdef"): "site_alice"})
        self.assertEqual(handler._authenticate(), ("acme", self.SUBJECT))

    def test_a_forged_merchant_header_cannot_move_the_credential(self) -> None:
        """🔴 Contract §5.5: the merchant the request lands in comes from the key.

        Both halves are asserted, because "refused" alone would still pass if the header
        were honoured on some other path: the request is refused, and a second request
        without the header lands on the key's own merchant rather than the named one.
        """
        handler = self._handler(
            {
                "X-Sites-Service-Token": self.MERCHANT_KEY,
                "X-Acting-Subject": self.SUBJECT,
                "X-Merchant-ID": "other",
            },
            tenants={("acme", "0123456789abcdef0123456789abcdef"): "site_alice"},
            merchants=[
                _merchant_row(),
                _merchant_row("acme", may_act_as_subjects=True),
                _merchant_row("other", may_act_as_subjects=True),
            ],
            merchant_keys={"acme": self.MERCHANT_KEY},
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)

        honest = self._acme(tenants={("acme", "0123456789abcdef0123456789abcdef"): "site_alice"})
        self.assertEqual(honest._authenticate(), ("acme", self.SUBJECT))

    def test_a_key_without_the_grant_is_refused_not_ignored(self) -> None:
        """🔴 Contract §5.4: fail closed, and loudly.

        Ignoring the header would silently demote the call to the key's own identity, so
        the caller would file one user's resources under another and read 2xx while doing
        it. This is Kubernetes impersonation semantics: may-I-act-for-others is a property
        of the caller's own credential.
        """
        handler = self._acme(may_act=False)
        self.assertIsNone(handler._authenticate())
        status, payload = self.responses[-1]
        self.assertEqual(status, 403)
        self.assertIn("not authorized to act", payload["error"])
        self.assertEqual(handler.store.created, [])

    def test_a_key_with_the_grant_must_name_the_subject(self) -> None:
        # Not guessing a default tenant: a caller that forgets the header would otherwise
        # build resources somewhere else without a word.
        handler = self._acme(headers={"X-Acting-Subject": ""})
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 400)

    def test_a_key_without_the_grant_acts_as_its_own_default_tenant(self) -> None:
        handler = self._acme(may_act=False, headers={"X-Acting-Subject": ""})
        self.assertEqual(handler._authenticate(), ("acme", DEFAULT_USER_ID))

    def test_a_malformed_subject_is_refused(self) -> None:
        # The accepted shape is exactly the derivation's output. Anything else is either a
        # caller that computed it wrong or one trying a raw account name.
        for bad in ("alice", self.SUBJECT.upper(), self.SUBJECT[:31], self.SUBJECT + "a"):
            with self.subTest(subject=bad):
                self.responses.clear()
                handler = self._acme(headers={"X-Acting-Subject": bad})
                self.assertIsNone(handler._authenticate())
                self.assertEqual(self.responses[-1][0], 400)
                self.assertEqual(handler.store.created, [])

    def test_an_expired_key_is_refused_like_an_unknown_one(self) -> None:
        handler = self._acme(
            merchant_overrides={
                "key_expires_at": dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(seconds=1)
            }
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 401)
        self.assertEqual(self.responses[-1][1]["error"], "invalid service token")

    def test_a_key_inside_its_lifetime_still_works(self) -> None:
        # Forward comparison for the case above: without it, a clock comparison inverted
        # the wrong way would refuse every key and still show green.
        handler = self._acme(
            merchant_overrides={
                "key_expires_at": dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(days=1)
            }
        )
        self.assertEqual(handler._authenticate(), ("acme", self.SUBJECT))

    def test_every_credential_miss_answers_identically(self) -> None:
        # admin does not match / merchant table cannot be found / tenant table cannot be found, the three failures must be the same byte
        # Sequence - A single word difference will turn this endpoint into a detector for "does this merchant/tenant exist?"
        answers = []
        for token in ("wrong-admin", "sitem_nope", "site_nope"):
            self.responses.clear()
            handler = self._handler(
                {"X-Sites-Service-Token": token},
                merchants=[_merchant_row(), _merchant_row("acme")],
                merchant_keys={"acme": self.MERCHANT_KEY},
                tenants={("acme", "0123456789abcdef0123456789abcdef"): "site_alice"},
            )
            self.assertIsNone(handler._authenticate())
            answers.append(self.responses[-1])
        self.assertEqual(len(set(map(repr, answers))), 1, answers)
        self.assertEqual(answers[0][0], 401)

    def test_the_lookup_never_branches_on_the_credential_prefix(self) -> None:
        # The prefix is only used for categorization when the log is read by humans. Relying on it to divert traffic is equivalent to revealing "which category this certificate belongs to", and there are two
        # A failure response on the path will almost certainly be followed by a bifurcation - so both tables have to be checked.
        odd_merchant_key = "site_looks-like-a-tenant-token"
        handler = self._handler(
            {
                "X-Sites-Service-Token": odd_merchant_key,
                "X-Acting-Subject": self.SUBJECT,
            },
            merchants=[
                _merchant_row(),
                _merchant_row("acme", may_act_as_subjects=True),
            ],
            merchant_keys={"acme": odd_merchant_key},
        )
        self.assertEqual(handler._authenticate(), ("acme", self.SUBJECT))

        odd_tenant_token = "sitem_looks-like-a-merchant-key"
        handler = self._handler(
            {"X-Sites-Service-Token": odd_tenant_token},
            merchants=[_merchant_row(), _merchant_row("acme")],
            merchant_keys={"acme": self.MERCHANT_KEY},
            tenants={("acme", "alice"): odd_tenant_token},
        )
        self.assertEqual(handler._authenticate(), ("acme", "alice"))

    def test_an_unknown_subject_is_registered_just_in_time(self) -> None:
        # Tenants are created on first use; merchants never are (contract §4).
        handler = self._acme()
        self.assertEqual(handler._authenticate(), ("acme", self.SUBJECT))
        self.assertEqual(handler.store.created, [("acme", self.SUBJECT)])

    def test_the_merchant_tenant_ceiling_blocks_the_registration(self) -> None:
        handler = self._handler(
            {
                "X-Sites-Service-Token": self.MERCHANT_KEY,
                "X-Acting-Subject": self.SUBJECT,
            },
            merchants=[
                _merchant_row(),
                _merchant_row("acme", max_tenants=1, may_act_as_subjects=True),
            ],
            merchant_keys={"acme": self.MERCHANT_KEY},
            tenants={("acme", "bob"): "site_bob"},
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 429)
        self.assertEqual(
            self.responses[-1][1]["code"], "merchant_tenant_quota_exceeded"
        )
        self.assertEqual(handler.store.created, [])

    def test_a_disabled_tenant_is_refused_through_the_merchant_key(self) -> None:
        # To deactivate the tenant, you must also block the merchant key path: if it only blocks its own token, deactivate the
        # The caller holding the merchant key does not happen.
        handler = self._acme(tenants={("acme", "0123456789abcdef0123456789abcdef"): "site_alice"})
        handler.store.tenant("acme", "0123456789abcdef0123456789abcdef")["disabled_at"] = "2026-08-14"
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)
        self.assertIn("disabled", self.responses[-1][1]["error"])

    def test_disabling_a_merchant_closes_both_credential_paths(self) -> None:
        # Only the merchant key is revoked and the tokens of tenants under its name can still be used = deactivation has no effect, and it is most common.
        # It didn't work on the path I took.
        merchants = [
            _merchant_row(),
            _merchant_row("acme", disabled_at="2026-08-14", may_act_as_subjects=True),
        ]
        key_handler = self._handler(
            {
                "X-Sites-Service-Token": self.MERCHANT_KEY,
                "X-Acting-Subject": self.SUBJECT,
            },
            merchants=merchants,
            merchant_keys={"acme": self.MERCHANT_KEY},
            tenants={("acme", "alice"): "site_alice"},
        )
        self.assertIsNone(key_handler._authenticate())
        # The merchant key is "cannot be found", which is the same 401 as the token error - no additional disclosure.
        # "This merchant has been disabled".
        self.assertEqual(self.responses[-1][0], 401)

        self.responses.clear()
        token_handler = self._handler(
            {"X-Sites-Service-Token": "site_alice"},
            merchants=merchants,
            merchant_keys={"acme": self.MERCHANT_KEY},
            tenants={("acme", "alice"): "site_alice"},
        )
        self.assertIsNone(token_handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)
        self.assertIn("merchant", self.responses[-1][1]["error"])

    def test_a_tenant_token_may_not_act_for_a_subject(self) -> None:
        handler = self._handler(
            {
                "X-Sites-Service-Token": "site_alice",
                "X-Acting-Subject": self.SUBJECT,
            },
            merchants=[
                _merchant_row(),
                _merchant_row("acme", may_act_as_subjects=True),
            ],
            merchant_keys={"acme": self.MERCHANT_KEY},
            tenants={("acme", "alice"): "site_alice"},
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(self.responses[-1][0], 403)


class AdminAggregationGuardTests(unittest.TestCase):
    """list_all_deployments is the only query without tenant filtering, and this is where the unauthorized access occurs."""

    SERVICE_TOKEN = "s" * 32

    class _Store(_FakeTenantStore):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.all_calls: list[dict] = []

        def list_all_deployments(self, **kwargs):
            self.all_calls.append(kwargs)
            return []

        def list_deployments(self, merchant_id, user_id, *, limit):
            return []

    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _handler(self, token: str) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": token}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.store = self._Store({"alice": "site_alice"})
        handler.synchronizer = None
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    def test_a_deployment_response_carries_both_identity_halves(self) -> None:
        """merchantId and userId must appear in pairs.

        The user_id is only unique within the merchant, and merchantId alone cannot locate a specific row. Management end overview
        Without the userId, the administrator sees a bunch of sites but doesn’t know who they belong to - and "two merchants have the same name"
        "Tenants" is exactly the scenario this set of renovations deals with, precisely when distinction is most needed.

        This was captured from the browser: The deployment card of the console was rendered as "acme /", after the slash
        Empty. The upstream e2e only asserts "with merchantId", so the other half is missed.
        """
        response = _deployment_record_response(
            {
                "cr_name": "acme-alice-probe-3ab5a79afba5",
                "merchant_id": "acme",
                "user_id": "alice",
                "service_name": "probe",
                "image": "example.invalid/nginx:1",
                "port": 8080,
                "health_path": "/",
                "revision": "1",
                "phase": "Running",
                "message": "",
                "url": "http://127.0.0.1:18090",
            }
        )
        self.assertEqual(response["merchantId"], "acme")
        self.assertEqual(response["userId"], "alice")

    def test_a_tenant_token_cannot_reach_the_cross_tenant_aggregate(self) -> None:
        handler = self._handler("site_alice")
        handler.path = "/v1/admin/deployments"
        handler.do_GET()
        self.assertEqual(self.responses[-1][0], 403)
        # Rejection should occur before querying, rather than "filtering after querying".
        self.assertEqual(handler.store.all_calls, [])

    def test_the_tenant_collection_never_uses_the_aggregate(self) -> None:
        handler = self._handler("site_alice")
        handler.path = "/v1/deployments"
        handler.do_GET()
        self.assertEqual(self.responses[-1][0], 200)
        self.assertEqual(handler.store.all_calls, [])

    def test_admin_filters_reach_the_query(self) -> None:
        handler = self._handler(self.SERVICE_TOKEN)
        handler.path = "/v1/admin/deployments?merchantId=acme&phase=Running&limit=7"
        handler.do_GET()
        self.assertEqual(self.responses[-1][0], 200)
        self.assertEqual(
            handler.store.all_calls,
            [{"merchant_id": "acme", "phase": "Running", "limit": 7}],
        )

    def test_an_oversized_limit_is_refused_before_the_query(self) -> None:
        handler = self._handler(self.SERVICE_TOKEN)
        handler.path = "/v1/admin/deployments?limit=1000"
        handler.do_GET()
        self.assertEqual(self.responses[-1][0], 400)
        self.assertEqual(handler.store.all_calls, [])


class AdminHealthTests(unittest.TestCase):
    """/v1/admin/health. The administrator came to this page exactly when something went wrong."""

    SERVICE_TOKEN = "s" * 32
    OPERATOR_PATH = (
        "/apis/apps/v1/namespaces/sites-local/deployments/sites-operator"
    )

    class _Kube:
        def __init__(self, ready: int | None = 1, checked_at: str | None = None):
            self.ready = ready
            self.checked_at = checked_at

        def get(self, path: str) -> dict:
            if path.endswith("/deployments/sites-operator"):
                if self.ready is None:
                    raise ApiError(403, "forbidden")
                return {"status": {"readyReplicas": self.ready}}
            if path == "/version":
                return {"gitVersion": "v1.36.1"}
            status = {"checkedAt": self.checked_at} if self.checked_at else {}
            return {"items": [{"metadata": {"name": "x"}, "status": status}]}

    def _handler(self, kube, *, store=None):
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.kube = kube
        handler.store = store if store is not None else _FakeReadyStore("postgresql")
        handler.synchronizer = None
        handler.path = "/v1/admin/health"
        self.responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    def test_operator_liveness_comes_from_its_deployment(self) -> None:
        # If we extrapolate from the CR timestamp, the meaning of reachable will become "I can read a certain signal"——
        # When there are no CRs in the cluster, it is indistinguishable from "everything is fine" on the page.
        handler = self._handler(self._Kube(ready=1))
        self.assertEqual(handler._probe_operator()["reachable"], True)
        handler = self._handler(self._Kube(ready=0))
        probe = handler._probe_operator()
        self.assertFalse(probe["reachable"])
        self.assertIn("0 ready replica", probe["error"])
        handler = self._handler(self._Kube(ready=None))
        self.assertFalse(handler._probe_operator()["reachable"])

    def test_no_reconciled_resource_reports_null_not_zero(self) -> None:
        # null = "no observable convergence"; 0 will be read as "just converged".
        handler = self._handler(self._Kube(ready=1))
        self.assertIsNone(handler._probe_operator()["lastReconcileSeconds"])
        handler = self._handler(
            self._Kube(ready=1, checked_at="2026-01-01T00:00:00+00:00")
        )
        self.assertGreater(handler._probe_operator()["lastReconcileSeconds"], 0)

    def test_a_failing_probe_never_takes_the_whole_page_down(self) -> None:
        class _BrokenStore:
            backend = "postgresql"

            def ping(self) -> None:
                raise StorageError("database health check failed")

        handler = self._handler(self._Kube(ready=None), store=_BrokenStore())
        with patch(
            "sites.api_admin.registry_get",
            side_effect=RuntimeError("local registry is unavailable"),
        ):
            handler.do_GET()
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload["database"]["reachable"])
        self.assertFalse(payload["operator"]["reachable"])
        self.assertFalse(payload["registry"]["reachable"])
        self.assertTrue(payload["kubernetes"]["reachable"])
        self.assertEqual(payload["kubernetes"]["version"], "v1.36.1")

    def test_health_is_admin_only(self) -> None:
        handler = self._handler(self._Kube(ready=1))
        handler.headers = {"X-Sites-Service-Token": "site_alice"}
        handler.do_GET()
        self.assertEqual(self.responses[-1][0], 403)


class PatchEndpointTests(unittest.TestCase):
    """PATCH both management endpoints. Quotas are variable, but changing quotas and changing vouchers must be two actions."""

    SERVICE_TOKEN = "s" * 32

    class _Store(_FakeTenantStore):
        """Stand-in with partial update write. Empty update throws ValueError, which is isomorphic to the real Store."""

        def __init__(self, *args, fail_after: int = 99, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.writes: list[tuple] = []
            self.fail_after = fail_after
            self.fail_reads = False

        def update_tenant_quota(
            self,
            merchant_id,
            user_id,
            *,
            max_deployments=None,
            max_public_routes=None,
        ) -> None:
            if max_deployments is None and max_public_routes is None:
                raise ValueError("nothing to update")
            self._write(
                ("tenant", merchant_id, user_id, max_deployments, max_public_routes)
            )

        def update_merchant(
            self,
            merchant_id,
            *,
            display_name=None,
            max_tenants=None,
            max_deployments=None,
            may_act_as_subjects=None,
        ) -> None:
            if (
                display_name is None
                and max_tenants is None
                and max_deployments is None
                and may_act_as_subjects is None
            ):
                raise ValueError("nothing to update")
            self._write(
                (
                    "merchant",
                    merchant_id,
                    max_tenants,
                    max_deployments,
                    display_name,
                    may_act_as_subjects,
                )
            )

        def merchant(self, merchant_id):
            # Fail only after there has been a write: simulate the split surface of "write success, readback failure",
            # The preceding existence check must still succeed.
            if self.fail_reads and self.writes:
                raise StorageError("merchant read failed")
            return super().merchant(merchant_id)

        def _write(self, entry: tuple) -> None:
            """Writing after ``fail_after`` throws an error, which is used to create a "half-changed" state."""
            if len(self.writes) >= self.fail_after:
                raise StorageError("merchant update failed")
            self.writes.append(entry)

    def setUp(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _handler(self, path: str, body: dict, **store_kwargs) -> Handler:
        handler = object.__new__(Handler)
        handler.headers = {"X-Sites-Service-Token": self.SERVICE_TOKEN}
        handler.service_token = self.SERVICE_TOKEN
        handler.proxy_token = ""
        handler.path = path
        handler.store = self._Store(
            store_kwargs.pop("tenants", {"alice": "site_alice"}), **store_kwargs
        )
        handler._read_body = lambda *_a, **_k: body
        handler._json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        return handler

    def test_a_tenant_patch_needs_the_merchant_to_locate_a_row(self) -> None:
        # user_id is only unique within the merchant. If merchantId is missing, it will be 400. Do not guess a default merchant——
        # The wrong guess will be changed to a tenant with the same name under another merchant's name, and "success" will be displayed on both sides.
        handler = self._handler("/v1/tenants/alice", {"maxDeployments": 5})
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 400)
        self.assertEqual(handler.store.writes, [])

    def test_a_tenant_patch_forwards_only_the_submitted_fields(self) -> None:
        handler = self._handler(
            f"/v1/tenants/alice?merchantId={DEFAULT_MERCHANT_ID}",
            {"maxDeployments": 5},
        )
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 200)
        # The uncommitted item is passed None (= not moved), rather than being silently overwritten by the default value.
        self.assertEqual(
            handler.store.writes,
            [("tenant", DEFAULT_MERCHANT_ID, "alice", 5, None)],
        )

    def test_an_empty_tenant_patch_is_refused(self) -> None:
        handler = self._handler(
            f"/v1/tenants/alice?merchantId={DEFAULT_MERCHANT_ID}", {}
        )
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 400)
        self.assertEqual(handler.store.writes, [])

    @_POOL_ONLY
    def test_a_tenant_patch_refuses_a_quota_larger_than_the_pool(self) -> None:
        handler = self._handler(
            f"/v1/tenants/alice?merchantId={DEFAULT_MERCHANT_ID}",
            {"maxPublicRoutes": len(NODE_PORT_RANGE) + 1},
        )
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 400)
        self.assertEqual(handler.store.writes, [])

    def test_a_merchant_patch_writes_quota_and_name_as_one_update(self) -> None:
        # The data layer used to have two statements and two transactions for quota/name change. Failure in the middle left partial updates, and the order was still scored.
        # "Which partial update is harmless". The storage side has been merged into a single UPDATE (single statement natural atom),
        # The ordering problem disappears - "carry both in one call" is nailed here.
        handler = self._handler(
            "/v1/merchants/acme",
            {"displayName": "Acme Inc", "maxTenants": 7},
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 200)
        self.assertEqual(
            handler.store.writes,
            [("merchant", "acme", 7, None, "Acme Inc", None)],
        )

    def test_a_quota_only_merchant_patch_leaves_the_name_alone(self) -> None:
        handler = self._handler(
            "/v1/merchants/acme",
            {"maxDeployments": 9},
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 200)
        # Fields that have not been submitted are passed None (= do not move) and will not be silently overwritten by the default value.
        self.assertEqual(
            handler.store.writes, [("merchant", "acme", None, 9, None, None)]
        )

    def test_a_half_applied_merchant_patch_does_not_report_no_change(self) -> None:
        # After a single UPDATE, there is no longer "half-changed" within the merchant row; the remaining split points are
        # "The write succeeded but the readback failed." At this time, the report "database unavailable" will be read as "nothing has been changed".
        # The administrator performs other operations based on that conclusion, and Curry already has a new value.
        handler = self._handler(
            "/v1/merchants/acme",
            {"displayName": "Acme Inc", "maxTenants": 7},
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        handler.store.fail_reads = True
        handler.do_PATCH()
        status, payload = self.responses[-1]
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "partial_update")
        self.assertEqual(payload["applied"], ["maxTenants", "displayName"])
        self.assertEqual(
            handler.store.writes, [("merchant", "acme", 7, None, "Acme Inc", None)]
        )

    def test_a_wholly_failed_merchant_patch_still_reads_as_no_change(self) -> None:
        handler = self._handler(
            "/v1/merchants/acme",
            {"displayName": "Acme Inc"},
            merchants=[_merchant_row(), _merchant_row("acme")],
            fail_after=0,
        )
        handler.do_PATCH()
        status, payload = self.responses[-1]
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "database unavailable")
        self.assertEqual(handler.store.writes, [])

    def test_an_empty_merchant_patch_is_refused_before_the_write(self) -> None:
        handler = self._handler(
            "/v1/merchants/acme",
            {},
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 400)
        self.assertEqual(handler.store.writes, [])

    def test_a_merchant_patch_bounds_the_display_name(self) -> None:
        # displayName is external controllable text, which will be entered into the log and rendered by the console.
        for value in ("", "x" * 65, "line\nbreak"):
            handler = self._handler(
                "/v1/merchants/acme",
                {"displayName": value},
                merchants=[_merchant_row(), _merchant_row("acme")],
            )
            handler.do_PATCH()
            self.assertEqual(self.responses[-1][0], 400, repr(value))
            self.assertEqual(handler.store.writes, [])

    def test_an_unknown_merchant_is_a_404_before_any_write(self) -> None:
        handler = self._handler("/v1/merchants/nobody", {"maxTenants": 3})
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 404)
        self.assertEqual(handler.store.writes, [])

    def test_patch_is_admin_only(self) -> None:
        handler = self._handler(
            f"/v1/tenants/alice?merchantId={DEFAULT_MERCHANT_ID}",
            {"maxDeployments": 5},
        )
        handler.headers = {"X-Sites-Service-Token": "site_alice"}
        handler.do_PATCH()
        self.assertEqual(self.responses[-1][0], 403)
        self.assertEqual(handler.store.writes, [])


class ConsoleAssetTests(unittest.TestCase):
    """Static service for /console/. This is the only place where the files on the disk are returned to the outside according to the requested path."""

    def _handler(self) -> tuple[Handler, dict]:
        handler = object.__new__(Handler)
        captured: dict = {"status": None, "headers": {}, "json": None}
        handler.send_response = lambda status: captured.__setitem__(
            "status", status
        )
        handler.send_header = lambda key, value: captured["headers"].__setitem__(
            key, value
        )
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler._json = lambda status, payload: captured.update(
            status=status, json=payload
        )
        return handler, captured

    def _bundle(self) -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "console"
        (root / "assets").mkdir(parents=True)
        (root / "index.html").write_text("<!doctype html><title>console</title>")
        (root / "assets" / "app.js").write_text("export const x = 1;\n")
        (root / "assets" / "notes.md").write_text("# not a web type\n")
        return str(root)

    def test_a_missing_bundle_is_a_503_that_names_the_real_cause(self) -> None:
        # 404 will be read as "Route not configured", and the real reason is that the product is not imported into the image - the two troubleshooting directions
        # Quite the opposite.
        handler, captured = self._handler()
        with patch("sites.http_kit.CONSOLE_ROOT", "/nonexistent/console"):
            handler._serve_console("/console/")
        self.assertEqual(captured["status"], 503)
        self.assertEqual(
            captured["json"]["error"],
            "console assets are not bundled in this image",
        )

    def test_the_index_carries_the_hardening_headers_and_no_cors(self) -> None:
        handler, captured = self._handler()
        with patch("sites.http_kit.CONSOLE_ROOT", self._bundle()):
            handler._serve_console("/console/")
        self.assertEqual(captured["status"], 200)
        self.assertEqual(
            captured["headers"]["Content-Type"], "text/html; charset=utf-8"
        )
        self.assertEqual(captured["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertEqual(captured["headers"]["Cache-Control"], "no-store")
        self.assertEqual(captured["headers"]["Referrer-Policy"], "no-referrer")
        self.assertEqual(captured["headers"]["Content-Security-Policy"], CONSOLE_CSP)
        self.assertIn(b"<title>console</title>", handler.wfile.getvalue())
        # The console has the same origin as /v1/*. Posting a CORS header will only expand the exposure from "same source" to "a certain source".
        self.assertFalse(
            [key for key in captured["headers"] if key.startswith("Access-Control")]
        )

    def test_an_unmatched_path_falls_back_to_the_spa_entrypoint(self) -> None:
        handler, captured = self._handler()
        with patch("sites.http_kit.CONSOLE_ROOT", self._bundle()):
            handler._serve_console("/console/merchants/acme")
        self.assertEqual(captured["status"], 200)
        self.assertIn(b"<title>console</title>", handler.wfile.getvalue())

    def test_an_unknown_extension_is_never_given_a_renderable_type(self) -> None:
        handler, captured = self._handler()
        with patch("sites.http_kit.CONSOLE_ROOT", self._bundle()):
            handler._serve_console("/console/assets/notes.md")
        self.assertEqual(captured["status"], 200)
        self.assertEqual(
            captured["headers"]["Content-Type"], "application/octet-stream"
        )

    def test_hashed_assets_are_immutable_but_html_is_not_cached(self) -> None:
        handler, captured = self._handler()
        with patch("sites.http_kit.CONSOLE_ROOT", self._bundle()):
            handler._serve_console("/console/assets/app.js")
        self.assertEqual(captured["status"], 200)
        self.assertEqual(
            captured["headers"]["Cache-Control"],
            "public, max-age=31536000, immutable",
        )

    def test_json_responses_are_not_stored_by_shared_caches(self) -> None:
        handler, captured = self._handler()
        handler._json = Handler._json.__get__(handler)
        handler._json(200, {"deployments": ["sensitive"]})
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["headers"]["Cache-Control"], "no-store")
        self.assertEqual(captured["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertEqual(captured["headers"]["Referrer-Policy"], "no-referrer")

    def test_unexpected_handler_exceptions_return_an_opaque_500(self) -> None:
        handler, captured = self._handler()
        with patch.object(
            BaseHTTPRequestHandler,
            "handle_one_request",
            side_effect=RuntimeError("internal detail"),
        ):
            handler.handle_one_request()
        self.assertEqual(captured["status"], 500)
        self.assertEqual(captured["json"], {"error": "internal server error"})

    def test_traversal_is_refused_rather_than_falling_back(self) -> None:
        # The traversal request must be 403 instead of "return to index.html if not found": the latter will cause an explicit
        # Override attempts to turn into a normal looking 200.
        root = self._bundle()
        outside = Path(root).parent / "secret.txt"
        outside.write_text("do not serve me")
        for path in (
            "/console/../secret.txt",
            "/console/%2e%2e/secret.txt",
            "/console/assets/../../secret.txt",
            "//etc/passwd",
        ):
            handler, captured = self._handler()
            with patch("sites.http_kit.CONSOLE_ROOT", root):
                handler._serve_console(
                    path if path.startswith("/console/") else f"/console/{path}"
                )
            self.assertEqual(captured["status"], 403, path)
            self.assertNotIn(b"do not serve me", handler.wfile.getvalue())

    def test_a_symlink_pointing_outside_the_root_is_refused(self) -> None:
        # Only blocking `..` at the string level cannot block soft links, so the assertion must occur on realpath
        # After that.
        root = self._bundle()
        outside = Path(root).parent / "secret.txt"
        outside.write_text("do not serve me")
        os.symlink(outside, Path(root) / "escape.txt")
        handler, captured = self._handler()
        with patch("sites.http_kit.CONSOLE_ROOT", root):
            handler._serve_console("/console/escape.txt")
        self.assertEqual(captured["status"], 403)

    def test_the_bare_console_path_redirects_to_the_directory(self) -> None:
        # If there is one missing slash, 404 is a complete pitfall: all resources in the product are relative paths.
        handler, captured = self._handler()
        handler.path = "/console"
        handler.do_GET()
        self.assertEqual(captured["status"], 301)
        self.assertEqual(captured["headers"]["Location"], "/console/")


if __name__ == "__main__":
    unittest.main()


class RuntimePackageLayoutTests(unittest.TestCase):
    """Keep runtime modules inside the physical ``sites`` package.

    The former repository-root mapping made the installed package contain every test module
    and duplicated ``safe_stdout`` as both a top-level and package module. Tests run from
    the checkout could not expose that wheel defect.
    """

    ROOT = Path(__file__).resolve().parent.parent
    PACKAGE = ROOT / "src" / "sites"

    def test_runtime_modules_and_safe_stdout_are_packaged_together(self) -> None:
        dockerfile = (self.ROOT / "Dockerfile").read_text(encoding="utf-8")
        runtime_modules = sorted(path.name for path in self.PACKAGE.glob("*.py"))
        self.assertIn(
            "safe_stdout.py",
            runtime_modules,
            "safe_stdout is a sites runtime module, not a second top-level package",
        )
        self.assertIn("COPY src/sites/ /app/sites/", dockerfile)
        self.assertIn("packages.find", (self.ROOT / "pyproject.toml").read_text())


class RouteTemplateContractTests(unittest.TestCase):
    """Each route literal in the do_* distribution chain must be claimed by _route_template.

    The routing table (static list in _ROUTE_TEMPLATES + _route_template) misses a true
    When routing, the metric does not report errors and looks healthy, but all the traffic of that route falls into "other" from now on.
    The bucket is not visible in sites_api_requests_total - /v1/admin/* Four items are silent like this
    Drifted away. Use AST to exhaustively enumerate distribution literals in source code instead of manually maintaining a "known route"
    List: The list itself will also leak, but the source code will not.
    """

    def _dispatched_literals(self) -> set[str]:
        source = Path(__file__).resolve().parent.parent / "src" / "sites" / "api.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        literals: set[str] = set()
        for node in ast.walk(tree):
            # Only look at the do_* distribution methods of Handler; the path in the helper does not belong to the distribution chain.
            if not (
                isinstance(node, ast.FunctionDef)
                and node.name.startswith("do_")
            ):
                continue
            for compare in ast.walk(node):
                if not isinstance(compare, ast.Compare):
                    continue
                # Only accept `path == "/v1/..."` / `path != "/v1/..."` Form - Distribution
                # This is what is used for determination; string comparisons of other variables have nothing to do with routing.
                if not (
                    len(compare.ops) == 1
                    and isinstance(compare.ops[0], (ast.Eq, ast.NotEq))
                    and isinstance(compare.left, ast.Name)
                    and compare.left.id == "path"
                    and len(compare.comparators) == 1
                    and isinstance(compare.comparators[0], ast.Constant)
                    and isinstance(compare.comparators[0].value, str)
                    and compare.comparators[0].value.startswith("/")
                ):
                    continue
                literals.add(compare.comparators[0].value)
        # Keep the scan itself: If the matching condition is written incorrectly, it will be "not a single literal was scanned".
        # The form fails, instead of being silent all green.
        self.assertTrue(literals, "AST did not scan any distribution literals, the scanner is broken.")
        return literals

    def test_every_dispatched_route_lands_in_a_named_template(self) -> None:
        from sites.api import _route_template

        unmatched = sorted(
            literal
            for literal in self._dispatched_literals()
            if _route_template(literal) == "other"
        )
        self.assertEqual(
            unmatched,
            [],
            "These routes distributed by do_* are not claimed by _route_template, and their metrics will"
            "All fall into the other bucket",
        )


class ErrorResponseTableTests(unittest.TestCase):
    """Exception → Invariant of the response map itself.

    After the three except ladders converged into two tables, the consistency between the table sequence and the two tables became a new manual alignment.
    Point: The general table has changed status/code but the build table has not kept up. /v1/builds will continue to use the old semantics.
    There is no test red - until the caller discovers that the two endpoints report different things for the same rejection.
    """

    def test_build_table_agrees_with_the_shared_table(self) -> None:
        shared = {
            exc_type: (status, code, fixed)
            for exc_type, status, code, fixed in _MUTATION_ERROR_RESPONSES
            if exc_type is not StorageError
        }
        build = {
            exc_type: (status, code, fixed)
            for exc_type, status, code, fixed in _BUILD_ERROR_RESPONSES
        }
        for exc_type, response in shared.items():
            self.assertIn(
                exc_type,
                build,
                f"{exc_type.__name__} in the general table but not in the /v1/builds table,"
                "The two watches are gone",
            )
            self.assertEqual(
                build[exc_type],
                response,
                f"{exc_type.__name__} The responses in the two tables are inconsistent,"
                "/v1/builds and other endpoints will report different things for the same rejection",
            )

    def test_no_entry_is_shadowed_by_an_ancestor(self) -> None:
        # Table order is semantics: if a subclass is ranked after one of its ancestors, issubclass/isinstance will come first
        # Hit the ancestor box and specifically refuse to be written as an ancestor response (almost always 502).
        for table_name, table in (
            ("_MUTATION_ERROR_RESPONSES", _MUTATION_ERROR_RESPONSES),
            ("_BUILD_ERROR_RESPONSES", _BUILD_ERROR_RESPONSES),
        ):
            for later_index, (later_type, *_later) in enumerate(table):
                for earlier_type, *_earlier in table[:later_index]:
                    shadowed = (
                        issubclass(later_type, earlier_type)
                        and later_type is not earlier_type
                    )
                    self.assertFalse(
                        shadowed,
                        f"{table_name}: {later_type.__name__} Yes "
                        f"{earlier_type.__name__} The subcategories of are ranked behind it, "
                        "You will never get your own turn",
                    )


class StartupWiringTests(unittest.TestCase):
    """serve() is the process entry point; the two pieces split out of it are tested here."""

    def setUp(self) -> None:
        self._previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }

    def tearDown(self) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)

    def test_initial_sync_tolerates_an_unreachable_apiserver(self) -> None:
        """A restart during an apiserver outage must not CrashLoop.

        The run() loop retries every interval anyway; the first round escaping
        serve() only added a requirement that Kubernetes be healthy at the one
        instant the Pod starts.
        """
        from sites.api import KUBERNETES_HEALTH, _initial_sync

        kube = DatabaseSynchronizerTests._Kube()
        kube.fail = True
        synchronizer = DatabaseSynchronizer(
            kube, DatabaseSynchronizerTests._Store(), threading.Lock()
        )
        try:
            _initial_sync(synchronizer)
            # The failure still has to reach the health signal: swallowing it must
            # not also hide it from /v1/admin/health.
            health = KUBERNETES_HEALTH.snapshot()
            self.assertTrue(health["observed"])
            self.assertFalse(health["ok"])
        finally:
            KUBERNETES_HEALTH.record_success()

    def test_listen_stops_on_sigterm_and_joins_request_threads(self) -> None:
        from sites.api import _listen

        server = _listen("127.0.0.1", 0)
        # server_close() only joins request threads that are not daemons; with the
        # default (daemon) threads a SIGTERM would exit mid-response.
        self.assertFalse(server.daemon_threads)
        returned = threading.Event()

        def run() -> None:
            server.serve_forever(poll_interval=0.05)
            returned.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(
                returned.wait(3.0), "serve_forever must return after SIGTERM"
            )
        finally:
            if not returned.is_set():
                server.shutdown()
            server.server_close()
