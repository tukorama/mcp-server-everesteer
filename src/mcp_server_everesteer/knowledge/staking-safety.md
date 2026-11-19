# Safety Gates, Micro-USDC, and Staking Guardrails

## Three Default-Closed Safety Gates

All mutating and financial operations are disabled by default and **cannot** be enabled from a project `.env` file. Set them in your MCP client config or shell environment:

1. `EVERESTEER_ALLOW_WRITES=true`: Enables operational writes (creating/updating/uploading models, submitting predictions/diagnostics, launching/cancelling compute jobs, renaming agent/model).
2. `EVERESTEER_ALLOW_STAKING=true`: Exposes and enables staking writes (`stake_on_model`, `confirm_stake`, `unstake_from_model`, `claim_payout`, `relay_stake`, `relay_claim`, `build_vault_config_tx`, `set_stake_allocation`, `withdraw_stake_allocation`).
3. `EVERESTEER_ALLOW_WITHDRAWALS=true`: Enables `withdraw_usdc` and destructive/emergency tools (`freeze_withdrawals`, `archive_model`, `revoke_api_key`, `delete_diagnostics_run`).

## Exact Money Rules

- Every USDC amount on the platform settles as an **integer number of micro-USDC** (`1 USDC = 1_000_000 micro-USDC`).
- **Binary floats are refused** on staking and withdrawal calls. Always pass `amount_micro` (int) or `amount_usdc` as a decimal string (`"25.50"`).
- Per-call and UTC daily spend caps are enforced locally against `EVERESTEER_MAX_STAKE_USDC`, `EVERESTEER_MAX_WITHDRAW_USDC`, and `EVERESTEER_MAX_DAILY_USDC` using a local SQLite write journal (`journal.sqlite3`).
- Every write with an ambiguous network failure (`status="unknown"`) blocks identical duplicate submissions for 15 minutes until you verify account state via `get_wallet_status` or `list_unconfirmed_writes`.

## Withdrawals, Cooling & Emergency Freeze

- All withdrawal and destructive operations require **mandatory interactive human confirmation** (`Elicit` in MCP or `interrupt()` in LangGraph), even if `EVERESTEER_CONFIRM=none`.
- Changing a withdrawal destination triggers a 24-hour cooling window (`get_wallet_status` reports `cooling_until`).
- Staking, payout, and withdrawal endpoints require a verified email (`email_verified_at` in `get_profile`; call `resend_email_verification` if blocked).
- **`freeze_withdrawals`** freezes all withdrawals AND immediately revokes **every API key** on the account (including the calling key). Unfreezing requires signing in via browser on Everesteer's Security page.
