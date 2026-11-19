# 04 — Hands-On Labs (No API Key Needed)

Run every exercise below directly from the root of the repository to build hands-on intuition for how the control plane behaves at runtime.

---

## Lab 1 — Inspect Dynamic Capability Filtering & Live Diagrams

Compare how the exposed tool surface changes as you open environment flags (even without real credentials):

```bash
# Default unauthenticated server (4 safe orientation tools):
uv run mcp-server-everesteer tools

# Simulate an API key + writes + staking enabled:
EVERESTEER_API_KEY=eiq_test_fixture_lab_key_001 \
EVERESTEER_ALLOW_WRITES=true \
EVERESTEER_ALLOW_STAKING=true \
  uv run mcp-server-everesteer tools

# Render the current exposed tools as a color-coded Mermaid diagram:
EVERESTEER_API_KEY=eiq_test_fixture_lab_key_001 \
  uv run mcp-server-everesteer diagram --kind tools --fence
```

Notice how `tools` lists both active tools and exact remediation hints for every hidden group.

---

## Lab 2 — Enforce Strict Endpoint Coverage & Architectural Layering

This repository verifies two global invariants automatically in tests:

1. Every HTTP route constant in `client/endpoints.py` (`106` total routes) is exercised by tests under `EVERESTEER_TEST_COVERAGE=strict`.
2. Every module import inside `src/mcp_server_everesteer/` respects the 6-layer DAG checked by AST traversal.

Run both checks now:

```bash
EVERESTEER_TEST_COVERAGE=strict uv run pytest tests/test_coverage.py -v
```

Try running the entire mock test suite alongside strict type checking and linting:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
EVERESTEER_TEST_COVERAGE=strict uv run pytest
```

---

## Lab 3 — Trigger Duplicate Blocking and State Transitions in `Journal`

Open an ephemeral SQLite write journal in Python to observe the state machine enforcing legal transitions and duplicate protection:

```bash
uv run python -c "
import tempfile
from decimal import Decimal
from pathlib import Path
from mcp_server_everesteer.journal import Journal

with tempfile.TemporaryDirectory() as tmp:
    j = Journal(Path(tmp) / 'journal.sqlite3')
    eid = j.record(
        tool='staking:stake_on_model',
        endpoint='POST /api/v1/staking/stake',
        summary='stake 25.00 USDC',
        usdc=Decimal('25.00'),
        fingerprint='lab-fp-1',
    )
    j.finish(eid, 'unknown', {'error': 'simulated timeout'})
    print('Duplicate blocked:', j.pending_duplicate('lab-fp-1').status)
    print('Daily spent (unknown counts):', j.spent_today('staking'))
    j.clear_unknown(eid, {'reconciled': 'not sent on chain'})
    print('After clear_unknown duplicate:', j.pending_duplicate('lab-fp-1'))
    print('After clear_unknown daily spent:', j.spent_today('staking'))
    j.close()
"
```

---

## Lab 4 — Run Stateful Property-Based Invariant Checks

Hypothesis explores random sequences of writes, valid/illegal transitions, IEEE-754 floats, and malicious Unicode inputs to prove our invariants hold across edge cases:

```bash
uv run pytest tests/unit/test_invariants.py -v
```

Once you obtain an Everesteer API key, you can verify read-only live connectivity before enabling any write gate:

```bash
uvx mcp-server-everesteer auth
EVERESTEER_LIVE=1 uv run pytest -m live -v
```
