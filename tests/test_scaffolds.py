"""Executable scaffold catalog and version-policy evidence."""
from __future__ import annotations

import unittest

from sites.scaffolds import scaffold_catalog
from sites.validation import ValidationError
from sites.version_policy import normalize_version_policy
from sites.migrations import MAX_MIGRATION_BYTES


class ScaffoldCatalogTests(unittest.TestCase):
    def test_migration_artifact_limit_fits_inside_request_budget(self) -> None:
        self.assertLess(MAX_MIGRATION_BYTES, 64 * 1024)

    def test_catalog_separates_contract_checks_from_e2e(self) -> None:
        catalog = scaffold_catalog()
        self.assertEqual(catalog["summary"]["profiles"], 4)
        self.assertEqual(catalog["summary"]["failed"], 0)
        self.assertGreater(catalog["summary"]["passed"], 0)
        self.assertEqual(catalog["summary"]["notRun"], 4)
        self.assertEqual(catalog["summary"]["contractCheckSuccessRate"], 1.0)
        self.assertIsNone(catalog["methodology"]["agentEndToEndSuccessRate"])

    def test_dynamic_profiles_are_postgresql_schema_only(self) -> None:
        dynamic = [
            item for item in scaffold_catalog()["scaffolds"]
            if item["siteType"] == "dynamic"
        ]
        self.assertEqual(
            {item["database"] for item in dynamic}, {"postgresql-schema"}
        )
        self.assertEqual(
            {item["recommendedTool"] for item in dynamic},
            {"deploy_dynamic"},
        )

    def test_static_profiles_disclose_object_storage_gap(self) -> None:
        static = [
            item for item in scaffold_catalog()["scaffolds"]
            if item["siteType"] == "static"
        ]
        self.assertTrue(
            all(
                any("Object" in limitation or "S3/OSS" in limitation for limitation in item["limitations"])
                for item in static
            )
        )

    def test_destructive_shared_schema_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "destructive schema changes"):
            normalize_version_policy(
                {
                    "schemaChange": "destructive",
                    "migrationStrategy": "manual-cutover",
                }
            )

    def test_additive_policy_requires_migration_digest(self) -> None:
        with self.assertRaisesRegex(ValidationError, "migrationSha256"):
            normalize_version_policy(
                {
                    "schemaChange": "additive",
                    "migrationStrategy": "expand-contract",
                }
            )


if __name__ == "__main__":
    unittest.main()
