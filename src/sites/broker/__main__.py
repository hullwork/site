"""Entry point: ``python -m sites.broker gateway|worker|ingest``."""
from __future__ import annotations

import sys
import time
from os import getenv
from pathlib import Path

from sites import telemetry, tracing
from sites.broker import config_store
from sites.broker import db as broker_db
from sites.broker import identity
from sites.broker import mailer as broker_mailer
from sites.broker import schema
from sites.broker import secretbox
from sites.broker import server as broker_server
from sites.broker import tokens as broker_tokens
from sites.broker.auth import AuthService
from sites.broker.http import Services


def _load_admin_token() -> str:
    token = (getenv("HULL_BROKER_ADMIN_TOKEN", "") or "").strip()
    token_file = (getenv("HULL_BROKER_ADMIN_TOKEN_FILE", "") or "").strip()
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as exc:
            raise RuntimeError(
                f"HULL_BROKER_ADMIN_TOKEN_FILE is unreadable: {token_file}"
            ) from exc
    if len(token) < 32:
        raise RuntimeError(
            "HULL_BROKER_ADMIN_TOKEN(_FILE) must contain at least 32 characters"
        )
    return token


def _build_services() -> Services:
    db = broker_db.BrokerDatabase.from_env()
    # Safe at every gateway start (not just the worker's own migrate cycle):
    # migrate() takes a transaction-scoped advisory lock, so N replicas racing
    # here serialize instead of double-applying a step.
    schema.migrate_all(db)
    keys = broker_tokens.KeyRing.from_env()
    secret_key = secretbox.key_from_env()
    config = config_store.ConfigStore(db, secret_key)
    dev_tokens = (getenv("HULL_BROKER_DEV_TOKENS", "") or "").strip() == "1"
    mailer = broker_mailer.DatabaseMailer(db)
    auth_service = AuthService(db, config, keys, mailer, secret_key)
    reviewer = identity.KubeTokenReviewer()
    return Services(
        db=db, config=config, keys=keys, clock=time.time, auth=auth_service,
        mailer=mailer, reviewer=reviewer, admin_token=_load_admin_token(),
        dev_tokens_enabled=dev_tokens,
    )


def _gateway() -> None:
    tracing.configure("hull-broker-gateway")
    services = _build_services()
    router = broker_server.build_router(services)
    host = getenv("HULL_BROKER_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(getenv("HULL_BROKER_PORT", "8080") or "8080")
    server = broker_server.listen(host, port, router, services)
    telemetry.log("hull_broker_gateway_listening", host=host, port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        tracing.shutdown()


def _worker() -> None:
    from sites.broker import worker

    worker.serve()


def _ingest() -> None:
    from sites.broker import ingest

    ingest.serve()


_ROLES = {"gateway": _gateway, "worker": _worker, "ingest": _ingest}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in _ROLES:
        print(f"usage: python -m sites.broker {{{'|'.join(_ROLES)}}}", file=sys.stderr)
        raise SystemExit(2)
    _ROLES[sys.argv[1]]()


if __name__ == "__main__":
    # Same reason as sites.api and sites.activator: once the log consumer is torn
    # down, an ordinary print() into a closed pipe can wedge the process.
    from sites import safe_stdout

    safe_stdout.install()
    telemetry.configure("hull-broker")
    main()
