"""API v1 route & legacy SDK exclusion manifest checked by tests/test_coverage.py."""

# Active API v1 endpoints superseded by newer v1 routes (deliberately not exposed as tools).
SUPERSEDED = {
    "POST /api/v1/futures/submit": (
        "Legacy per-target submission format superseded by POST /api/v1/futures/submit/v2"
    ),
    "GET /api/v1/staking/deposit-address": (
        "Retired CREATE2 forwarder path (410 Gone); superseded by GET /api/v1/staking/strategy"
    ),
    "GET /api/v1/staking/forwarder-balance": (
        "Retired CREATE2 forwarder path (410 Gone); superseded by GET /api/v1/staking/strategy"
    ),
    "GET /api/v1/staking/vault": (
        "Retired vault path (410 forwarder_path_retired); superseded by staking strategy"
    ),
    "POST /api/v1/staking/vault/config-tx": (
        "Superseded by per-model stake plans (PUT /api/v1/staking/plan/{model_id})"
    ),
    "PUT /api/v1/models/{model_id}/auto-stake": (
        "Moved (410) to PUT /api/v1/staking/plan/{model_id}"
    ),
    "DELETE /api/v1/models/{model_id}": (
        "Alias for POST /api/v1/models/{model_id}/archive (archive_model)"
    ),
}

# Routes deliberately excluded from the MCP/LangGraph control-plane surface.
WAIVED: dict[str, str] = {}

# Known non-control-plane modules from everesteer-api 0.5.0 intentionally excluded by design.
EXCLUDED_SDK_FEATURES = {
    "everestapi.plots.plot_fit_curve": (
        "Visualization (plotnine/matplotlib) belongs in the caller/agent execution layer."
    ),
    "EverestAPI.format_diagnostics_table": (
        "Console ASCII formatting belongs in UI/presentation, not structured MCP JSON."
    ),
    "EverestAPI.format_leaderboard_table": (
        "Console ASCII formatting belongs in UI/presentation, not structured MCP JSON."
    ),
    "EverestAPI.evaluate": (
        "Local numpy/pandas/scipy evaluation belongs in the agent's Python workspace."
    ),
    "everestapi.scoring": (
        "Local numpy/pandas/scipy scoring engine; live rules come from explain_scoring."
    ),
    "EverestAPI.quick_train": "Deprecated alias for POST /api/v1/compute/train.",
    "EverestAPI.custom_train": "Removed server-side (410); replaced by train(model='custom').",
    "EverestAPI.submit_futures_predictions_legacy": "Deprecated v1 submission format.",
}
