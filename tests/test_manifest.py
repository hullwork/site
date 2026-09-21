"""Every rule `sites.manifest.validate` enforces gets a positive and a negative case.

The positive case is always the same starting manifest (`_base_manifest`), proven valid
once in `test_the_base_manifest_is_valid`; every negative case mutates exactly one field
away from that known-good shape and asserts the resulting error names the right path, so
a failure here points at the rule that broke rather than at "something in this big fixture
is wrong".
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from sites import manifest
from sites import manifest as manifest_mod


ROOT = Path(__file__).resolve().parent.parent


def _base_manifest() -> dict:
    return {
        "app": "support-desk",
        "auth": {
            "methods": ["email_otp"],
            "registration": "open",
            "visibility": "login_required",
            "organizations": False,
        },
        "roles": ["member", "org_admin"],
        "default_role": "member",
        "plans": {
            "free": {"features": ["chat"], "limits": {"ai_credits_month": 200}},
            "pro": {"features": ["chat", "auto_triage"], "limits": {"ai_credits_month": 5000}},
        },
        "default_plan": "free",
        "entities": {
            "tickets": {
                "table": "tickets",
                "primary_key": "id",
                "columns": ["id", "title", "status", "owner_id"],
            }
        },
        "permissions": {"member": {"tickets": {"actions": ["read"]}}},
        "secrets": ["ZENDESK_TOKEN"],
        "capabilities": [
            {
                "alias": "kb",
                "kind": "mcp",
                "url": "https://mcp.example.com/mcp",
                "tools": ["search_docs"],
                "credential": "app",
                "risk": "read",
                "pin": None,
            }
        ],
        "agents": {
            "triage": {
                "instructions": "you triage tickets",
                "scope": "classify tickets",
                "guardrails": ["never promise a refund"],
                "model_tier": "balanced",
                "skills": [],
                "tools": ["entity:tickets.read", "kb.search_docs"],
                "run_as": "end_user",
                "requires_feature": None,
                "budget": {"max_credits_per_run": 30},
            }
        },
        "flows": {
            "summarize_ticket": {
                "mode": "llm",
                "model_tier": "fast",
                "prompt": "summarize {{input.title}}",
                "input_schema": {},
                "output_schema": {},
                "fallback": {"summary": ""},
                "audience": "server",
                "requires_feature": None,
                "budget": {"max_credits_per_call": 10},
            }
        },
    }


def _errors(mutated: dict, **kwargs) -> list[str]:
    return manifest.validate(mutated, **kwargs)


def _has(errors: list[str], path: str) -> bool:
    return any(e.startswith(path + ":") for e in errors)


class BaseManifestTests(unittest.TestCase):
    def test_the_base_manifest_is_valid(self) -> None:
        self.assertEqual([], manifest.validate(_base_manifest()))


class ApiVersionTests(unittest.TestCase):
    def test_absent_is_fine(self) -> None:
        m = _base_manifest()
        self.assertEqual([], manifest.validate(m))

    def test_wrong_value_is_rejected(self) -> None:
        m = _base_manifest()
        m["apiVersion"] = "hull/v2"
        self.assertTrue(_has(_errors(m), "/apiVersion"))

    def test_correct_value_is_accepted(self) -> None:
        m = _base_manifest()
        m["apiVersion"] = "hull/v1alpha1"
        self.assertEqual([], manifest.validate(m))


class AppNameTests(unittest.TestCase):
    def test_bad_slug_is_rejected(self) -> None:
        m = _base_manifest()
        m["app"] = "Support Desk!"
        self.assertTrue(_has(_errors(m), "/app"))

    def test_missing_is_rejected(self) -> None:
        m = _base_manifest()
        del m["app"]
        self.assertTrue(_has(_errors(m), "/app"))


class RolesTests(unittest.TestCase):
    def test_empty_roles_rejected(self) -> None:
        m = _base_manifest()
        m["roles"] = []
        self.assertTrue(_has(_errors(m), "/roles"))

    def test_duplicate_role_rejected(self) -> None:
        m = _base_manifest()
        m["roles"] = ["member", "member"]
        errors = _errors(m)
        self.assertTrue(any("duplicate role name" in e for e in errors))

    def test_unique_roles_accepted(self) -> None:
        m = _base_manifest()
        m["roles"] = ["member", "org_admin", "viewer"]
        m["default_role"] = "member"
        self.assertEqual([], manifest.validate(m))

    def test_default_role_must_be_declared(self) -> None:
        m = _base_manifest()
        m["default_role"] = "ghost"
        self.assertTrue(_has(_errors(m), "/default_role"))

    def test_default_role_declared_accepted(self) -> None:
        m = _base_manifest()
        m["default_role"] = "org_admin"
        self.assertEqual([], manifest.validate(m))


class PlansTests(unittest.TestCase):
    def test_empty_plans_rejected(self) -> None:
        m = _base_manifest()
        m["plans"] = {}
        self.assertTrue(_has(_errors(m), "/plans"))

    def test_bad_feature_type_rejected(self) -> None:
        m = _base_manifest()
        m["plans"]["free"]["features"] = [1, 2]
        self.assertTrue(_has(_errors(m), "/plans/free/features"))

    def test_good_features_accepted(self) -> None:
        m = _base_manifest()
        m["plans"]["free"]["features"] = ["chat", "extra"]
        self.assertEqual([], manifest.validate(m))

    def test_negative_limit_rejected(self) -> None:
        m = _base_manifest()
        m["plans"]["free"]["limits"]["ai_credits_month"] = -5
        self.assertTrue(_has(_errors(m), "/plans/free/limits/ai_credits_month"))

    def test_null_limit_accepted(self) -> None:
        m = _base_manifest()
        m["plans"]["free"]["limits"]["ai_credits_month"] = None
        self.assertEqual([], manifest.validate(m))

    def test_default_plan_must_be_declared(self) -> None:
        m = _base_manifest()
        m["default_plan"] = "enterprise"
        self.assertTrue(_has(_errors(m), "/default_plan"))

    def test_default_plan_declared_accepted(self) -> None:
        m = _base_manifest()
        m["default_plan"] = "pro"
        self.assertEqual([], manifest.validate(m))


class EntitiesTests(unittest.TestCase):
    def test_bad_entity_name_rejected(self) -> None:
        m = _base_manifest()
        m["entities"]["Tickets!"] = m["entities"].pop("tickets")
        self.assertTrue(any("/entities/Tickets!" in e for e in _errors(m)))

    def test_good_entity_name_accepted(self) -> None:
        m = _base_manifest()
        m["entities"]["support_tickets"] = m["entities"].pop("tickets")
        m["permissions"]["member"]["support_tickets"] = m["permissions"]["member"].pop("tickets")
        m["agents"]["triage"]["tools"] = ["entity:support_tickets.read", "kb.search_docs"]
        self.assertEqual([], manifest.validate(m))

    def test_bad_table_name_rejected(self) -> None:
        m = _base_manifest()
        m["entities"]["tickets"]["table"] = "Tickets Table"
        self.assertTrue(_has(_errors(m), "/entities/tickets/table"))

    def test_bad_primary_key_pattern_rejected(self) -> None:
        m = _base_manifest()
        m["entities"]["tickets"]["primary_key"] = "ID!"
        self.assertTrue(_has(_errors(m), "/entities/tickets/primary_key"))

    def test_primary_key_not_in_columns_rejected(self) -> None:
        m = _base_manifest()
        m["entities"]["tickets"]["primary_key"] = "uuid"
        self.assertTrue(_has(_errors(m), "/entities/tickets/primary_key"))

    def test_primary_key_in_columns_accepted(self) -> None:
        m = _base_manifest()
        m["entities"]["tickets"]["columns"] = ["id", "title", "status", "owner_id", "uuid"]
        m["entities"]["tickets"]["primary_key"] = "uuid"
        self.assertEqual([], manifest.validate(m))

    def test_empty_columns_rejected(self) -> None:
        m = _base_manifest()
        m["entities"]["tickets"]["columns"] = []
        self.assertTrue(_has(_errors(m), "/entities/tickets/columns"))

    def test_duplicate_column_rejected(self) -> None:
        m = _base_manifest()
        m["entities"]["tickets"]["columns"] = ["id", "id"]
        errors = _errors(m)
        self.assertTrue(any("duplicate column name" in e for e in errors))


class PermissionsTests(unittest.TestCase):
    def test_undeclared_role_rejected(self) -> None:
        m = _base_manifest()
        m["permissions"]["ghost_role"] = {"tickets": {"actions": ["read"]}}
        self.assertTrue(_has(_errors(m), "/permissions/ghost_role"))

    def test_undeclared_entity_rejected(self) -> None:
        m = _base_manifest()
        m["permissions"]["member"]["ghost_entity"] = {"actions": ["read"]}
        self.assertTrue(_has(_errors(m), "/permissions/member/ghost_entity"))

    def test_declared_role_and_entity_accepted(self) -> None:
        m = _base_manifest()
        m["permissions"]["org_admin"] = {"tickets": {"actions": ["read", "update"]}}
        self.assertEqual([], manifest.validate(m))

    def test_bad_action_rejected(self) -> None:
        m = _base_manifest()
        m["permissions"]["member"]["tickets"]["actions"] = ["obliterate"]
        self.assertTrue(_has(_errors(m), "/permissions/member/tickets/actions/0"))

    def test_empty_actions_rejected(self) -> None:
        m = _base_manifest()
        m["permissions"]["member"]["tickets"]["actions"] = []
        self.assertTrue(_has(_errors(m), "/permissions/member/tickets/actions"))


class SecretsTests(unittest.TestCase):
    def test_lowercase_secret_rejected(self) -> None:
        m = _base_manifest()
        m["secrets"] = ["zendesk_token"]
        self.assertTrue(_has(_errors(m), "/secrets/0"))

    def test_uppercase_secret_accepted(self) -> None:
        m = _base_manifest()
        m["secrets"] = ["ZENDESK_TOKEN", "STRIPE_KEY"]
        self.assertEqual([], manifest.validate(m))

    def test_duplicate_secret_rejected(self) -> None:
        m = _base_manifest()
        m["secrets"] = ["ZENDESK_TOKEN", "ZENDESK_TOKEN"]
        errors = _errors(m)
        self.assertTrue(any("duplicate secret name" in e for e in errors))


class CapabilitiesTests(unittest.TestCase):
    def test_duplicate_alias_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"].append(dict(m["capabilities"][0]))
        errors = _errors(m)
        self.assertTrue(any("duplicate capability alias" in e for e in errors))

    def test_second_capability_accepted(self) -> None:
        m = _base_manifest()
        m["capabilities"].append(
            {
                "alias": "helpdesk",
                "kind": "mcp",
                "url": "https://mcp.example.com/helpdesk",
                "tools": ["list_macros"],
                "credential": "end_user",
                "risk": "write",
                "pin": None,
            }
        )
        self.assertEqual([], manifest.validate(m))

    def test_non_mcp_kind_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["kind"] = "connector"
        self.assertTrue(_has(_errors(m), "/capabilities/0/kind"))

    def test_non_http_url_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["url"] = "ftp://mcp.example.com"
        self.assertTrue(_has(_errors(m), "/capabilities/0/url"))

    def test_http_url_accepted(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["url"] = "http://mcp.example.com/mcp"
        self.assertEqual([], manifest.validate(m))

    def test_bad_credential_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["credential"] = "everyone"
        self.assertTrue(_has(_errors(m), "/capabilities/0/credential"))

    def test_end_user_credential_accepted(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["credential"] = "end_user"
        self.assertEqual([], manifest.validate(m))

    def test_bad_risk_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["risk"] = "catastrophic"
        self.assertTrue(_has(_errors(m), "/capabilities/0/risk"))

    def test_destructive_risk_accepted(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["risk"] = "destructive"
        self.assertEqual([], manifest.validate(m))

    def test_empty_tools_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["tools"] = []
        self.assertTrue(_has(_errors(m), "/capabilities/0/tools"))

    def test_bad_pin_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["pin"] = "not-a-digest"
        self.assertTrue(_has(_errors(m), "/capabilities/0/pin"))

    def test_valid_pin_accepted(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["pin"] = "sha256:" + "a" * 64
        self.assertEqual([], manifest.validate(m))


class AgentsTests(unittest.TestCase):
    def test_bad_run_as_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["run_as"] = "anonymous"
        self.assertTrue(_has(_errors(m), "/agents/triage/run_as"))

    def test_run_as_app_accepted_with_read_capability(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["run_as"] = "app"
        self.assertEqual([], manifest.validate(m))

    def test_run_as_app_rejects_write_capability(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["run_as"] = "app"
        m["capabilities"][0]["risk"] = "write"
        self.assertTrue(_has(_errors(m), "/agents/triage/tools/1"))

    def test_missing_budget_rejected(self) -> None:
        m = _base_manifest()
        del m["agents"]["triage"]["budget"]
        self.assertTrue(_has(_errors(m), "/agents/triage/budget/max_credits_per_run"))

    def test_zero_budget_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["budget"]["max_credits_per_run"] = 0
        self.assertTrue(_has(_errors(m), "/agents/triage/budget/max_credits_per_run"))

    def test_positive_budget_accepted(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["budget"]["max_credits_per_run"] = 1
        self.assertEqual([], manifest.validate(m))

    def test_too_many_tools_rejected(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["tools"] = [f"tool{i}" for i in range(11)]
        m["agents"]["triage"]["tools"] = [f"kb.tool{i}" for i in range(11)]
        self.assertTrue(_has(_errors(m), "/agents/triage/tools"))

    def test_tool_count_at_limit_accepted(self) -> None:
        m = _base_manifest()
        m["capabilities"][0]["tools"] = [f"tool{i}" for i in range(9)]
        m["agents"]["triage"]["tools"] = ["entity:tickets.read"] + [f"kb.tool{i}" for i in range(9)]
        self.assertEqual([], manifest.validate(m))

    def test_custom_max_tools_per_agent(self) -> None:
        m = _base_manifest()
        self.assertEqual([], manifest.validate(m, max_tools_per_agent=2))
        self.assertTrue(_has(_errors(m, max_tools_per_agent=1), "/agents/triage/tools"))

    def test_entity_write_reference_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["tools"] = ["entity:tickets.update"]
        self.assertTrue(_has(_errors(m), "/agents/triage/tools/0"))

    def test_entity_reference_to_undeclared_entity_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["tools"] = ["entity:customers.read"]
        self.assertTrue(_has(_errors(m), "/agents/triage/tools/0"))

    def test_capability_tool_not_listed_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["tools"] = ["kb.delete_everything"]
        self.assertTrue(_has(_errors(m), "/agents/triage/tools/0"))

    def test_reference_to_undeclared_capability_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["tools"] = ["ghost.search"]
        self.assertTrue(_has(_errors(m), "/agents/triage/tools/0"))

    def test_malformed_reference_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["tools"] = ["not-a-reference"]
        self.assertTrue(_has(_errors(m), "/agents/triage/tools/0"))

    def test_duplicate_tool_reference_rejected(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["tools"] = ["kb.search_docs", "kb.search_docs"]
        errors = _errors(m)
        self.assertTrue(any("duplicate tool reference" in e for e in errors))

    def test_reference_that_maps_to_an_overlong_tool_name_rejected(self) -> None:
        m = _base_manifest()
        long_alias = "a" * 32
        long_tool = "b" * 32
        m["capabilities"][0]["alias"] = long_alias
        m["capabilities"][0]["tools"] = [long_tool]
        m["agents"]["triage"]["tools"] = [f"{long_alias}.{long_tool}"]
        self.assertTrue(_has(_errors(m), "/agents/triage/tools/0"))


class FlowsTests(unittest.TestCase):
    def test_non_llm_mode_rejected(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["mode"] = "agent"
        self.assertTrue(_has(_errors(m), "/flows/summarize_ticket/mode"))

    def test_llm_mode_accepted(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["mode"] = "llm"
        self.assertEqual([], manifest.validate(m))

    def test_bad_audience_rejected(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["audience"] = "everyone"
        self.assertTrue(_has(_errors(m), "/flows/summarize_ticket/audience"))

    def test_public_audience_without_capabilities_accepted(self) -> None:
        m = _base_manifest()
        m["capabilities"] = []
        m["agents"]["triage"]["tools"] = ["entity:tickets.read"]
        m["flows"]["summarize_ticket"]["audience"] = "public"
        self.assertEqual([], manifest.validate(m))

    def test_public_audience_with_capabilities_rejected(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["audience"] = "public"
        self.assertTrue(_has(_errors(m), "/flows"))

    def test_missing_fallback_rejected(self) -> None:
        m = _base_manifest()
        del m["flows"]["summarize_ticket"]["fallback"]
        self.assertTrue(_has(_errors(m), "/flows/summarize_ticket/fallback"))

    def test_null_fallback_accepted(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["fallback"] = None
        self.assertEqual([], manifest.validate(m))

    def test_missing_budget_rejected(self) -> None:
        m = _base_manifest()
        del m["flows"]["summarize_ticket"]["budget"]
        self.assertTrue(_has(_errors(m), "/flows/summarize_ticket/budget/max_credits_per_call"))

    def test_positive_budget_accepted(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["budget"]["max_credits_per_call"] = 5
        self.assertEqual([], manifest.validate(m))


class AgentToolNameTests(unittest.TestCase):
    def test_capability_reference_mapping(self) -> None:
        self.assertEqual("kb__search_docs", manifest.agent_tool_name("kb.search_docs"))

    def test_entity_reference_mapping(self) -> None:
        self.assertEqual(
            "entity__tickets__read", manifest.agent_tool_name("entity:tickets.read")
        )

    def test_mapped_names_satisfy_the_agui_tool_name_grammar(self) -> None:
        import re

        pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
        self.assertTrue(pattern.fullmatch(manifest.agent_tool_name("kb.search_docs")))
        self.assertTrue(pattern.fullmatch(manifest.agent_tool_name("entity:tickets.read")))

    def test_refuses_a_reference_with_no_separator(self) -> None:
        with self.assertRaises(ValueError):
            manifest.agent_tool_name("nodotnocollon")

    def test_refuses_a_reference_that_would_overflow_the_grammar(self) -> None:
        with self.assertRaises(ValueError):
            manifest.agent_tool_name(("a" * 32) + "." + ("b" * 32))


class CompileTests(unittest.TestCase):
    def test_fills_defaults(self) -> None:
        m = _base_manifest()
        del m["default_role"]
        del m["default_plan"]
        del m["agents"]["triage"]["skills"]
        del m["agents"]["triage"]["requires_feature"]
        del m["flows"]["summarize_ticket"]["requires_feature"]
        del m["capabilities"][0]["pin"]

        compiled = manifest.compile(m, lambda p: f"<{p}>")

        self.assertEqual("hull/v1alpha1", compiled["apiVersion"])
        self.assertEqual("member", compiled["default_role"])
        self.assertEqual("free", compiled["default_plan"])
        self.assertEqual([], compiled["agents"]["triage"]["skills"])
        self.assertIsNone(compiled["agents"]["triage"]["requires_feature"])
        self.assertIsNone(compiled["flows"]["summarize_ticket"]["requires_feature"])
        self.assertIsNone(compiled["capabilities"][0]["pin"])

    def test_explicit_defaults_are_kept(self) -> None:
        m = _base_manifest()
        m["default_role"] = "org_admin"
        m["default_plan"] = "pro"
        compiled = manifest.compile(m, lambda p: f"<{p}>")
        self.assertEqual("org_admin", compiled["default_role"])
        self.assertEqual("pro", compiled["default_plan"])

    def test_inlines_markdown_instructions(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["instructions"] = "prompts/triage.md"

        def read_file(path: str) -> str:
            self.assertEqual("prompts/triage.md", path)
            return "You triage tickets.\n"

        compiled = manifest.compile(m, read_file)
        self.assertEqual("You triage tickets.\n", compiled["agents"]["triage"]["instructions"])

    def test_inlines_text_prompt(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["prompt"] = "prompts/summarize.txt"
        compiled = manifest.compile(m, lambda p: "summarize this ticket")
        self.assertEqual("summarize this ticket", compiled["flows"]["summarize_ticket"]["prompt"])

    def test_literal_instructions_are_not_treated_as_a_path(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["instructions"] = "just triage tickets politely"
        compiled = manifest.compile(m, lambda p: self.fail("should not read a file"))
        self.assertEqual(
            "just triage tickets politely", compiled["agents"]["triage"]["instructions"]
        )

    def test_refuses_absolute_instructions_path(self) -> None:
        m = _base_manifest()
        m["agents"]["triage"]["instructions"] = "/etc/prompts/triage.md"
        with self.assertRaises(manifest.ManifestError):
            manifest.compile(m, lambda p: "should not be reached")

    def test_refuses_traversal_in_prompt_path(self) -> None:
        m = _base_manifest()
        m["flows"]["summarize_ticket"]["prompt"] = "../../etc/passwd.md"
        with self.assertRaises(manifest.ManifestError):
            manifest.compile(m, lambda p: "should not be reached")

    def test_refuses_an_invalid_manifest_before_touching_the_filesystem(self) -> None:
        m = _base_manifest()
        del m["app"]
        with self.assertRaises(manifest.ManifestError) as ctx:
            manifest.compile(m, lambda p: self.fail("should not read a file"))
        self.assertTrue(any(e.startswith("/app:") for e in ctx.exception.errors))


class CanonicalDigestTests(unittest.TestCase):
    def test_canonical_json_is_compact_and_sorted(self) -> None:
        text = manifest.canonical_json({"b": 1, "a": 2})
        self.assertEqual('{"a":2,"b":1}', text)

    def test_digest_is_stable_across_key_order(self) -> None:
        compiled = manifest.compile(_base_manifest(), lambda p: f"<{p}>")
        reordered = json.loads(json.dumps(compiled))
        shuffled = {key: reordered[key] for key in reversed(list(reordered.keys()))}
        self.assertEqual(
            manifest.canonical_digest(compiled), manifest.canonical_digest(shuffled)
        )

    def test_digest_changes_when_content_changes(self) -> None:
        compiled = manifest.compile(_base_manifest(), lambda p: f"<{p}>")
        other = copy.deepcopy(compiled)
        other["app"] = "another-app"
        self.assertNotEqual(
            manifest.canonical_digest(compiled), manifest.canonical_digest(other)
        )

    def test_digest_is_sha256_hex(self) -> None:
        compiled = manifest.compile(_base_manifest(), lambda p: f"<{p}>")
        digest = manifest.canonical_digest(compiled)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")


class SchemaFileTests(unittest.TestCase):
    def test_schema_file_matches_the_python_source_of_truth(self) -> None:
        expected = json.dumps(manifest.JSON_SCHEMA, indent=2) + "\n"
        actual = (ROOT / "docs" / "hull-manifest.schema.json").read_text(encoding="utf-8")
        self.assertEqual(expected, actual)

    def test_schema_is_draft_2020_12(self) -> None:
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema", manifest.JSON_SCHEMA["$schema"]
        )

    def test_schema_accepts_the_base_manifest_shape(self) -> None:
        # A hand-rolled structural check standing in for a real JSON Schema validator
        # (none is a project dependency): every field the schema marks required exists
        # on the fixture, proving the two were not authored out of sync.
        base = _base_manifest()
        for key in manifest.JSON_SCHEMA["required"]:
            self.assertIn(key, base)


if __name__ == "__main__":
    unittest.main()


class FlowSchemaSubsetTests(unittest.TestCase):
    """A schema keyword the Broker does not enforce would be ignored at run time."""

    def test_an_unenforced_keyword_is_refused(self) -> None:
        manifest = _base_manifest()
        flow = next(iter(manifest["flows"].values()))
        flow["output_schema"] = {"type": "object", "properties": {"code": {"type": "string", "pattern": "^A"}}}
        errors = manifest_mod.validate(manifest)
        self.assertTrue(any("pattern" in e and "output_schema" in e for e in errors), errors)

    def test_the_supported_subset_is_accepted(self) -> None:
        manifest = _base_manifest()
        flow = next(iter(manifest["flows"].values()))
        flow["output_schema"] = {"type": "object", "required": ["summary"],
                                 "properties": {"summary": {"type": "string", "maxLength": 200}},
                                 "additionalProperties": False}
        self.assertEqual(manifest_mod.validate(manifest), [])
