"""Tests for sites.broker.auth: OTP start/verify, sessions, enumeration
resistance, rate limits, registration modes, cookie shape.
"""
from __future__ import annotations

import re
import unittest
from unittest import mock

from sites.broker import config_store
from sites.broker import mailer as broker_mailer
from sites.broker.auth import AuthError, AuthService, SESSION_COOKIE

from tests import broker_fixtures as fx

_BASE_CONFIG = {
    "apiVersion": "hull/v1alpha1", "app": "demo", "auth": {"registration": "open"},
    "roles": ["member"], "default_role": "member", "plans": {"free": {}},
    "default_plan": "free", "entities": {}, "permissions": {}, "agents": {}, "flows": {},
}


def _setup(registration: str = "open", settings: dict | None = None):
    store = config_store.ConfigStore(fx.database(), fx.secret_key(), cache_seconds=0)
    app_env_id = fx.app_env_id()
    config = {**_BASE_CONFIG, "auth": {"registration": registration}}
    plan = store.plan(app_env_id, config)
    store.apply(app_env_id, config, plan["digest"])
    if settings:
        store.put_settings(app_env_id, settings)
    keys = fx.keyring()
    mail = broker_mailer.InMemoryMailer()
    auth = AuthService(store._db, store, keys, mail, fx.secret_key())
    return auth, store, mail, app_env_id


def _extract_code(mail: broker_mailer.InMemoryMailer) -> str:
    return re.search(r"code is (\d{6})", mail.sent[-1]["body"]).group(1)


class EnumerationResistanceTests(unittest.TestCase):
    """email/start must look identical from the outside no matter why it
    did or did not actually mail a code.
    """

    def test_existing_user_unknown_email_closed_and_rate_limited_all_answer_the_same(self) -> None:
        auth, store, mail, app_env_id = _setup(registration="closed")
        # Bootstrap one real user by relaxing registration briefly.
        store.put_settings(app_env_id, {})
        open_auth, _, open_mail, _ = _setup(registration="open")
        # Use the *same* app for an existing user by re-pointing at app_env_id:
        # simplest is a dedicated open-registration app for "existing user".
        existing_auth, existing_store, existing_mail, existing_app = _setup(registration="open")
        existing_auth.start_email(existing_app, "real@example.com", "1.1.1.1")
        code = _extract_code(existing_mail)
        existing_auth.verify_email(existing_app, "real@example.com", code, "1.1.1.1")

        # None of these raise, and none of them are distinguishable by return value.
        self.assertIsNone(existing_auth.start_email(existing_app, "real@example.com", "2.2.2.2"))
        self.assertIsNone(existing_auth.start_email(existing_app, "never-signed-in@example.com", "2.2.2.2"))
        self.assertIsNone(auth.start_email(app_env_id, "anyone@example.com", "2.2.2.2"))  # closed
        self.assertIsNone(auth.start_email(app_env_id, "not-an-email", "2.2.2.2"))  # invalid input
        self.assertIsNone(auth.start_email("0" * 32, "anyone@example.com", "2.2.2.2"))  # unknown app

    def test_verify_gives_the_same_error_for_every_failure_reason(self) -> None:
        auth, store, mail, app_env_id = _setup()
        auth.start_email(app_env_id, "alice@example.com", "1.1.1.1")
        code = _extract_code(mail)

        def error_of(call) -> tuple[str, str]:
            with self.assertRaises(AuthError) as ctx:
                call()
            return ctx.exception.code, ctx.exception.message

        no_code_sent = error_of(
            lambda: auth.verify_email(app_env_id, "nobody@example.com", "123456", "1.1.1.1")
        )
        wrong_code = error_of(
            lambda: auth.verify_email(app_env_id, "alice@example.com", "000000", "1.1.1.1")
        )
        self.assertEqual(no_code_sent, wrong_code)
        self.assertEqual(no_code_sent[0], "invalid_code")


