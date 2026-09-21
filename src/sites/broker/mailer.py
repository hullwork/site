"""Outbound mail for the end-user auth flow: an interface, plus two implementations.

The slice never talks to a real mail transport -- ``DatabaseMailer`` is what ships,
and the admin API's dev-only ``GET .../mail`` endpoint is how anyone finds out what
would have been sent.
"""
from __future__ import annotations

from sites.broker import db as broker_db


class Mailer:
    def send(self, app_env_id: str, to_email: str, subject: str, body: str) -> None:
        raise NotImplementedError


class DatabaseMailer(Mailer):
    def __init__(self, db: broker_db.BrokerDatabase):
        self._db = db

    def send(self, app_env_id: str, to_email: str, subject: str, body: str) -> None:
        with self._db.transaction() as cursor:
            broker_db.insert_outbound_mail(cursor, app_env_id, to_email, subject, body)


class InMemoryMailer(Mailer):
    """Test double: keeps every message sent, in order, nothing persisted."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, app_env_id: str, to_email: str, subject: str, body: str) -> None:
        self.sent.append(
            {"appEnvId": app_env_id, "to": to_email, "subject": subject, "body": body}
        )
