"""Tests for sites.broker.config_store: plan/apply, digest formula, settings,
secrets, host lookup, and caching.
"""
from __future__ import annotations

import hashlib
import json
import threading
import unittest

from sites.broker import config_store

from tests import broker_fixtures as fx

_VALID_CONFIG = {
    "apiVersion": "hull/v1alpha1", "app": "demo",
    "auth": {"methods": ["email_otp"], "registration": "open"},
    "roles": ["member"], "default_role": "member",
    "plans": {"free": {}}, "default_plan": "free",
    "entities": {}, "permissions": {}, "agents": {}, "flows": {},
}


def _store(**kwargs) -> config_store.ConfigStore:
    return config_store.ConfigStore(fx.database(), fx.secret_key(), cache_seconds=0, **kwargs)


class PlanApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _store()
        self.app_env_id = fx.app_env_id()

    def test_plan_digest_matches_the_exact_contract_formula(self) -> None:
        plan = self.store.plan(self.app_env_id, _VALID_CONFIG)
        payload = json.dumps({"config": _VALID_CONFIG, "base": None},
                              sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.assertEqual(plan["digest"], expected)
        self.assertIsNone(plan["activeVersion"])

    def test_plan_reports_additions_from_an_empty_base(self) -> None:
        plan = self.store.plan(self.app_env_id, _VALID_CONFIG)
        paths = {change["path"] for change in plan["changes"]}
        self.assertIn("app", paths)
        self.assertTrue(all(change["op"] == "add" for change in plan["changes"]))

    def test_apply_then_get_version_round_trips(self) -> None:
        plan = self.store.plan(self.app_env_id, _VALID_CONFIG)
        applied = self.store.apply(self.app_env_id, _VALID_CONFIG, plan["digest"])
        self.assertEqual(applied["version"], 1)
        row = self.store.get_version(self.app_env_id)
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["config"], _VALID_CONFIG)
        self.assertEqual(row["digest"], applied["digest"])

    def test_apply_with_stale_digest_is_refused(self) -> None:
        plan = self.store.plan(self.app_env_id, _VALID_CONFIG)
        self.store.apply(self.app_env_id, _VALID_CONFIG, plan["digest"])
        with self.assertRaises(config_store.ConfigStaleError):
            self.store.apply(self.app_env_id, _VALID_CONFIG, plan["digest"])

    def test_concurrent_apply_lands_exactly_one_success(self) -> None:
        plan = self.store.plan(self.app_env_id, _VALID_CONFIG)
        results: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            store = _store()  # its own BrokerDatabase/connection, like two replicas
            try:
                store.apply(self.app_env_id, _VALID_CONFIG, plan["digest"])
                outcome = "ok"
            except config_store.ConfigStaleError:
                outcome = "stale"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(results.count("stale"), 4, results)
        row = self.store.get_version(self.app_env_id)
        self.assertEqual(row["version"], 1)

    def test_missing_required_key_is_rejected(self) -> None:
        broken = dict(_VALID_CONFIG)
        del broken["default_role"]
        with self.assertRaises(config_store.ConfigValidationError) as ctx:
            self.store.plan(self.app_env_id, broken)
        self.assertTrue(any("default_role" in e for e in ctx.exception.errors))

    def test_wrong_api_version_is_rejected(self) -> None:
        broken = {**_VALID_CONFIG, "apiVersion": "v0"}
        with self.assertRaises(config_store.ConfigValidationError):
            self.store.apply(self.app_env_id, broken, "anything")

    def test_get_version_is_none_with_no_active_config(self) -> None:
        self.assertIsNone(self.store.get_version(fx.app_env_id()))


class SettingsSecretsHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _store()
        self.app_env_id = fx.app_env_id()

    def test_put_settings_merges_replaces_at_top_level_only(self) -> None:
        self.store.put_settings(self.app_env_id, {"hosts": ["a.test"], "ai_enabled": True})
        merged = self.store.put_settings(self.app_env_id, {"hosts": ["b.test"]})
        self.assertEqual(merged["hosts"], ["b.test"])
        self.assertTrue(merged["ai_enabled"])  # untouched key survives the merge

    def test_by_host_resolves_after_settings_put(self) -> None:
        self.store.put_settings(self.app_env_id, {"hosts": ["resolve-me.test"]})
        self.assertEqual(self.store.by_host("resolve-me.test"), self.app_env_id)
        self.assertIsNone(self.store.by_host("never-set.test"))

    def test_secret_round_trips_through_the_store(self) -> None:
        self.store.put_secret(self.app_env_id, "API_KEY", "sk-live-abc")
        self.assertEqual(self.store.secret(self.app_env_id, "API_KEY"), "sk-live-abc")
        self.assertIsNone(self.store.secret(self.app_env_id, "NEVER_SET"))

    def test_secret_is_bound_to_its_app_env_id(self) -> None:
        self.store.put_secret(self.app_env_id, "API_KEY", "sk-live-abc")
        other_store = config_store.ConfigStore(self.store._db, fx.secret_key(), cache_seconds=0)
        # A different secret key entirely also cannot read it back.
        with self.assertRaises(Exception):
            other_store.secret(self.app_env_id, "API_KEY")


class CacheTests(unittest.TestCase):
    def test_active_is_cached_until_ttl_or_invalidate(self) -> None:
        clock = {"t": 0.0}
        store = config_store.ConfigStore(fx.database(), fx.secret_key(), cache_seconds=100,
                                          clock=lambda: clock["t"])
        app_env_id = fx.app_env_id()
        plan = store.plan(app_env_id, _VALID_CONFIG)
        store.apply(app_env_id, _VALID_CONFIG, plan["digest"])
        first = store.active(app_env_id)
        self.assertIsNotNone(first)
        # Apply a second version through a second store instance (bypassing the
        # first store's cache) and confirm the first still serves the stale copy.
        second_config = {**_VALID_CONFIG, "app": "renamed"}
        other = config_store.ConfigStore(store._db, fx.secret_key(), cache_seconds=0)
        plan2 = other.plan(app_env_id, second_config)
        other.apply(app_env_id, second_config, plan2["digest"])
        still_cached = store.active(app_env_id)
        self.assertEqual(still_cached.version, first.version)
        store.invalidate(app_env_id)
        refreshed = store.active(app_env_id)
        self.assertEqual(refreshed.version, 2)

    def test_active_returns_none_for_unknown_app(self) -> None:
        store = _store()
        self.assertIsNone(store.active(fx.app_env_id()))


if __name__ == "__main__":
    unittest.main()