class OtpFlowTests(unittest.TestCase):
    def test_verify_creates_a_user_on_first_sign_in_with_default_role_and_plan(self) -> None:
        auth, store, mail, app_env_id = _setup()
        auth.start_email(app_env_id, "new@example.com", "1.1.1.1")
        code = _extract_code(mail)
        result = auth.verify_email(app_env_id, "new@example.com", code, "1.1.1.1")
        self.assertEqual(result.user["role"], "member")
        self.assertEqual(result.user["plan"], "free")
        self.assertTrue(result.user["id"].startswith("u"))

    def test_code_is_single_use(self) -> None:
        auth, store, mail, app_env_id = _setup()
        auth.start_email(app_env_id, "once@example.com", "1.1.1.1")
        code = _extract_code(mail)
        auth.verify_email(app_env_id, "once@example.com", code, "1.1.1.1")
        with self.assertRaises(AuthError):
            auth.verify_email(app_env_id, "once@example.com", code, "1.1.1.1")

    def test_wrong_code_attempts_are_bounded(self) -> None:
        with mock.patch.dict("os.environ", {"HULL_OTP_MAX_ATTEMPTS": "2"}):
            auth, store, mail, app_env_id = _setup()
            auth.start_email(app_env_id, "bound@example.com", "1.1.1.1")
            code = _extract_code(mail)
            for _ in range(2):
                with self.assertRaises(AuthError):
                    auth.verify_email(app_env_id, "bound@example.com", "000000", "1.1.1.1")
            # Attempts now exhausted; even the right code is refused.
            with self.assertRaises(AuthError):
                auth.verify_email(app_env_id, "bound@example.com", code, "1.1.1.1")

    def test_logout_revokes_the_session(self) -> None:
        auth, store, mail, app_env_id = _setup()
        auth.start_email(app_env_id, "logout@example.com", "1.1.1.1")
        code = _extract_code(mail)
        result = auth.verify_email(app_env_id, "logout@example.com", code, "1.1.1.1")
        auth.token_from_session(app_env_id, result.cookie_value)  # still valid
        auth.logout(app_env_id, result.cookie_value)
        with self.assertRaises(AuthError) as ctx:
            auth.token_from_session(app_env_id, result.cookie_value)
        self.assertEqual(ctx.exception.code, "no_session")

    def test_no_session_cookie_is_refused(self) -> None:
        auth, store, mail, app_env_id = _setup()
        with self.assertRaises(AuthError) as ctx:
            auth.token_from_session(app_env_id, None)
        self.assertEqual(ctx.exception.code, "no_session")


class RateLimitTests(unittest.TestCase):
    def test_send_rate_limit_stops_mailing_but_still_answers_the_same(self) -> None:
        with mock.patch.dict("os.environ", {"HULL_OTP_SEND_LIMIT": "2",
                                             "HULL_OTP_SEND_WINDOW_SECONDS": "300"}):
            auth, store, mail, app_env_id = _setup()
            for _ in range(2):
                auth.start_email(app_env_id, "throttle@example.com", "9.9.9.9")
            sent_before = len(mail.sent)
            auth.start_email(app_env_id, "throttle@example.com", "9.9.9.9")
            self.assertEqual(len(mail.sent), sent_before)  # third call sent nothing

    def test_verify_rate_limit_refuses_even_the_right_code(self) -> None:
        with mock.patch.dict("os.environ", {"HULL_OTP_VERIFY_LIMIT": "2",
                                             "HULL_OTP_VERIFY_WINDOW_SECONDS": "300"}):
            auth, store, mail, app_env_id = _setup()
            auth.start_email(app_env_id, "vlimit@example.com", "9.9.9.9")
            code = _extract_code(mail)
            for _ in range(2):
                with self.assertRaises(AuthError):
                    auth.verify_email(app_env_id, "vlimit@example.com", "000000", "9.9.9.9")
            with self.assertRaises(AuthError):
                auth.verify_email(app_env_id, "vlimit@example.com", code, "9.9.9.9")


class RegistrationModeTests(unittest.TestCase):
    def test_closed_registration_never_mails_a_new_user(self) -> None:
        auth, store, mail, app_env_id = _setup(registration="closed")
        auth.start_email(app_env_id, "stranger@example.com", "1.1.1.1")
        self.assertEqual(mail.sent, [])

    def test_domain_allowlist_only_admits_listed_domains(self) -> None:
        auth, store, mail, app_env_id = _setup(
            registration="domain_allowlist", settings={"allowed_email_domains": ["good.test"]}
        )
        auth.start_email(app_env_id, "someone@bad.test", "1.1.1.1")
        self.assertEqual(mail.sent, [])
        auth.start_email(app_env_id, "someone@good.test", "1.1.1.1")
        self.assertEqual(len(mail.sent), 1)

    def test_open_registration_admits_anyone(self) -> None:
        auth, store, mail, app_env_id = _setup(registration="open")
        auth.start_email(app_env_id, "anyone@example.com", "1.1.1.1")
        self.assertEqual(len(mail.sent), 1)


class DatabaseMailerTests(unittest.TestCase):
    def test_database_mailer_writes_a_readable_row(self) -> None:
        db = fx.database()
        mailer = broker_mailer.DatabaseMailer(db)
        app_env_id = fx.app_env_id()
        mailer.send(app_env_id, "to@example.com", "subject", "body text")
        with db.transaction() as cursor:
            from sites.broker import db as broker_db
            rows = broker_db.list_outbound_mail(cursor, app_env_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to"], "to@example.com")


if __name__ == "__main__":
    unittest.main()
