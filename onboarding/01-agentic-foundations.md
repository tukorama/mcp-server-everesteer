# 01 — Agentic Foundations

If you are coming from traditional REST APIs or classic SDKs, building for an AI agent requires a different mental model. An LLM does not read a 3,000-line Python client module; instead, it sees **tool schemas**, **read-only resources**, **structured errors**, and **runtime guardrails**.

---

## 1. Control Plane vs. Data-Science Workspace

A human SDK bundles plotting (`matplotlib`, `plotnine`), console tables, dataframe math (`pandas`, `numpy`), and HTTP requests together. When an AI coding agent uses that SDK inside MCP, those dependencies waste startup time, pollute tool definitions, and trap long runs inside synchronous `time.sleep` loops.

`mcp-server-everesteer` separates responsibilities cleanly:

- **Control plane (this package):** handles credentials, discovery, downloads, job submission, polling, structured responses, and human approvals over async `httpx2`.
- **Modeling workspace (the user's or agent's local Python environment):** loads downloaded `.parquet` files, runs cross-validation, plots curves, and exports `.pkl` prediction callables.

---

## 2. The Three MCP Primitives

When an MCP host (such as Claude Code, Cursor, Claude Desktop, or VS Code) connects over `stdio` or `streamable-http`, the server exposes three distinct primitives rather than cramming everything into tool descriptions:

| Primitive | Who Triggers It | Purpose in This Repo | Source File |
|---|---|---|---|
| **Tools** | The LLM | Executable actions with typed Pydantic input schemas and `ToolAnnotations` (`read_only_hint`, `destructive_hint`, `idempotent_hint`) | [tools.py](../src/mcp_server_everesteer/tools.py) |
| **Resources** | Application or LLM read | Live read-only JSON snapshots (`everesteer://scoring`, `everesteer://capabilities`, `everesteer://rounds/current`, `everesteer://models`) | [resources.py](../src/mcp_server_everesteer/resources.py) |
| **Prompts** | Human user / client workflow | Reusable multi-step audit checklists (`orient_and_plan`, `validate_and_submit`, `diagnose_model`, `review_staking`) | [prompts.py](../src/mcp_server_everesteer/prompts.py) |

Keeping multi-page rules in live `everesteer://*` resources and concise prompts instead of repeating them inside every tool docstring keeps the model's context budget sharp and focused.

---

## 3. Capability Detection & Progressive Exposure

Showing all **91** operations to an unauthenticated agent wastes tokens and tempts the model into calling operations that will immediately fail. Instead, [capabilities.py](../src/mcp_server_everesteer/capabilities.py) inspects credentials and three boolean gates at server startup and filters which tools are registered:

- **No API key configured:** exposes only **4** safe public/diagnostic tools (`get_health`, `get_capabilities`, `explain_scoring`, `whoami`).
- **API key configured, default gates closed:** exposes **45** read-only and dry-run tools across `core`, `models`, `compete`, `compute`, and `account`.
- **Gates explicitly enabled (`EVERESTEER_ALLOW_WRITES`, `EVERESTEER_ALLOW_STAKING`, `EVERESTEER_ALLOW_WITHDRAWALS`) + `+equities`:** unlocks up to all **91** operations.

You can inspect what is visible vs. hidden at any time via CLI:

```bash
uv run mcp-server-everesteer tools
```

---

## 4. Dual Surface: MCP & In-Process LangGraph

Rather than maintaining separate tool definitions for MCP and Python agents, every operation is written once as a transport-agnostic `@operation` in `src/mcp_server_everesteer/ops/` returning typed Pydantic models (`ApiPayload`, `WriteResult`, `DownloadResult`, etc.):

- [tools.py](../src/mcp_server_everesteer/tools.py) compiles each `Operation` into an MCP tool with MCP Elicitation confirmations.
- [integrations/langgraph.py](../src/mcp_server_everesteer/integrations/langgraph.py) compiles the exact same `Operation` list into LangChain `StructuredTool` objects backed by an in-process thread-isolated event-loop bridge and LangGraph `interrupt()` pauses.

---

## 5. Designing Outputs and Errors for Recovery

An autonomous agent cannot fix a failure if it receives an opaque HTTP `403` stack trace:

- **No `null` bloat:** `CompactModel` in [models.py](../src/mcp_server_everesteer/models.py) drops `None` fields at serialization time.
- **Forward compatibility:** `extra="allow"` preserves unexpected API fields, while `sanitize_payload()` cleans control/bidirectional Unicode characters and normalizes aliases (`bmc` → `unq`, `fnc` → `inov`).
- **Actionable errors:** Every custom exception in [errors.py](../src/mcp_server_everesteer/errors.py) exposes `.for_agent()` appending `Next step: ...` so the LLM knows whether to check email verification, omit `"current"` as an `exped`, switch to a dry run, or ask the user before retrying.
- **Bounded waiting:** When clients such as Claude Desktop enforce ~60 s stdio timeouts (`_wait_cap` in `tools.py`), long waits stop cleanly at **45 s** and return `status="pending"` plus exact next-step instructions (`get_job_status` / `get_diagnostics_run`).

Next: [02 — Control-Plane Architecture](02-control-plane-architecture.md)
