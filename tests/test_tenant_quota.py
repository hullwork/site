"""Merchant-tier resource quotas: persistence, CR issuance, and propagation.

Resource ceilings were once three global environment variables, giving every tenant the
same ResourceQuota. These tests cover merchant-specific tiers stored in metadata,
propagated through custom resources, and reconciled onto existing tenant Namespaces.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from pathlib import Path

from sites.k8s_resources import default_tenant_quota, normalize_tenant_quota
from tests.test_support import postgres_store


def token_digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MerchantResourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = postgres_store(Path(directory.name) / "sites.db")
        self.store.migrate()
        self.store.create_merchant("acme", "Acme", token_digest("k"), 10, 20)

    def test_unset_merchant_reads_as_none(self) -> None:
        """Not configured = None, the caller will fall back to the default file.

        There is no row of existing merchants, and if an error occurs if it cannot be read or half of the data is returned, the entire deployment will fail.
        """
        self.assertIsNone(self.store.merchant_resources("acme"))

    def test_round_trip(self) -> None:
        quota = {"cpu": "16", "memory": "32Gi", "pods": "64"}
        self.store.set_merchant_resources("acme", quota)
        self.assertEqual(self.store.merchant_resources("acme"), quota)

    def test_second_write_overwrites(self) -> None:
        """Whole Coverage: Three values together form a package."""
        self.store.set_merchant_resources("acme", {"cpu": "8", "memory": "8Gi", "pods": "32"})
        self.store.set_merchant_resources("acme", {"cpu": "2", "memory": "2Gi", "pods": "8"})
        self.assertEqual(
            self.store.merchant_resources("acme"),
            {"cpu": "2", "memory": "2Gi", "pods": "8"},
        )

    def test_merchants_do_not_share_a_row(self) -> None:
        self.store.create_merchant("globex", "Globex", token_digest("k2"), 1, 1)
        self.store.set_merchant_resources("acme", {"cpu": "16", "memory": "32Gi", "pods": "64"})
        self.assertIsNone(self.store.merchant_resources("globex"))

    def test_schema_is_created_by_migrate_not_by_alter(self) -> None:
        """This table uses CREATE TABLE IF NOT EXISTS and does not rely on any ALTER.

        Adding columns to sites_merchants is the original intuitive approach, but migrate() will only run the table template.
        The independent table keeps the merchant row stable and allows existing merchants
        to naturally fall back to the default file.
        """
        self.store.migrate()          # Idempotent: The second run should not explode
        self.assertIsNone(self.store.merchant_resources("acme"))


class QuotaNormalisationTests(unittest.TestCase):
    def test_partial_input_keeps_the_rest_at_defaults(self) -> None:
        defaults = default_tenant_quota()
        merged = normalize_tenant_quota({"cpu": "8"})
        self.assertEqual(merged["cpu"], "8")
        self.assertEqual(merged["memory"], defaults["memory"])

    def test_junk_input_falls_back_entirely(self) -> None:
        for value in (None, "4", [], {"cpu": "  "}):
            with self.subTest(value=value):
                self.assertEqual(normalize_tenant_quota(value), default_tenant_quota())


class _FakeKube:
    """Only two methods used for propagation are implemented."""

    def __init__(self, items):
        self.items = items
        self.patches: list[tuple[str, dict]] = []
        self.fail_list = False

    def get(self, path):
        if self.fail_list:
            raise RuntimeError("apiserver unavailable")
        return {"items": self.items}

    def patch(self, path, body):
        self.patches.append((path, body))
        return body


class PropagationTests(unittest.TestCase):
    """🔴 After changing the file, the new value must be written into all existing CRs of the merchant.

    ResourceQuota is a copy of the Namespace level, and the operator presses that copy every time it processes a site.
    The site spec is written once. When the old CR has the old value and the new CR has the new value, they overwrite each other every round——
    The phenomenon is that the quota jumps between two numbers, while each CR looks "normal" individually.
    """

    NEW = {"cpu": "16", "memory": "32Gi", "pods": "64"}

    def handler(self, items):
        from sites.api import Handler

        handler = Handler.__new__(Handler)
        handler.kube = _FakeKube(items)
        return handler

    def cr(self, name, merchant, quota=None):
        spec = {"merchantID": merchant, "userID": "alice", "serviceName": name}
        if quota is not None:
            spec["tenantQuota"] = quota
        return {"metadata": {"name": name}, "spec": spec}

    def test_every_cr_of_that_merchant_is_updated(self) -> None:
        handler = self.handler([self.cr("a", "acme"), self.cr("b", "acme")])
        changed = handler._propagate_tenant_quota("acme", self.NEW)
        self.assertEqual(changed, 2)
        for _, body in handler.kube.patches:
            self.assertEqual(body["spec"]["tenantQuota"], self.NEW)

    def test_other_merchants_are_left_alone(self) -> None:
        """Filter by merchantID. Without this item, increasing the quota for one merchant will increase the entire platform."""
        handler = self.handler([self.cr("a", "acme"), self.cr("b", "globex")])
        handler._propagate_tenant_quota("acme", self.NEW)
        self.assertEqual(len(handler.kube.patches), 1)
        self.assertTrue(handler.kube.patches[0][0].endswith("/a"))

    def test_already_current_crs_are_not_rewritten(self) -> None:
        """No need to patch if the value is already correct: rewriting it completely every time the file is changed will push the generation in vain.
        And the operator is ready according to observedGeneration."""
        handler = self.handler([self.cr("a", "acme", self.NEW)])
        self.assertEqual(handler._propagate_tenant_quota("acme", self.NEW), 0)
        self.assertEqual(handler.kube.patches, [])

    def test_a_failed_list_does_not_raise(self) -> None:
        """Propagation failure cannot cause the entire tier change to fail: the library is already a new value, and the next tier change or the next deployment
        Will continue to converge. If thrown, the caller will think that the quota has not been changed."""
        handler = self.handler([])
        handler.kube.fail_list = True
        self.assertEqual(handler._propagate_tenant_quota("acme", self.NEW), 0)


class ConsoleContractTests(unittest.TestCase):
    """The console side must follow the real response.

    This repository suffered a loss: /v1/admin/images The server sends `images` and the frontend reads `repositories`.
    What is written in the mock is the name of the frontend - the page developed with the mock is completely self-consistent, and bugs are only found in the real world.
    The backend exists, and neither side is tested. Pin the three places together here.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent / "console/src"

    def test_the_server_always_sends_the_quota(self) -> None:
        """Fix the server side first: If the three places in the console are correct, but there is no such field in the response,
        The page will just appear blank.

        (This is made up through the rollback experiment: initially only three frontends of types/mock/api were nailed, and the server
        None of the echoes were red when deleted. )
        """
        from sites.api import Handler

        class _Store:
            def merchant_resources(self, merchant_id):
                return None

        handler = Handler.__new__(Handler)
        handler.store = _Store()
        view = handler._merchant_view(
            {
                "merchant_id": "acme",
                "display_name": "Acme",
                "max_tenants": 1,
                "max_deployments": 1,
            }
        )
        self.assertIn("tenantQuota", view)
        self.assertEqual(set(view["tenantQuota"]), {"cpu", "memory", "pods"})

    def test_the_view_type_declares_the_quota(self) -> None:
        types = (self.ROOT / "types.ts").read_text(encoding="utf-8")
        merchant_view = types[types.index("interface MerchantView") :]
        merchant_view = merchant_view[: merchant_view.index("}")]
        self.assertIn("tenantQuota", merchant_view)

    def test_every_mock_merchant_carries_what_the_server_always_sends(self) -> None:
        """The server **constantly** echoes tenantQuota. Without the mock, it will be looser than the real response.

        The frontend will then write the branch "This field may not exist" and verify it against the mock, while the real back-end
        Always given - the difference is only exposed when connecting to the backend, usually by rendering some undefined as blank.
        """
        mock = (self.ROOT / "mock.ts").read_text(encoding="utf-8")
        block = mock[mock.index("const merchants") :]
        block = block[: block.index("\n];")]
        merchants = block.count("merchantId:")
        self.assertGreater(merchants, 0, "There is not a single merchant sample in the mock.")
        self.assertEqual(
            block.count("tenantQuota:"),
            merchants,
            "If a mock merchant does not bring tenantQuota, the frontend will be developed based on a form that is looser than the real response.",
        )

    def test_the_patch_type_accepts_it(self) -> None:
        api = (self.ROOT / "api.ts").read_text(encoding="utf-8")
        patch = api[api.index("interface MerchantPatch") :]
        patch = patch[: patch.index("}")]
        self.assertIn("tenantQuota", patch)


if __name__ == "__main__":
    unittest.main()
