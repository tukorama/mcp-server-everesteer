"""Finite-state SQLite write journal for money-moving and quota-sensitive writes."""

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

DUPLICATE_TTL_SECONDS = 15 * 60
STALE_PENDING_SECONDS = 120

_now = time.time

WriteState = Literal["pending", "sent", "rejected", "failed", "unknown", "cleared"]

ACTIVE_SPEND_STATES: frozenset[WriteState] = frozenset({"pending", "sent", "unknown"})
BLOCKING_DUPLICATE_STATES: frozenset[WriteState] = frozenset({"pending", "unknown"})

WRITE_TRANSITIONS: dict[WriteState | None, frozenset[WriteState]] = {
    None: frozenset({"pending"}),
    "pending": frozenset({"sent", "rejected", "failed", "unknown"}),
    "unknown": frozenset({"cleared"}),
    "sent": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset(),
    "cleared": frozenset(),
}

_VALID_STATES: frozenset[str] = frozenset(k for k in WRITE_TRANSITIONS if k is not None)

# Columns added after v0.1.1; journals created by older versions gain them when opened.
_ADDED_COLUMNS = {
    "idempotency_key": "TEXT",
    "plan_key": "TEXT",
    "per_round": "INTEGER NOT NULL DEFAULT 0",
}


def validate_transition(current: WriteState | None, target: WriteState) -> WriteState:
    """Enforce the finite-state transition table for recorded write entries."""
    allowed = WRITE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(
            f"Illegal write state transition: {current!r} -> {target!r} "
            + f"(allowed: {sorted(allowed)})."
        )
    return target


@dataclass(frozen=True)
class JournalEntry:
    """One recorded mutating call."""

    id: str
    created: float
    tool: str
    endpoint: str
    summary: str
    usdc: str | None
    status: WriteState
    fingerprint: str | None
    response: str | None
    idempotency_key: str | None = None


