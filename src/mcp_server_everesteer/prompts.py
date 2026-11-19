"""Reusable MCP prompts guiding agents through common Everesteer workflows."""

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server_everesteer.capabilities import Capabilities


def register_prompts(server: MCPServer, caps: Capabilities) -> None:
    """Register workflow prompts tailored to active capabilities."""

    @server.prompt(title="Orient and plan next Everesteer steps")
    def orient_and_plan() -> str:
        """Inspect your account scope, models, round clock, and scoring rules without writing."""
        return (
            "Orient on the Everesteer platform (read-only):\n\n"
            "1. Call `whoami` and `get_started` to confirm authentication, scope "
            "(hackathon vs full), and active gates.\n"
            "2. Call `get_status` and `get_rounds(current_only=True)` to inspect registered "
            "models and the open round clock.\n"
            "3. Call `explain_scoring` and `get_dataset_schema` to record "
            "`score_multiple_constant`, `primary_target`, `feature_encoding`, and `-1` missing "
            "semantics.\n"
            "4. Summarize the recommended workflow without submitting or staking."
        )

    @server.prompt(title="Validate and prepare a submission")
    def validate_and_submit(
        model_id: Annotated[str, Field(description="Your target model UUID or name.")],
        lane: Annotated[
            str,
            Field(
                description="'manual_futures', 'auto_pkl', 'validation_practice', or 'event_round'."
            ),
        ] = "manual_futures",
    ) -> str:
        """Checklist for validating predictions or a .pkl callable before submitting."""
        return (
            f"Prepare and validate submission for model '{model_id}' on lane '{lane}':\n\n"
            "1. Read `everesteer://docs/submission-contract` and verify finite values in [0, 1].\n"
            "2. For `manual_futures`, call `validate_futures_submission` first; fix any "
            "missing/invalid instruments before `submit_futures_predictions`.\n"
            "3. For `auto_pkl`, confirm your `.pkl` implements `predict(live_features)` returning "
            "a single-column DataFrame, call `upload_model`, poll `get_model_upload_status`, "
            "then enable `set_auto_submit(enabled=True)` and verify `lane_active` via "
            "`list_models`.\n"
            "4. For hackathon lanes, confirm `split='validation'` goes to "
            "`submit_validation_diagnostics` (free) and `split='live'` goes to "
            "`submit_event_predictions` (paid pool)."
        )

    @server.prompt(title="Diagnose model performance")
    def diagnose_model(
        model_id: Annotated[str, Field(description="Model UUID or name to analyze.")],
    ) -> str:
        """Analyze FIT20, UNQ20, INOV, T+1..T+20 progression, and leaderboard standing."""
        return (
            f"Diagnose Everesteer model '{model_id}' (read-only):\n\n"
            "1. Call `explain_scoring` for current scoring weights and `b`.\n"
            f"2. Call `get_validation_panel(model_id='{model_id}')`, "
            f"`get_scores(model_id='{model_id}', response_format='detailed')`, and "
            f"`get_diagnostics(target_id='{model_id}', scope='rounds')`.\n"
            "3. Compare against `get_benchmarks` and `get_diagnostics_leaderboard` / "
            "`get_leaderboard`.\n"
            "4. Recommend concrete modeling adjustments (feature exposure, uniqueness vs "
            "benchmark, missing `-1` handling) without mutating account state."
        )

    if "staking" in caps.toolsets:

        @server.prompt(title="Review staking and wallet safety")
        def review_staking() -> str:
            """Audit forwarder/vault balances, 24h cooling status, and unconfirmed writes."""
            return (
                "Review Everesteer staking & wallet status (read-only):\n\n"
                "1. Call `get_profile` to verify `email_verified_at` is set.\n"
                "2. Call `get_wallet_status` to check your verified destination address, "
                "`cooling_until`, and whether withdrawals are frozen.\n"
                "3. Call `get_staking_vault`, `get_deposit_address`, `get_forwarder_balance`, "
                "and `list_unconfirmed_writes`.\n"
                "4. Report spendable balances, active allocations/locks, and any cooling or "
                "verification blockers before proposing any stake or withdrawal."
            )
