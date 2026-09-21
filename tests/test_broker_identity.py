"""Tests for sites.broker.identity: app/user token verification, TokenReviewer,
and the workload-to-app exchange (contract section 3.3)."""
from __future__ import annotations

import unittest

from sites.broker import db as broker_db
from sites.broker import identity
from sites.broker import tokens as broker_tokens

from tests import broker_fixtures as fx


class TokenVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = fx.keyring()

    def test_verify_app_token_round_trips(self) -> None:
        token, _ = broker_tokens.issue_app_token(self.keys, "a" * 32, "site-1")
        caller = identity.verify_app_token(self.keys, token)
        self.assertEqual(caller.app_env_id, "a" * 32)
        self.assertEqual(caller.site, "site-1")

    def test_verify_user_token_requires_matching_audience(self) -> None:
        app_env_id = "b" * 32
        token, _ = broker_tokens.issue_user_token(
            self.keys, app_env_id=app_env_id, user_id="u1", session_id="s1",
            org=None, role="member", plan="free", env="prod",
        )
        user = identity.verify_user_token(self.keys, token, app_env_id=app_env_id)
        self.assertEqual(user.id, "u1")
        with self.assertRaises(broker_tokens.TokenError) as ctx:
            identity.verify_user_token(self.keys, token, app_env_id="c" * 32)
        self.assertEqual(ctx.exception.code, "invalid_claims")

    def test_app_token_is_not_accepted_as_a_user_token(self) -> None:
        app_env_id = "d" * 32
        token, _ = broker_tokens.issue_app_token(self.keys, app_env_id, "site")
        with self.assertRaises(broker_tokens.TokenError):
            identity.verify_user_token(self.keys, token, app_env_id=app_env_id)


class ParseServiceAccountTests(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(
            identity.parse_service_account("system:serviceaccount:ns-a:sa-a"),
            ("ns-a", "sa-a"),
        )

    def test_rejects_non_service_account_identities(self) -> None:
        for bad in ("system:node:worker-1", "", "system:serviceaccount:ns-only",
                    "system:serviceaccount::", "system:serviceaccount:ns:"):
            with self.subTest(bad=bad):
                self.assertIsNone(identity.parse_service_account(bad))


class FakeTokenReviewerTests(unittest.TestCase):
    def test_unknown_token_reviews_to_none(self) -> None:
        reviewer = identity.FakeTokenReviewer()
        self.assertIsNone(reviewer.review("nope"))

    def test_known_token_reviews_to_its_username(self) -> None:
        reviewer = identity.FakeTokenReviewer({"tok": "system:serviceaccount:ns:sa"})
        self.assertEqual(reviewer.review("tok"), "system:serviceaccount:ns:sa")


class IssueAppTokenForWorkloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fx.database()
        self.keys = fx.keyring()
        self.app_env_id = fx.app_env_id()
        with self.db.transaction() as cursor:
            broker_db.put_settings(cursor, self.app_env_id, {
                "workloads": [{"namespace": f"ns-{self.app_env_id[:12]}", "serviceAccount": "sa-x", "site": "svc-x"}],
            })

    def test_known_workload_yields_a_scoped_app_token(self) -> None:
        reviewer = identity.FakeTokenReviewer({"sa-tok": f"system:serviceaccount:ns-{self.app_env_id[:12]}:sa-x"})
        result = identity.issue_app_token_for_workload(self.db, self.keys, reviewer, "sa-tok")
        self.assertIsNotNone(result)
        token, _ = result
        caller = identity.verify_app_token(self.keys, token)
        self.assertEqual(caller.app_env_id, self.app_env_id)
        self.assertEqual(caller.site, "svc-x")

    def test_unauthenticated_token_yields_none(self) -> None:
        reviewer = identity.FakeTokenReviewer()
        self.assertIsNone(
            identity.issue_app_token_for_workload(self.db, self.keys, reviewer, "unknown")
        )

    def test_authenticated_but_unmapped_workload_yields_none(self) -> None:
        reviewer = identity.FakeTokenReviewer({"sa-tok": "system:serviceaccount:ns-y:sa-y"})
        self.assertIsNone(
            identity.issue_app_token_for_workload(self.db, self.keys, reviewer, "sa-tok")
        )


class ServiceAccountAnnotationMappingTests(unittest.TestCase):
    """The operator annotates each per-site ServiceAccount, so a workload maps to
    its app without anyone registering it by hand."""

    def setUp(self) -> None:
        self.db = fx.database()
        self.keys = fx.keyring()
        self.app_env_id = fx.app_env_id()
        with self.db.transaction() as cursor:
            broker_db.ensure_app(cursor, self.app_env_id)
            broker_db.insert_config_version(cursor, self.app_env_id, 1, "d" * 64, {"apiVersion": "hull/v1alpha1"})
            broker_db.set_active_version(cursor, self.app_env_id, 1)
        # A namespace no other test registers in the shared database's workloads table.
        self.namespace = "ns-" + fx.app_env_id()[:12]
        self.reviewer = identity.FakeTokenReviewer({"sa-tok": f"system:serviceaccount:{self.namespace}:sa"})

    def _annotate(self, app_env_id: str, site: str = "web") -> None:
        self.reviewer.annotate(self.namespace, "sa", {identity.APP_ENV_ID_ANNOTATION: app_env_id,
                                                      identity.SITE_ANNOTATION: site})

    def test_annotated_workload_of_a_configured_app_gets_a_token(self) -> None:
        self._annotate(self.app_env_id)
        token, _ = identity.issue_app_token_for_workload(self.db, self.keys, self.reviewer, "sa-tok")
        caller = identity.verify_app_token(self.keys, token)
        self.assertEqual((caller.app_env_id, caller.site), (self.app_env_id, "web"))

    def test_an_app_the_broker_has_never_configured_gets_nothing(self) -> None:
        self._annotate(fx.app_env_id())
        self.assertIsNone(identity.issue_app_token_for_workload(self.db, self.keys, self.reviewer, "sa-tok"))

    def test_a_malformed_app_id_gets_nothing(self) -> None:
        self._annotate("not-a-pseudonym")
        self.assertIsNone(identity.issue_app_token_for_workload(self.db, self.keys, self.reviewer, "sa-tok"))

    def test_no_annotation_gets_nothing(self) -> None:
        self.assertIsNone(identity.issue_app_token_for_workload(self.db, self.keys, self.reviewer, "sa-tok"))


if __name__ == "__main__":
    unittest.main()
