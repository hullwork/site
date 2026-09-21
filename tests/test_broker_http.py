"""Tests for sites.broker.http: the transport-neutral Router/Request/Response
types shared by every route module.
"""
from __future__ import annotations

import unittest

from sites.broker.http import HttpError, Router


class RouterTests(unittest.TestCase):
    def test_match_extracts_path_parameters(self) -> None:
        router = Router()
        router.add("GET", "/_hull/admin/v1/apps/{appEnvId}/config", lambda r: None, auth="admin")
        match = router.match("GET", "/_hull/admin/v1/apps/abc123/config")
        self.assertIsNotNone(match)
        route, params = match
        self.assertEqual(params, {"appEnvId": "abc123"})

    def test_unmatched_path_returns_none(self) -> None:
        router = Router()
        router.add("GET", "/_hull/x", lambda r: None, auth="none")
        self.assertIsNone(router.match("GET", "/_hull/y"))

    def test_matching_path_wrong_method_is_405(self) -> None:
        router = Router()
        router.add("POST", "/_hull/x", lambda r: None, auth="none")
        with self.assertRaises(HttpError) as ctx:
            router.match("GET", "/_hull/x")
        self.assertEqual(ctx.exception.status, 405)
        self.assertEqual(ctx.exception.code, "method_not_allowed")

    def test_a_path_parameter_never_spans_a_slash(self) -> None:
        router = Router()
        router.add("GET", "/_hull/admin/v1/apps/{appEnvId}/config", lambda r: None, auth="admin")
        self.assertIsNone(router.match("GET", "/_hull/admin/v1/apps/a/b/config"))

    def test_unknown_auth_mode_is_rejected(self) -> None:
        router = Router()
        with self.assertRaises(ValueError):
            router.add("GET", "/_hull/x", lambda r: None, auth="everyone")


class HttpErrorTests(unittest.TestCase):
    def test_body_is_the_one_sentence_envelope(self) -> None:
        exc = HttpError(404, "unknown_app", "no app is configured for this host")
        self.assertEqual(exc.body(), {"error": "no app is configured for this host",
                                       "code": "unknown_app"})

    def test_extra_fields_are_included(self) -> None:
        exc = HttpError(400, "invalid_config", "bad config", errors=["x is required"])
        self.assertEqual(exc.body()["errors"], ["x is required"])


if __name__ == "__main__":
    unittest.main()
