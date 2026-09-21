"""Double-entry credit ledger: reserve, settle, release, and per-period usage.

Every credit movement is recorded as a balanced set of entries across three named
buckets per (app_env_id, subject, period): ``available`` (implicit headroom under the
configured limit), ``reserved`` (credits held by an in-flight call) and ``settled``
(credits actually spent). ``reserve`` debits ``available`` and credits ``reserved``;
``settle`` moves the reservation's hold out of ``reserved``, credits ``settled`` with
the clamped actual amount, and returns any unused hold to ``available``; ``release``
reverses a reservation outright. Every one of those movements sums to zero across the
buckets it touches, which is the property that makes ``broker_credit_entries`` an
audit trail rather than just a running total.

Concurrency correctness (two reservations racing for the last credits: exactly one
wins) comes from one conditional ``UPDATE ... WHERE reserved + settled + %s <= limit``
per subject, inside the same transaction as the idempotency check and the reservation
row insert. PostgreSQL's row lock on the ``broker_credit_periods`` row serializes
concurrent reservations for the same subject/period; the loser's ``UPDATE`` matches
zero rows and the whole transaction (including any subject it already touched) rolls
back, so there is nothing to compensate by hand.

SEAM: the design notes in-memory segment pre-allocation (claim a batch of credits
once per process and decrement it locally) as a latency optimization for high-QPS
callers. It is deliberately not implemented here — every reservation round-trips to
PostgreSQL, which is the correct and sufficient behavior for this slice. A future
change could intercept ``reserve`` before the transaction below and serve it from a
process-local segment, refilling by calling this same code path.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from sites.broker.db import BrokerDatabase, register_migration

# ``supported`` keyword set intentionally excluded: this module only ever appends
# rows and reads aggregates, so it has no schema-shaped input to validate.


class LedgerError(Exception):
    """Base class for ledger refusals."""


class BudgetExceeded(LedgerError):
    """A reservation would exceed a hard limit.

    ``level`` is one of ``"user"``, ``"org"``, ``"app"``, ``"call"`` — the caller
    (flows.py, tools.py, agent_bridge.py) maps this to the right fallback reason.
    """

    def __init__(self, level: str) -> None:
        super().__init__(f"credit budget exceeded at the {level} level")
        self.level = level


class ReservationNotFound(LedgerError):
    def __init__(self, reservation_id: str) -> None:
        super().__init__(f"no such reservation: {reservation_id}")
        self.reservation_id = reservation_id


@dataclass(frozen=True)
class Reservation:
    id: str
    app_env_id: str
    scope: str
    amount: int
    period: str
    status: str  # "reserved" | "settled" | "released"
    settled_amount: int | None
    response: Any | None
    replayed: bool


def _is_unique_violation(exc: BaseException) -> bool:
    # Same duck-typed check as sites.storage._is_unique_violation: SQLSTATE 23505,
    # read off exc.diag without importing psycopg's exception hierarchy by name.
    diag = getattr(exc, "diag", None)
    return getattr(diag, "sqlstate", None) == "23505"


def _period_for(clock: Callable[[], float]) -> str:
    when = dt.datetime.fromtimestamp(clock(), tz=dt.timezone.utc)
    return when.strftime("%Y-%m")


_MIGRATION_DDL = (
    """
    CREATE TABLE broker_credit_periods (
        app_env_id TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        period TEXT NOT NULL,
        reserved INTEGER NOT NULL DEFAULT 0,
        settled INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (app_env_id, subject_type, subject_id, period)
    )
    """,
    """
    CREATE TABLE broker_credit_reservations (
        id TEXT PRIMARY KEY,
        app_env_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        idempotency_key TEXT,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        amount INTEGER NOT NULL,
        period TEXT NOT NULL,
        status TEXT NOT NULL,
        settled_amount INTEGER,
        response_json TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        settled_at TIMESTAMPTZ
    )
    """,
    # A caller may retry the exact same POST without an idempotency key at all
    # (idempotency_key IS NULL); those rows never collide with one another, only
    # a repeated key within the same app and scope is a replay.
    """
    CREATE UNIQUE INDEX broker_credit_reservations_idem_idx
        ON broker_credit_reservations (app_env_id, scope, idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
    "CREATE INDEX broker_credit_reservations_usage_idx"
    " ON broker_credit_reservations (app_env_id, subject_type, subject_id, status, created_at)",
    """
    CREATE TABLE broker_credit_entries (
        id BIGSERIAL PRIMARY KEY,
        reservation_id TEXT NOT NULL,
        app_env_id TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        period TEXT NOT NULL,
        account TEXT NOT NULL,
        kind TEXT NOT NULL,
        delta INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
)


def _apply_v1(cursor: Any) -> None:
    for statement in _MIGRATION_DDL:
        cursor.execute(statement)


register_migration("runtime", 1, "credit ledger: periods, reservations, entries", _apply_v1)


class Ledger:
    def __init__(self, db: BrokerDatabase, clock: Callable[[], float]) -> None:
        self._db = db
        self._clock = clock

    def period_for(self) -> str:
        return _period_for(self._clock)

    # -- internals -----------------------------------------------------

    @staticmethod
    def _ensure_period_row(cursor: Any, app_env_id: str, subject_type: str, subject_id: str, period: str) -> None:
        cursor.execute(
            "INSERT INTO broker_credit_periods (app_env_id, subject_type, subject_id, period)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (app_env_id, subject_type, subject_id, period),
        )

    @staticmethod
    def _log_entry(cursor: Any, reservation_id: str, app_env_id: str, subject_type: str,
                    subject_id: str, period: str, account: str, kind: str, delta: int) -> None:
        cursor.execute(
            "INSERT INTO broker_credit_entries"
            " (reservation_id, app_env_id, subject_type, subject_id, period, account, kind, delta)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (reservation_id, app_env_id, subject_type, subject_id, period, account, kind, delta),
        )

    @staticmethod
    def _row_to_reservation(row: tuple, *, replayed: bool) -> Reservation:
        (reservation_id, amount, period, status, settled_amount, response_json) = row
        response = json.loads(response_json) if response_json is not None else None
        return Reservation(
            id=reservation_id,
            app_env_id="",  # filled in by the caller when it matters; kept out of the row to stay short
            scope="",
            amount=int(amount),
            period=period,
            status=status,
            settled_amount=None if settled_amount is None else int(settled_amount),
            response=response,
            replayed=replayed,
        )

    def _existing_reservation(self, app_env_id: str, scope: str, idempotency_key: str) -> Reservation:
        with self._db.transaction() as cursor:
            cursor.execute(
                "SELECT id, amount, period, status, settled_amount, response_json"
                " FROM broker_credit_reservations"
                " WHERE app_env_id = %s AND scope = %s AND idempotency_key = %s",
                (app_env_id, scope, idempotency_key),
            )
            row = cursor.fetchone()
        if row is None:
            # The concurrent writer that caused our unique-violation must have
            # committed for the violation to have happened at all; a missing row
            # here would mean it rolled back for an unrelated reason after all.
            raise LedgerError("idempotent reservation vanished after a conflicting insert")
        reservation = self._row_to_reservation(row, replayed=True)
        return Reservation(**{**reservation.__dict__, "app_env_id": app_env_id, "scope": scope})

    # -- public API ------------------------------------------------------

    def reserve(
        self,
        *,
        app_env_id: str,
        scope: str,
        amount: int,
        idempotency_key: str | None = None,
        user_subject: tuple[str, str] | None = None,
        user_limit: int | None = None,
        app_limit: int | None = None,
        call_cap: int | None = None,
    ) -> Reservation:
        """Reserve ``amount`` credits, enforcing every configured hard limit.

        ``user_subject`` is ``("user", <id>)`` or ``("org", <id>)`` — the caller has
        already resolved "per org when the user has one, else per user". Pass
        ``None`` for a ``run_as: app`` call, in which case only the app-level limit
        applies. ``call_cap`` is the flow/agent's own per-call or per-run budget; it
        is a pure comparison, not a persisted bucket.
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        if call_cap is not None and amount > call_cap:
            raise BudgetExceeded("call")

        period = self.period_for()
        app_key = ("app", app_env_id)
        subjects: dict[tuple[str, str], int | None] = {}
        if user_subject is not None:
            subjects[user_subject] = user_limit
        if app_key in subjects:
            existing_limit = subjects[app_key]
            if existing_limit is None:
                subjects[app_key] = app_limit
            elif app_limit is not None:
                subjects[app_key] = min(existing_limit, app_limit)
        else:
            subjects[app_key] = app_limit
        primary_subject = user_subject or app_key

        try:
            with self._db.transaction() as cursor:
                if idempotency_key:
                    cursor.execute(
                        "SELECT id, amount, period, status, settled_amount, response_json"
                        " FROM broker_credit_reservations"
                        " WHERE app_env_id = %s AND scope = %s AND idempotency_key = %s",
                        (app_env_id, scope, idempotency_key),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        reservation = self._row_to_reservation(row, replayed=True)
                        return Reservation(**{**reservation.__dict__, "app_env_id": app_env_id, "scope": scope})

                # Generated before the budget checks below so every entry row can
                # carry it directly. Safe: a BudgetExceeded raised partway through
                # aborts this whole transaction, so an id that never becomes a real
                # reservation never lingers on a committed entry row either.
                reservation_id = "r" + uuid.uuid4().hex
                for (subject_type, subject_id), limit in subjects.items():
                    self._ensure_period_row(cursor, app_env_id, subject_type, subject_id, period)
                    if limit is None:
                        cursor.execute(
                            "UPDATE broker_credit_periods SET reserved = reserved + %s"
                            " WHERE app_env_id = %s AND subject_type = %s AND subject_id = %s AND period = %s",
                            (amount, app_env_id, subject_type, subject_id, period),
                        )
                    else:
                        cursor.execute(
                            "UPDATE broker_credit_periods SET reserved = reserved + %s"
                            " WHERE app_env_id = %s AND subject_type = %s AND subject_id = %s AND period = %s"
                            " AND reserved + settled + %s <= %s",
                            (amount, app_env_id, subject_type, subject_id, period, amount, limit),
                        )
                        if cursor.rowcount == 0:
                            raise BudgetExceeded(subject_type)
                    self._log_entry(cursor, reservation_id, app_env_id, subject_type, subject_id, period,
                                     "available", "reserve", -amount)
                    self._log_entry(cursor, reservation_id, app_env_id, subject_type, subject_id, period,
                                     "reserved", "reserve", amount)

                cursor.execute(
                    "INSERT INTO broker_credit_reservations"
                    " (id, app_env_id, scope, idempotency_key, subject_type, subject_id,"
                    "  amount, period, status)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'reserved')",
                    (reservation_id, app_env_id, scope, idempotency_key,
                     primary_subject[0], primary_subject[1], amount, period),
                )
                return Reservation(
                    id=reservation_id, app_env_id=app_env_id, scope=scope, amount=amount,
                    period=period, status="reserved", settled_amount=None, response=None,
                    replayed=False,
                )
        except Exception as exc:
            if idempotency_key and _is_unique_violation(exc):
                return self._existing_reservation(app_env_id, scope, idempotency_key)
            raise

    def _subjects_for(self, app_env_id: str, subject_type: str, subject_id: str) -> set[tuple[str, str]]:
        return {(subject_type, subject_id), ("app", app_env_id)}

    def settle(self, reservation_id: str, *, app_env_id: str, actual_amount: int,
               response: Any | None = None) -> Reservation:
        """Charge the actual amount, clamped to the reservation, and free the rest.

        Idempotent: settling an already-settled (or released) reservation again is
        a no-op that returns the stored outcome rather than moving credits twice —
        this is what makes replaying an ``Idempotency-Key`` a "no double charge".
        """
        if actual_amount < 0:
            raise ValueError("actual_amount must not be negative")
        with self._db.transaction() as cursor:
            cursor.execute(
                "SELECT scope, subject_type, subject_id, amount, period, status,"
                " settled_amount, response_json"
                " FROM broker_credit_reservations WHERE id = %s AND app_env_id = %s FOR UPDATE",
                (reservation_id, app_env_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ReservationNotFound(reservation_id)
            scope, subject_type, subject_id, amount, period, status, settled_amount, response_json = row
            if status != "reserved":
                return Reservation(
                    id=reservation_id, app_env_id=app_env_id, scope=scope, amount=int(amount),
                    period=period, status=status,
                    settled_amount=None if settled_amount is None else int(settled_amount),
                    response=json.loads(response_json) if response_json is not None else None,
                    replayed=True,
                )

            settled_amt = min(actual_amount, amount)
            excess = actual_amount - settled_amt
            for (subj_type, subj_id) in self._subjects_for(app_env_id, subject_type, subject_id):
                cursor.execute(
                    "UPDATE broker_credit_periods SET reserved = reserved - %s, settled = settled + %s"
                    " WHERE app_env_id = %s AND subject_type = %s AND subject_id = %s AND period = %s",
                    (amount, settled_amt, app_env_id, subj_type, subj_id, period),
                )
                self._log_entry(cursor, reservation_id, app_env_id, subj_type, subj_id, period,
                                 "reserved", "settle", -amount)
                self._log_entry(cursor, reservation_id, app_env_id, subj_type, subj_id, period,
                                 "settled", "settle", settled_amt)
                self._log_entry(cursor, reservation_id, app_env_id, subj_type, subj_id, period,
                                 "available", "settle", amount - settled_amt)
                if excess:
                    self._log_entry(cursor, reservation_id, app_env_id, subj_type, subj_id, period,
                                     "excess", "excess", excess)

            response_json_out = json.dumps(response) if response is not None else None
            cursor.execute(
                "UPDATE broker_credit_reservations"
                " SET status = 'settled', settled_amount = %s, response_json = %s, settled_at = NOW()"
                " WHERE id = %s",
                (settled_amt, response_json_out, reservation_id),
            )
            return Reservation(
                id=reservation_id, app_env_id=app_env_id, scope=scope, amount=int(amount),
                period=period, status="settled", settled_amount=settled_amt, response=response,
                replayed=False,
            )

    def release(self, reservation_id: str, *, app_env_id: str) -> Reservation:
        """Give back a reservation's hold in full (call failed before settling)."""
        with self._db.transaction() as cursor:
            cursor.execute(
                "SELECT scope, subject_type, subject_id, amount, period, status,"
                " settled_amount, response_json"
                " FROM broker_credit_reservations WHERE id = %s AND app_env_id = %s FOR UPDATE",
                (reservation_id, app_env_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ReservationNotFound(reservation_id)
            scope, subject_type, subject_id, amount, period, status, settled_amount, response_json = row
            if status != "reserved":
                return Reservation(
                    id=reservation_id, app_env_id=app_env_id, scope=scope, amount=int(amount),
                    period=period, status=status,
                    settled_amount=None if settled_amount is None else int(settled_amount),
                    response=json.loads(response_json) if response_json is not None else None,
                    replayed=True,
                )
            for (subj_type, subj_id) in self._subjects_for(app_env_id, subject_type, subject_id):
                cursor.execute(
                    "UPDATE broker_credit_periods SET reserved = reserved - %s"
                    " WHERE app_env_id = %s AND subject_type = %s AND subject_id = %s AND period = %s",
                    (amount, app_env_id, subj_type, subj_id, period),
                )
                self._log_entry(cursor, reservation_id, app_env_id, subj_type, subj_id, period,
                                 "reserved", "release", -amount)
                self._log_entry(cursor, reservation_id, app_env_id, subj_type, subj_id, period,
                                 "available", "release", amount)
            cursor.execute(
                "UPDATE broker_credit_reservations SET status = 'released' WHERE id = %s",
                (reservation_id,),
            )
            return Reservation(
                id=reservation_id, app_env_id=app_env_id, scope=scope, amount=int(amount),
                period=period, status="released", settled_amount=None, response=None, replayed=False,
            )

    def usage(self, app_env_id: str, subject_type: str, subject_id: str, period: str) -> int:
        """Settled credits for one subject in one period (the ``GET /usage/me`` number)."""
        with self._db.transaction() as cursor:
            cursor.execute(
                "SELECT settled FROM broker_credit_periods"
                " WHERE app_env_id = %s AND subject_type = %s AND subject_id = %s AND period = %s",
                (app_env_id, subject_type, subject_id, period),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def usage_since(self, app_env_id: str, since: dt.date) -> list[dict]:
        """Per-user settled credits and call counts since ``since`` (admin usage report)."""
        with self._db.transaction() as cursor:
            cursor.execute(
                "SELECT subject_id, COALESCE(SUM(settled_amount), 0), COUNT(*)"
                " FROM broker_credit_reservations"
                " WHERE app_env_id = %s AND subject_type = 'user' AND status = 'settled'"
                " AND created_at >= %s"
                " GROUP BY subject_id ORDER BY subject_id",
                (app_env_id, since),
            )
            rows = cursor.fetchall()
        return [{"userId": r[0], "credits": int(r[1]), "calls": int(r[2])} for r in rows]
