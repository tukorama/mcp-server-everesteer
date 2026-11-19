# Engineering Onboarding

This directory walks you step by step through how `mcp-server-everesteer` works, from the fundamentals of agentic tool calling down to transaction safety, static analysis, and hands-on local labs.

Treat the Python source under [src/mcp_server_everesteer/](../src/mcp_server_everesteer/) as the source of truth. Every concept below maps directly to real modules and tests you can inspect and execute locally without an API key.

## Curriculum

| Module | Topic | Key Files |
|---|---|---|
| [01 — Agentic Foundations](01-agentic-foundations.md) | Why MCP splits Tools, Resources, and Prompts; capability visibility; keeping the model's context focused | `capabilities.py`, `resources.py`, `prompts.py`, `knowledge/` |
| [02 — Control-Plane Architecture](02-control-plane-architecture.md) | The 6-layer dependency graph; tracing a tool call from MCP/LangGraph to HTTP and credential-free S3 storage | `tools.py`, `ops/registry.py`, `client/base.py`, `client/transfer.py` |
| [03 — State, Money, and Safety](03-state-money-and-safety.md) | Exact micro-USDC math, default-closed gates, human confirmations, and the SQLite write journal finite-state machine | `money.py`, `safety.py`, `journal.py`, `config.py` |
| [04 — Hands-On Labs](04-hands-on-labs.md) | Four runnable terminal exercises covering CLI introspection, mocked MCP sessions, journal transitions, and property tests | `cli.py`, `tests/mcp/`, `tests/unit/test_invariants.py` |

## Prerequisites

Install [uv](https://docs.astral.sh/uv/) and sync the workspace development environment once:

```bash
uv sync --all-extras
```

All commands in these modules run offline against local checks and `httpx2.MockTransport` responders unless explicitly noted otherwise.
