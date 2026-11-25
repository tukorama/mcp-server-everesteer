# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Downloads stream into a temporary file next to the target, are fsynced and validated (`PAR1` magic for parquet, a zip header for `split="bulk"`, no HTML or JSON pages), and only then replace the target, so a failed download keeps the previous file. Output paths must resolve, symlinks included, inside `EVERESTEER_DOWNLOAD_DIR` (new, `os.pathsep`-separated, environment only; default: the working directory).
- A Cloudflare Access login redirect on a download raises `CloudflareAccessError` instead of being followed and saved.
- Empty, `.` and `..` path arguments are refused instead of collapsing into another route (for example `revoke_api_key(key_id=".")` became `DELETE /api/v1/agents/keys`).

## [0.1.1] - 2026-11-19

### Added
- Staking v2 operations (`get_staking_strategy`, `get_staking_plans`, `get_staking_positions`, `set_staking_plan` supporting `constant` and `compound` modes) backed by live `/api/v1/staking/*` endpoints.
- Direct `predictions_path` support (`.parquet`, `.csv`, `.json`) and optional `data_datestamp` parameter on `validate_futures_submission`, `submit_futures_predictions`, and `submit_futures_predictions_batch`.
- Hosted compute auto-deploy parameter `deploy_to_model_id` on `train_model` and `from_job_id` on `upload_model`, plus `download_dataset(split="bulk")`.

### Changed
- Removed the static `knowledge/` subpackage so live API resources (`everesteer://capabilities`, `everesteer://scoring`, `everesteer://rounds/current`, `everesteer://models`) and `prompts.py` remain the single source of truth.
- Removed live-API unavailability disclaimers from `README.md` after verification against `https://api.everesteer.ai` and `/himalayas_dataset`.

### Fixed
- Presign `404` model-not-found errors now surface directly as `NotFound` instead of falling back to multipart upload.
- Non-auth HTML error responses (such as `404` or `413`) no longer masquerade as `CloudflareAccessError`.
- Enabled `@pytest.mark.live` tests under `EVERESTEER_LIVE=1` to access local credentials in `tests/conftest.py`.

## [0.1.0] - 2026-11-19

### Added
- Clean-room 6-layer asynchronous MCP server (`stdio` and `streamable-http` with mandatory `BearerAuth` on non-loopback hosts) for `https://api.everesteer.ai`.
- Complete Everesteer API v1 endpoint coverage across 6 cohesive toolsets (`core`, `models`, `compete`, `compute`, `account`, `staking`) plus opt-in `equities`.
- Default-closed 3-gate safety model (`EVERESTEER_ALLOW_WRITES`, `EVERESTEER_ALLOW_STAKING`, `EVERESTEER_ALLOW_WITHDRAWALS`) with restricted `.env` loading, exact integer micro-USDC validation, per-call/daily USDC caps, SQLite write journal, and MCP Elicitation confirmations.
- Presigned CDN/S3 upload & download lanes that keep API keys and Cloudflare Access headers off third-party S3/CloudFront redirects.
- First-class `[langgraph]` integration (`get_everesteer_tools`, `create_everesteer_agent`, `draw_mermaid`) with native `interrupt()` human approvals and `examples/langgraph_app/`.
- CLI (`serve`, `auth`, `status`, `tools`, `diagram`, `logout`) including programmatic Mermaid diagram generation.
- Strict Ruff, strict Pyright, Gitleaks secret scanning, `pip-audit`, and 100% endpoint coverage verification in tests and CI.
