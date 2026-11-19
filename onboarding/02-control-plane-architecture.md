# 02 — Control-Plane Architecture

The package is structured as a strict directed acyclic graph (DAG) of 6 layers. Lower layers never import higher layers — an invariant verified mechanically on every test run via AST inspection in [tests/test_coverage.py](../tests/test_coverage.py) (`test_strict_architectural_layer_dag`).

---

## 1. The 6-Layer Dependency Graph

| Layer | Modules | Responsibility |
|---|---|---|
| **0 — Foundation** | `errors.py`, `money.py`, `redaction.py`, `logging_setup.py`, `coverage.py` | Domain exceptions, micro-USDC fixed-point math, secret/bidi scrubbing, endpoint coverage manifest |
| **1 — State & Config** | `config.py`, `journal.py`, `capabilities.py`, `models.py` | Restricted `.env` loading, SQLite finite-state write journal, gate visibility rules, Pydantic output models |
| **2 — Transport & Safety** | `client/`, `safety.py` | Endpoint registry (`106` routes), GET-only retry policy, presigned CDN isolation, USDC caps & duplicate locks |
| **3 — Operations** | `ops/` (`registry.py`, `core.py`, `models_api.py`, `compete.py`, `compute.py`, `account.py`, `staking.py`, `equities.py`) | `91` transport-agnostic `@operation` functions + shared bounded `Call.poll_until` engine |
| **4 — Prompts & Presentation** | `diagrams.py`, `prompts.py`, `resources.py` | System guide, Mermaid generators, MCP Prompts and live Resources |
| **5 — Delivery Surfaces** | `tools.py`, `server.py`, `http_app.py`, `cli.py`, `integrations/langgraph.py` | MCP stdio / Streamable HTTP (+ `BearerAuth`), CLI, and in-process LangGraph tools |

Generate the live system diagram anytime straight from code:

```bash
uv run mcp-server-everesteer diagram --kind architecture --fence
```

---

## 2. Tracing One Tool Call End-to-End

Follow what happens when an agent calls `download_job_artifact` or `stake_on_model`:

1. **Client invocation (`tools.py` or `integrations/langgraph.py`)**
   - Pydantic validates arguments directly against the Python type annotations on the `@operation` function.
   - If the operation defines a `describe` hook, the confirmation resolver intercepts *before* the tool executes. The LLM cannot forge an approval parameter because `confirmation` is hidden from the tool input schema (`Annotated[ElicitationResult[...], Resolve(...)]` in MCP, or `interrupt()` in LangGraph).
2. **Execution Context (`Call` in `ops/registry.py`)**
   - Attaches a shared `Runtime` (holding `Settings`, `EveresteerClient`, per-account `JsonCache`, and lazy `Journal`), a monotonic progress reporter, and any client timeout budget (`wait_cap`).
   - Long-running operations (`submit_validation_diagnostics`, `submit_event_predictions`, `train_model`) share a single polling engine (`Call.poll_until`) powered by a single clock source (`base.monotonic()` / `base.sleep()`).
3. **Preflight Safety & Journal (`safety.py` & `journal.py`)**
   - For financial/quota operations, `safety.py` enforces exact USDC caps, checks daily UTC spend totals in SQLite, verifies no identical `pending`/`unknown` fingerprint is already open, records the intent row, and fires **one** HTTP call without retry.
4. **Protocol Transport (`client/base.py` & `client/retry.py`)**
   - Every HTTP route must exist as an `Endpoint` constant in [client/endpoints.py](../src/mcp_server_everesteer/client/endpoints.py).
   - Safe routes (`safe_to_retry=True`, i.e., `GET` and read-only `validate_futures_submission`) use `RETRY_SAFE` with exponential backoff and `Retry-After` support.
   - Mutating routes (`POST`/`PUT`/`PATCH`/`DELETE`) use `RETRY_UNSAFE` (`rate_limit_attempts=1`, `server_attempts=1`, `retry_ambiguous_timeouts=False`) so a dropped socket never double-submits or double-stakes.
   - HTTP responses that contain HTML instead of API JSON are inspected (`_looks_like_cloudflare_html`) and turned into actionable `CloudflareAccessError` hints.
5. **Credential-Free Presigned Storage (`client/base.py` & `client/transfer.py`)**
   - Large `.parquet` downloads and `.pkl` uploads redirect to S3/CloudFront using a **separate credential-free `httpx2.AsyncClient`** (`self._cdn_http`) that never sends `X-API-Key` or `CF-Access-Client-Secret`.
   - Local writes verify path traversal (`safe_output_path`) and refuse symlinks (`O_NOFOLLOW` on POSIX / `lstat` check).

---

## 3. HTTP Server Hardening (`http_app.py`)

When running remotely (`mcp-server-everesteer serve --transport streamable-http`):

- Binding to a non-loopback address (`0.0.0.0`, etc.) without `EVERESTEER_HTTP_BEARER_TOKEN` immediately raises `ConfigError`.
- `/health` (`GET`) stays open for container liveness probes returning `{"status": "ok"}`.
- All other HTTP routes pass through `BearerAuth` using `hmac.compare_digest` constant-time token comparison.

Next: [03 — State, Money, and Safety](03-state-money-and-safety.md)
