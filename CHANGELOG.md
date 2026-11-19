# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-19

### Added
- Clean-room 6-layer asynchronous MCP server (`stdio` and `streamable-http` with mandatory `BearerAuth` on non-loopback hosts) for `https://api.everesteer.ai`.
- Complete Everesteer API v1 endpoint coverage across 6 cohesive toolsets (`core`, `models`, `compete`, `compute`, `account`, `staking`) plus opt-in `equities`.
- Default-closed 3-gate safety model (`EVERESTEER_ALLOW_WRITES`, `EVERESTEER_ALLOW_STAKING`, `EVERESTEER_ALLOW_WITHDRAWALS`) with restricted `.env` loading, exact integer micro-USDC validation, per-call/daily USDC caps, SQLite write journal, and MCP Elicitation confirmations.
- Presigned CDN/S3 upload & download lanes that keep API keys and Cloudflare Access headers off third-party S3/CloudFront redirects.
- First-class `[langgraph]` integration (`get_everesteer_tools`, `create_everesteer_agent`, `draw_mermaid`) with native `interrupt()` human approvals and `examples/langgraph_app/`.
- CLI (`serve`, `auth`, `status`, `tools`, `diagram`, `logout`) including programmatic Mermaid diagram generation.
- Strict Ruff, strict Pyright, Gitleaks secret scanning, `pip-audit`, and 100% endpoint coverage verification in tests and CI.
