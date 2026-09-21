"""Hull Broker Kubernetes wiring: per-SiteDeployment identity, env injection, and
NetworkPolicy egress (CONTRACT.md section 6, partition B deliverables B1-B3).

``SITES_HULL_BROKER_ENABLED`` gates all of it. The central claim these tests pin is
that leaving it unset reproduces today's ``deployment_resource``/``network_policy_
resources`` output exactly -- see ``HullBrokerDisabledIsByteIdenticalTests``.
"""
from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

from sites.k8s_resources import (
    HULL_TOKEN_FILE,
    HULL_TOKEN_MIN_EXPIRATION_SECONDS,
    deployment_resource,
    hull_service_account_resource,
    hull_token_expiration_seconds,
    network_policy_resources,
)
from sites.naming import cr_name_for
from sites.validation import ValidationError, normalize_deploy_payload
from sites import exposure


DEFAULT_MERCHANT_ID = "local"
DEFAULT_USER_ID = "local"
HEX_USER_ID = "0123456789abcdef0123456789abcdef"


@contextmanager
def hull_broker_env(**overrides: str):
    """Set SITES_HULL_* variables for the duration of a block, restoring after."""
    keys = [
        "SITES_HULL_BROKER_ENABLED",
        "SITES_HULL_BROKER_URL",
        "SITES_HULL_BROKER_SERVICE",
        "SITES_HULL_BROKER_PORT",
        "SITES_HULL_TOKEN_EXPIRATION_SECONDS",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def payload(**extra):
    body = {
        "name": "demo",
        "image": "example.invalid/demo:v1",
        "port": 8080,
        "healthPath": "/",
    }
    body.update(extra)
    return body


def spec_for(*, user_id: str = DEFAULT_USER_ID, **extra) -> dict:
    return normalize_deploy_payload(payload(**extra), DEFAULT_MERCHANT_ID, user_id)


class HullBrokerDisabledIsByteIdenticalTests(unittest.TestCase):
    """B1/B2: unset SITES_HULL_BROKER_ENABLED must change nothing.

    Not "unset produces a reasonable Deployment" -- literally the same dict as
    before this feature existed. The only variable across the two renders below
    is the environment; everything about the spec is otherwise identical, and
    an unrelated Hull env var being present (but the enabled flag absent) must
    not leak into the render either.
    """

    def test_deployment_resource_is_unaffected_when_unset(self) -> None:
        with hull_broker_env():
            baseline = deployment_resource(spec_for(), "ulocal-local")
        with hull_broker_env(SITES_HULL_BROKER_URL="http://hull-broker-gateway.sites-local.svc:8080"):
            with_unrelated_config = deployment_resource(spec_for(), "ulocal-local")
        self.assertEqual(baseline, with_unrelated_config)
        pod_spec = baseline["spec"]["template"]["spec"]
        self.assertNotIn("serviceAccountName", pod_spec)
        volume_names = {v["name"] for v in pod_spec["volumes"]}
        self.assertNotIn("hull-token", volume_names)
        env_names = {e["name"] for e in pod_spec["containers"][0]["env"]}
        self.assertFalse(env_names & {"HULL_BROKER_URL", "HULL_TOKEN_FILE", "HULL_APP_ENV_ID", "HULL_SITE"})

    def test_network_policy_egress_is_unaffected_when_unset(self) -> None:
        with hull_broker_env():
            baseline = network_policy_resources(spec_for(), "ulocal-local")
        with hull_broker_env(SITES_HULL_BROKER_URL="http://hull-broker-gateway.sites-local.svc:8080"):
            with_unrelated_config = network_policy_resources(spec_for(), "ulocal-local")
        self.assertEqual(baseline, with_unrelated_config)

    def test_service_account_resource_is_none_when_unset(self) -> None:
        with hull_broker_env():
            self.assertIsNone(hull_service_account_resource(spec_for(), "ulocal-local"))


class HullBrokerEnabledTests(unittest.TestCase):
    def test_service_account_named_after_the_cr_and_has_no_rbac_footprint(self) -> None:
        with hull_broker_env(SITES_HULL_BROKER_ENABLED="1"):
            spec = spec_for()
            resource = hull_service_account_resource(spec, "ulocal-local")
        self.assertIsNotNone(resource)
        assert resource is not None
        expected_name = cr_name_for(
            spec["merchantID"], spec["userID"], spec["serviceName"]
        )
        self.assertEqual(resource["metadata"]["name"], expected_name)
        self.assertEqual(resource["kind"], "ServiceAccount")
        self.assertIs(resource["automountServiceAccountToken"], False)
        # No RBAC-shaped keys anywhere on the object: it is not a Role/Binding
        # and does not carry annotations that would attach one implicitly.
        self.assertEqual(
            set(resource) - {"apiVersion", "kind", "metadata", "automountServiceAccountToken"},
            set(),
        )

    def test_pod_gets_service_account_and_projected_token_volume(self) -> None:
        with hull_broker_env(SITES_HULL_BROKER_ENABLED="true"):
            spec = spec_for()
            deployment = deployment_resource(spec, "ulocal-local")
        pod_spec = deployment["spec"]["template"]["spec"]
        expected_name = cr_name_for(
            spec["merchantID"], spec["userID"], spec["serviceName"]
        )
        self.assertEqual(pod_spec["serviceAccountName"], expected_name)
        # automountServiceAccountToken must stay false at the Pod level even
        # though a real ServiceAccount is now attached: the projected volume
        # below is the only credential this Pod is meant to see, not the
        # ServiceAccount's own ambient token.
        self.assertIs(pod_spec["automountServiceAccountToken"], False)
        volumes = {v["name"]: v for v in pod_spec["volumes"]}
        self.assertIn("hull-token", volumes)
        source = volumes["hull-token"]["projected"]["sources"][0]["serviceAccountToken"]
        self.assertEqual(source["path"], "token")
        self.assertEqual(source["audience"], "hull-broker")
        self.assertEqual(source["expirationSeconds"], 3600)
        mounts = {m["name"]: m for m in pod_spec["containers"][0]["volumeMounts"]}
        self.assertIn("hull-token", mounts)
        self.assertEqual(mounts["hull-token"]["mountPath"] + "/token", HULL_TOKEN_FILE)
        self.assertTrue(mounts["hull-token"]["readOnly"])

    def test_env_values_and_hex_app_env_id(self) -> None:
        with hull_broker_env(
            SITES_HULL_BROKER_ENABLED="1",
            SITES_HULL_BROKER_URL="http://hull-broker-gateway.sites-local.svc:8080",
        ):
            spec = spec_for(user_id=HEX_USER_ID)
            deployment = deployment_resource(spec, "uacme-hexid")
        env = {
            e["name"]: e["value"]
            for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(env["HULL_BROKER_URL"], "http://hull-broker-gateway.sites-local.svc:8080")
        self.assertEqual(env["HULL_TOKEN_FILE"], HULL_TOKEN_FILE)
        self.assertEqual(env["HULL_APP_ENV_ID"], HEX_USER_ID)
        self.assertEqual(env["HULL_SITE"], "demo")

    def test_app_env_id_is_empty_for_a_non_hex_user_id(self) -> None:
        # DEFAULT_USER_ID is "local" -- a valid userID (normalize_user_id
        # accepts it) but not 32 lowercase hex, so it must not leak into
        # HULL_APP_ENV_ID as though it were the acting-subject pseudonym.
        with hull_broker_env(SITES_HULL_BROKER_ENABLED="1"):
            deployment = deployment_resource(spec_for(), "ulocal-local")
        env = {
            e["name"]: e["value"]
            for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(env["HULL_APP_ENV_ID"], "")

    def test_caller_declared_env_cannot_override_the_hull_identity(self) -> None:
        """Control-plane identity wins over tenant-declared env of the same name.

        The opposite precedence from HOME/PORT: a workload able to override its
        own HULL_APP_ENV_ID or HULL_BROKER_URL could impersonate another
        tenant to the Broker or redirect its own traffic while still presenting
        a genuine hull-token.
        """
        with hull_broker_env(SITES_HULL_BROKER_ENABLED="1"):
            spec = spec_for(
                env=[
                    {"name": "HULL_APP_ENV_ID", "value": "f" * 32},
                    {"name": "HULL_BROKER_URL", "value": "http://attacker.invalid"},
                    {"name": "HULL_SITE", "value": "not-the-real-site"},
                    {"name": "HULL_TOKEN_FILE", "value": "/tmp/elsewhere"},
                    {"name": "SAFE_VAR", "value": "kept"},
                ]
            )
            deployment = deployment_resource(spec, "ulocal-local")
        env_list = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        # A duplicate name is not merely redundant: Kubernetes does not
        # deduplicate container env, and most libc getenv() implementations
        # return the *first* match. A caller-declared entry left ahead of the
        # control-plane's real one would silently win at runtime even though a
        # naive last-value-wins dict built from this same list would still show
        # the correct value -- so this asserts uniqueness, not just presence.
        names = [e["name"] for e in env_list]
        for reserved in ("HULL_APP_ENV_ID", "HULL_BROKER_URL", "HULL_SITE", "HULL_TOKEN_FILE"):
            self.assertEqual(names.count(reserved), 1, f"{reserved} appears {names.count(reserved)} times")
        env = {e["name"]: e["value"] for e in env_list}
        self.assertEqual(env["HULL_APP_ENV_ID"], "")
        self.assertEqual(env["HULL_BROKER_URL"], "")
        self.assertEqual(env["HULL_SITE"], "demo")
        self.assertEqual(env["HULL_TOKEN_FILE"], HULL_TOKEN_FILE)
        # A caller-declared name outside the four reserved ones is untouched.
        self.assertEqual(env["SAFE_VAR"], "kept")

    def test_network_policy_egress_targets_the_broker_gateway_port(self) -> None:
        with hull_broker_env(SITES_HULL_BROKER_ENABLED="1"):
            policies = network_policy_resources(spec_for(), "ulocal-local")
        allow_policy = next(p for p in policies if p["metadata"]["name"].endswith("-allow-http"))
        egress = allow_policy["spec"]["egress"]
        hull_rules = [
            rule
            for rule in egress
            if rule.get("ports") == [{"protocol": "TCP", "port": exposure.HULL_BROKER_GATEWAY_PORT}]
        ]
        self.assertEqual(len(hull_rules), 1)
        to = hull_rules[0]["to"][0]
        self.assertEqual(
            to["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "sites-local",
        )
        self.assertEqual(
            to["podSelector"]["matchLabels"]["app.kubernetes.io/name"],
            exposure.HULL_BROKER_GATEWAY_NAME,
        )

    def test_network_policy_egress_applies_regardless_of_database_field(self) -> None:
        """Gated on the platform toggle, not a per-spec field like the database rule next to it."""
        with hull_broker_env(SITES_HULL_BROKER_ENABLED="1"):
            policies = network_policy_resources(spec_for(), "ulocal-local")
        allow_policy = next(p for p in policies if p["metadata"]["name"].endswith("-allow-http"))
        ports = [
            rule.get("ports")
            for rule in allow_policy["spec"]["egress"]
            if rule.get("ports") == [{"protocol": "TCP", "port": exposure.HULL_BROKER_GATEWAY_PORT}]
        ]
        self.assertEqual(len(ports), 1)


class HullTokenExpirationTests(unittest.TestCase):
    def test_default_is_3600(self) -> None:
        with hull_broker_env():
            self.assertEqual(hull_token_expiration_seconds(), 3600)

    def test_custom_value_above_the_floor(self) -> None:
        with hull_broker_env(SITES_HULL_TOKEN_EXPIRATION_SECONDS="7200"):
            self.assertEqual(hull_token_expiration_seconds(), 7200)

    def test_rejects_below_the_kubernetes_minimum(self) -> None:
        with hull_broker_env(SITES_HULL_TOKEN_EXPIRATION_SECONDS="599"):
            with self.assertRaises(ValidationError):
                hull_token_expiration_seconds()

    def test_accepts_exactly_the_minimum(self) -> None:
        with hull_broker_env(
            SITES_HULL_TOKEN_EXPIRATION_SECONDS=str(HULL_TOKEN_MIN_EXPIRATION_SECONDS)
        ):
            self.assertEqual(
                hull_token_expiration_seconds(), HULL_TOKEN_MIN_EXPIRATION_SECONDS
            )

    def test_non_integer_value_refuses_rather_than_falling_back(self) -> None:
        with hull_broker_env(SITES_HULL_TOKEN_EXPIRATION_SECONDS="soon"):
            with self.assertRaises(ValidationError):
                hull_token_expiration_seconds()

    def test_expiration_feeds_the_projected_volume(self) -> None:
        with hull_broker_env(
            SITES_HULL_BROKER_ENABLED="1",
            SITES_HULL_TOKEN_EXPIRATION_SECONDS="1800",
        ):
            deployment = deployment_resource(spec_for(), "ulocal-local")
        pod_spec = deployment["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod_spec["volumes"]}
        source = volumes["hull-token"]["projected"]["sources"][0]["serviceAccountToken"]
        self.assertEqual(source["expirationSeconds"], 1800)


class HullBrokerEnabledFlagParsingTests(unittest.TestCase):
    def test_unset_is_false(self) -> None:
        with hull_broker_env():
            self.assertFalse(exposure.hull_broker_enabled())

    def test_common_true_and_false_spellings(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            with hull_broker_env(SITES_HULL_BROKER_ENABLED=value):
                self.assertTrue(exposure.hull_broker_enabled(), value)
        for value in ("0", "false", "FALSE", "no", "off"):
            with hull_broker_env(SITES_HULL_BROKER_ENABLED=value):
                self.assertFalse(exposure.hull_broker_enabled(), value)

    def test_garbage_value_refuses_rather_than_guessing(self) -> None:
        with hull_broker_env(SITES_HULL_BROKER_ENABLED="disabled"):
            with self.assertRaises(RuntimeError):
                exposure.hull_broker_enabled()


if __name__ == "__main__":
    unittest.main()
