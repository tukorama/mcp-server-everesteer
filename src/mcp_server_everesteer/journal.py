"""Finite-state SQLite write journal for money-moving and quota-sensitive writes."""

import json
import os
import sqlite3
import time
import uuid
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


class Journal:
    """Tracks in-flight, unknown, and completed USDC writes locally in 0600 SQLite."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not path.exists()
        self.db = sqlite3.connect(path, isolation_level=None)
        if fresh:
            os.chmod(path, 0o600)
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
                response TEXT
            )"""
        )
        # Reap orphaned in-flight rows from crashed processes (`pending -> unknown`).
        self.db.execute(
            "UPDATE writes SET status = 'unknown' WHERE status = 'pending' AND created < ?",
            (_now() - STALE_PENDING_SECONDS,),
        )

    def close(self) -> None:
        self.db.close()

    def record(
        self,
        *,
        tool: str,
        endpoint: str,
        summary: str,
        usdc: Decimal | None,
        fingerprint: str | None,
    ) -> str:
        if usdc is not None and usdc <= 0:
            raise ValueError(f"Recorded USDC amount must be strictly positive, got {usdc}.")
        initial = validate_transition(None, "pending")
        entry_id = uuid.uuid4().hex[:16]
        self.db.execute(
            "INSERT INTO writes "
            + "(id, created, tool, endpoint, summary, usdc, status, fingerprint, response) "
            + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                entry_id,
                _now(),
                tool,
                endpoint,
                summary,
                None if usdc is None else format(usdc, "f"),
                initial,
                fingerprint,
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
            "SELECT id, created, tool, endpoint, summary, usdc, status, fingerprint, response "
            + "FROM writes WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return _row_to_entry(row) if row else None

    def unknown(self, limit: int = 20) -> list[JournalEntry]:
        rows = self.db.execute(
            "SELECT id, created, tool, endpoint, summary, usdc, status, fingerprint, response "
            + "FROM writes WHERE status = 'unknown' ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def pending_duplicate(self, fingerprint: str) -> JournalEntry | None:
        """Return a recent in-flight or unknown identical write that blocks duplicates."""
        states = tuple(sorted(BLOCKING_DUPLICATE_STATES))
        row = self.db.execute(
            "SELECT id, created, tool, endpoint, summary, usdc, status, fingerprint, response "
            + "FROM writes WHERE fingerprint = ? AND status IN (?, ?) AND created >= ? "
            + "ORDER BY created DESC LIMIT 1",
            (fingerprint, *states, _now() - DUPLICATE_TTL_SECONDS),
        ).fetchone()
        return _row_to_entry(row) if row else None

    def spent_today(self, kind: str) -> Decimal:
        """Total USDC committed today (UTC) for `kind` ('staking' or 'withdrawals')."""
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        states = tuple(sorted(ACTIVE_SPEND_STATES))
        rows = self.db.execute(
            """SELECT usdc FROM writes
            WHERE created >= ? AND tool LIKE ? AND usdc IS NOT NULL AND status IN (?, ?, ?)""",
            (midnight.timestamp(), f"{kind}:%", *states),
        ).fetchall()
        total = Decimal(0)
        for (raw,) in rows:
            total += Decimal(str(raw))
        return total


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
    )


def _serialize_response(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value)[:4000]
    except (TypeError, ValueError):
        return str(value)[:4000]
