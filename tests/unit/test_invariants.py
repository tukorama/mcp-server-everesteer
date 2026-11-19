"""Property-based & stateful invariant tests for money math, redaction, and the write journal."""

import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from mcp_server_everesteer.journal import (
    ACTIVE_SPEND_STATES,
    BLOCKING_DUPLICATE_STATES,
    WRITE_TRANSITIONS,
    Journal,
    WriteState,
)
from mcp_server_everesteer.money import format_micro_usdc, micro_to_decimal, parse_usdc_to_micro
from mcp_server_everesteer.redaction import clean_label, clean_text, scrub

_FORBIDDEN_CHARS = re.compile(
    r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]"
)
_FINISH_TARGETS: tuple[WriteState, ...] = ("sent", "rejected", "failed", "unknown", "cleared")
_KINDS: tuple[str, ...] = ("staking", "withdrawals")
_FINGERPRINTS: tuple[str, ...] = ("fp-alpha", "fp-beta", "fp-gamma")


@given(
    a=st.integers(min_value=1, max_value=10**15),
    b=st.integers(min_value=1, max_value=10**15),
)
def test_micro_usdc_roundtrip_and_homomorphism(a: int, b: int) -> None:
    assert parse_usdc_to_micro(format_micro_usdc(a)) == a
    assert micro_to_decimal(a) + micro_to_decimal(b) == micro_to_decimal(a + b)
    assert micro_to_decimal(a) * Decimal(1_000_000) == Decimal(a)


@given(value=st.floats())
def test_micro_usdc_rejects_all_floats(value: float) -> None:
    raw: Any = value
    with pytest.raises(ValueError, match="USDC"):
        parse_usdc_to_micro(raw)


@given(raw=st.text(), suffix=st.from_regex(r"[A-Za-z0-9_-]{8,32}", fullmatch=True))
def test_redaction_and_text_sanitization_invariants(raw: str, suffix: str) -> None:
    cleaned = clean_text(raw)
    assert cleaned is not None
    assert not _FORBIDDEN_CHARS.search(cleaned)
    label = clean_label(raw)
    assert label is not None
    assert not _FORBIDDEN_CHARS.search(label)
    assert len(label) <= 200

    api_key = f"eiq_{suffix}"
    webhook_secret = f"whsec_{suffix}"
    assert api_key not in scrub(f"X-API-Key: {api_key}")
    assert webhook_secret not in scrub(f"secret={webhook_secret}")


class WriteJournalStateMachine(RuleBasedStateMachine):
    """Verify state machine transitions, duplicate locks, and daily caps over SQLite."""

    def __init__(self) -> None:
        super().__init__()
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._journal: Journal | None = None
        self.model: dict[str, tuple[WriteState, str, Decimal, str]] = {}

    @initialize()
    def setup_journal(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._journal = Journal(Path(self._tmpdir.name) / "journal.sqlite3")
        self.model = {}

    def teardown(self) -> None:
        if self._journal is not None:
            self._journal.close()
            self._journal = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    def _db(self) -> Journal:
        assert self._journal is not None
        return self._journal

    @rule(
        kind=st.sampled_from(_KINDS),
        micro=st.integers(min_value=1, max_value=500_000_000),
        fp=st.sampled_from(_FINGERPRINTS),
    )
    def record_write(self, kind: str, micro: int, fp: str) -> None:
        amount = micro_to_decimal(micro)
        entry_id = self._db().record(
            tool=f"{kind}:op",
            endpoint="POST /api/v1/staking/stake",
            summary=f"{kind} {amount} {fp}",
            usdc=amount,
            fingerprint=fp,
        )
        self.model[entry_id] = ("pending", kind, amount, fp)

    @rule(data=st.data(), next_state=st.sampled_from(_FINISH_TARGETS))
    def attempt_transition(self, data: st.DataObject, next_state: WriteState) -> None:
        if not self.model:
            return
        entry_id = data.draw(st.sampled_from(sorted(self.model)))
        current, kind, amount, fp = self.model[entry_id]
        allowed = WRITE_TRANSITIONS[current]
        if next_state in allowed:
            if next_state == "cleared":
                self._db().clear_unknown(entry_id)
            else:
                self._db().finish(entry_id, next_state)
            self.model[entry_id] = (next_state, kind, amount, fp)
        else:
            with pytest.raises(ValueError, match="Illegal write state transition"):
                self._db().finish(entry_id, next_state)
        stored = self._db().get(entry_id)
        assert stored is not None
        assert stored.status == self.model[entry_id][0]

    @invariant()
    def check_state_machine_invariants(self) -> None:
        if self._journal is None:
            return
        for kind in _KINDS:
            expected_spend = sum(
                (
                    amount
                    for state, k, amount, _ in self.model.values()
                    if k == kind and state in ACTIVE_SPEND_STATES
                ),
                Decimal(0),
            )
            assert self._journal.spent_today(kind) == expected_spend

        for fp in _FINGERPRINTS:
            should_block = any(
                state in BLOCKING_DUPLICATE_STATES and entry_fp == fp
                for state, _, _, entry_fp in self.model.values()
            )
            dup = self._journal.pending_duplicate(fp)
            assert (dup is not None) == should_block

        unknown_rows = {row.id for row in self._journal.unknown(limit=500)}
        expected_unknown = {
            eid for eid, (st_val, _, _, _) in self.model.items() if st_val == "unknown"
        }
        assert unknown_rows == expected_unknown


TestWriteJournalInvariants = WriteJournalStateMachine.TestCase
