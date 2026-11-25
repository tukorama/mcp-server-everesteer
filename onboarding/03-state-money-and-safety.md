# 03 — State, Money, and Safety

Autonomous agents fail in predictable ways when given write or wallet access: hallucinated precision (`0.1 + 0.2`), retrying a timed-out HTTP POST that actually succeeded on the server, reading malicious `.env` overrides in an untrusted git checkout, or skipping confirmation prompts. Here is how each failure mode is systematically eliminated.

---

## 1. Exact Fixed-Point Money (`money.py`)

Never pass USDC amounts as binary IEEE-754 floats inside financial tools.

- `1 USDC == 1_000_000 micro-USDC` (`MICRO_PER_USDC`).
- [money.py](../src/mcp_server_everesteer/money.py) exposes `parse_usdc_to_micro(raw: str | int | None) -> int`:
  - Rejects `bool`, rejects every `float` (including `nan` and `inf`), rejects values with more than 6 decimal places, and rejects zero/negative numbers **before** any network I/O.
  - Parses decimal strings (`"25.5"`) via `Decimal` into exact integer micro-USDC (`25_500_000`), guaranteeing lossless addition and round-tripping.

---

## 2. Three Default-Closed Gates & Restricted `.env` (`config.py`)

Mutating capability is controlled by three independent boolean flags, all defaulting to `False`:

1. `EVERESTEER_ALLOW_WRITES=true` — model slots, prediction submissions, practice diagnostics, hosted compute launches, account updates.
2. `EVERESTEER_ALLOW_STAKING=true` — staking, per-model stake plans, claims, event allocations.
3. `EVERESTEER_ALLOW_WITHDRAWALS=true` — on-chain USDC withdrawals and destructive/emergency actions (`withdraw_usdc`, `freeze_withdrawals`, `archive_model`, `revoke_api_key`, `delete_diagnostics_run`).

A cloned repository may contain an adversarial `.env` file. `_NOT_FROM_DOTENV` in [config.py](../src/mcp_server_everesteer/config.py) strips secrets, URLs, file paths, limits, bearer tokens, and all three gates from workspace `.env` sources so they can only come from the real process environment.

---

## 3. The Write Journal Finite-State Machine (`journal.py`)

Network drops after a `POST` leaves an ambiguous reality: did the server receive the transaction or not? Blindly retrying risks spending twice.

[journal.py](../src/mcp_server_everesteer/journal.py) persists write intent to an `0600`-protected SQLite database using an explicit transition table (`WRITE_TRANSITIONS`):

```text
  (none) ──record──▶ pending ──HTTP 2xx──▶ sent      (terminal)
                        │
                        ├──NotSent──────▶ failed    (terminal)
                        ├──HTTP 4xx/err─▶ rejected  (terminal)
                        └──timeout/drop─▶ unknown ──clear_unknown──▶ cleared (terminal)
```

Key invariants enforced in SQLite and checked by property-based stateful tests ([tests/unit/test_invariants.py](../tests/unit/test_invariants.py)):

- **Legal transitions only:** `validate_transition()` rejects any jump not present in `WRITE_TRANSITIONS` (`sent -> pending`, `failed -> sent`, etc.). Orphaned `pending` rows older than `STALE_PENDING_SECONDS` (`120 s`) are automatically transitioned to `unknown` when the journal opens.
- **Duplicate lock (`BLOCKING_DUPLICATE_STATES = {"pending", "unknown"}`):** Any new write matching the canonical SHA-256 `fingerprint(...)` of an active `pending` or `unknown` write within `DUPLICATE_TTL_SECONDS` (`15 min`) is refused locally with `Refused`. Once investigated and cleared via `clear_unknown()`, future writes proceed.
- **Withdrawal Idempotency-Keys:** `withdraw_usdc` leaves the key out of its fingerprint and stores it in the journal. An identical withdrawal whose outcome is `unknown` is retried under the stored key so the server can deduplicate it; one that is still `pending` (in flight) is refused like any duplicate, and so are a different key and a repeat of an identical withdrawal that is already `sent` within the window.
- **Daily spend accounting (`ACTIVE_SPEND_STATES = {"pending", "sent", "unknown"}`):** `spent_today(kind)` sums all committed staking and withdrawal entries for the current UTC day. Rejected, failed, and cleared entries do not consume daily quota. Entries that share an Idempotency-Key count once, `relay_stake` counts as the per-call staking limit, and a per-round stake plan counts one round on every later day while it is in force. The cap check and the insert run in one `BEGIN IMMEDIATE` transaction (`Journal.locked()`).

Render the confirmation & journal sequence diagram directly from code:

```bash
uv run mcp-server-everesteer diagram --kind safety --fence
```

---

## 4. Mandatory Out-of-Band Human Confirmation

Operations with financial cost, quota consumption (`submit_event_predictions`), or irreversible effects define a `describe(...)` coroutine in `ops/` returning a server-authored confirmation string sanitized via `clean_label()`:

- **Over MCP (`tools.py`):** uses MCP Elicitation (`Elicit(text, Confirmation)`). Clients without elicitation support are refused unless `EVERESTEER_CONFIRM=none` is set for non-destructive writes.
- **Over LangGraph (`integrations/langgraph.py`):** pauses execution with `interrupt({"type": "everesteer_approval", "operation": op.name, "message": text})`, resuming only on an explicit truthy response via `Command(resume=True)`.
- **`mandatory_confirm=True` (withdrawals & destructive actions):** confirmation **cannot** be bypassed even if `EVERESTEER_CONFIRM=none`.

Next: [04 — Hands-On Labs](04-hands-on-labs.md)