class Journal:
    """Tracks in-flight, unknown, and completed USDC writes locally in 0600 SQLite."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not path.exists()
        self.db = sqlite3.connect(path, isolation_level=None)
        if fresh:
            os.chmod(path, 0o600)
        with self.locked():
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS writes (
                    id TEXT PRIMARY KEY,
                    created REAL NOT NULL,
                    tool TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    usdc TEXT,
                    status TEXT NOT NULL,
                    fingerprint TEXT,
                    response TEXT,
                    idempotency_key TEXT,
                    plan_key TEXT,
                    per_round INTEGER NOT NULL DEFAULT 0
                )"""
            )
            present = {str(row[1]) for row in self.db.execute("PRAGMA table_info(writes)")}
            for name, declaration in _ADDED_COLUMNS.items():
                if name not in present:
                    self.db.execute(f"ALTER TABLE writes ADD COLUMN {name} {declaration}")
            # Reap orphaned in-flight rows from crashed processes (`pending -> unknown`).
            self.db.execute(
                "UPDATE writes SET status = 'unknown' WHERE status = 'pending' AND created < ?",
                (_now() - STALE_PENDING_SECONDS,),
            )

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def locked(self) -> Generator[None, None, None]:
        """Hold SQLite's write lock so a check-then-record sequence is atomic across processes."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def record(
        self,
        *,
        tool: str,
        endpoint: str,
        summary: str,
        usdc: Decimal | None,
        fingerprint: str | None,
        idempotency_key: str | None = None,
        plan_key: str | None = None,
        per_round: bool = False,
    ) -> str:
        if usdc is not None and usdc <= 0:
            raise ValueError(f"Recorded USDC amount must be strictly positive, got {usdc}.")
        if per_round and (usdc is None or plan_key is None):
            raise ValueError("A per-round stake plan needs an amount and a plan key.")
        initial = validate_transition(None, "pending")
        entry_id = uuid.uuid4().hex[:16]
        self.db.execute(
            "INSERT INTO writes (id, created, tool, endpoint, summary, usdc, status, "
            + "fingerprint, response, idempotency_key, plan_key, per_round) "
            + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                entry_id,
                _now(),
                tool,
                endpoint,
                summary,
                None if usdc is None else format(usdc, "f"),
                initial,
                fingerprint,
                idempotency_key,
                plan_key,
                int(per_round),
            ),
        )
        return entry_id

    def finish(self, entry_id: str, status: WriteState, response: Any = None) -> None:
        current = self.get(entry_id)
        if current is None:
            raise KeyError(f"Unknown journal entry: {entry_id}")
        next_state = validate_transition(current.status, status)
        self.db.execute(
            "UPDATE writes SET status = ?, response = ? WHERE id = ?",
            (next_state, _serialize_response(response), entry_id),
        )

    def clear_unknown(self, entry_id: str, response: Any = None) -> None:
        """Mark an investigated `unknown` entry as reconciled/cleared so future writes can run."""
        self.finish(entry_id, "cleared", response=response)

    def get(self, entry_id: str) -> JournalEntry | None:
        row = self.db.execute(
            """SELECT id, created, tool, endpoint, summary, usdc, status, fingerprint, response,
            idempotency_key FROM writes WHERE id = ?""",
            (entry_id,),
        ).fetchone()
        return _row_to_entry(row) if row else None

    def unknown(self, limit: int = 20) -> list[JournalEntry]:
        rows = self.db.execute(
            """SELECT id, created, tool, endpoint, summary, usdc, status, fingerprint, response,
            idempotency_key FROM writes WHERE status = 'unknown' ORDER BY created DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def latest_duplicate(
        self, fingerprint: str, states: frozenset[WriteState]
    ) -> JournalEntry | None:
        """Return the newest identical write in one of `states` within the duplicate window."""
        rows = self.db.execute(
            """SELECT id, created, tool, endpoint, summary, usdc, status, fingerprint, response,
            idempotency_key FROM writes WHERE fingerprint = ? AND created >= ?
            ORDER BY created DESC""",
            (fingerprint, _now() - DUPLICATE_TTL_SECONDS),
        ).fetchall()
        entries = (_row_to_entry(row) for row in rows)
        return next((entry for entry in entries if entry.status in states), None)

    def pending_duplicate(self, fingerprint: str) -> JournalEntry | None:
        """Return a recent in-flight or unknown identical write that blocks duplicates."""
        return self.latest_duplicate(fingerprint, BLOCKING_DUPLICATE_STATES)

    def spent_today(self, kind: str) -> Decimal:
        """Total USDC committed today (UTC) for `kind` ('staking' or 'withdrawals')."""
        return self.projected_spend(kind)

    def projected_spend(
        self, kind: str, amount: Decimal | None = None, idempotency_key: str | None = None
    ) -> Decimal:
        """Today's (UTC) total for `kind`, including a prospective write of `amount`.

        Writes sharing an Idempotency-Key are one movement and count once. A per-round stake
        plan also counts one round on every later day that starts while it is still in force,
        i.e. until a newer plan write for the same model is confirmed as sent.
        """
        start = datetime.fromtimestamp(_now(), UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        midnight = start.timestamp()
        states = tuple(sorted(ACTIVE_SPEND_STATES))
        today = self.db.execute(
            """SELECT usdc, idempotency_key FROM writes
            WHERE created >= ? AND tool LIKE ? AND usdc IS NOT NULL AND status IN (?, ?, ?)""",
            (midnight, f"{kind}:%", *states),
        ).fetchall()
        standing = self.db.execute(
            """SELECT plan.usdc, NULL FROM writes AS plan
            WHERE plan.per_round = 1 AND plan.created < ? AND plan.tool LIKE ?
            AND plan.status IN (?, ?, ?) AND NOT EXISTS (
                SELECT 1 FROM writes AS later WHERE later.plan_key = plan.plan_key
                AND later.created > plan.created AND later.created < ?
                AND later.status = 'sent'
            )""",
            (midnight, f"{kind}:%", *states, midnight),
        ).fetchall()
        prospective = [] if amount is None else [(format(amount, "f"), idempotency_key)]
        return _total([*today, *standing, *prospective])


def _total(amounts: Iterable[tuple[Any, Any]]) -> Decimal:
    total = Decimal(0)
    keyed: dict[str, Decimal] = {}
    for raw, key in amounts:
        amount = Decimal(str(raw))
        if key is None:
            total += amount
        else:
            keyed[str(key)] = max(keyed.get(str(key), amount), amount)
    return total + sum(keyed.values(), Decimal(0))


def _row_to_entry(row: tuple[Any, ...]) -> JournalEntry:
    raw_status = str(row[6])
    if raw_status not in _VALID_STATES:
        raise ValueError(f"Corrupt journal status in database: {raw_status!r}")
    return JournalEntry(
        id=str(row[0]),
        created=float(row[1]),
        tool=str(row[2]),
        endpoint=str(row[3]),
        summary=str(row[4]),
        usdc=str(row[5]) if row[5] is not None else None,
        status=cast(WriteState, raw_status),
        fingerprint=str(row[7]) if row[7] is not None else None,
        response=str(row[8]) if row[8] is not None else None,
        idempotency_key=str(row[9]) if row[9] is not None else None,
    )


def _serialize_response(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value)[:4000]
    except (TypeError, ValueError):
        return str(value)[:4000]
