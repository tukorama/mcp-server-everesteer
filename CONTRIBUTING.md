# Contributing

```bash
uv sync --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

To inspect the server interactively with the MCP Inspector:

```bash
uv run mcp dev src/mcp_server_everesteer/server.py
```

## Architecture & Conventions

- Every HTTP call goes through an `Endpoint` constant in `src/mcp_server_everesteer/client/endpoints.py`.
- Operations live in `src/mcp_server_everesteer/ops/`, decorated with `@operation(...)`. One operation becomes both an MCP tool and a LangChain/LangGraph tool.
- Tools that spend event upload quota, move money, or execute destructive/emergency changes must define a `describe` function (server-authored confirmation text) and return a `WriteResult`.
- Keep visualization (`matplotlib`/`plotnine`) and heavy dataframe/ML libraries (`pandas`/`numpy`/`scipy`) out of this package; those live in the caller's modeling layer.
- Tests use scripted `httpx2.MockTransport` responses in `tests/conftest.py` (`FakeApi`). Running `EVERESTEER_TEST_COVERAGE=strict uv run pytest` fails if any `Endpoint` constant is never exercised.
- Never commit real `eiq_*` API keys, Cloudflare Access secrets, or `credentials.json`. Gitleaks checks every commit and CI run via `.gitleaks.toml`.

## Live Testing

`tests/live` runs read-only smoke checks against `https://api.everesteer.ai` only when `EVERESTEER_LIVE=1` and an API key are set. It never writes.

## Versioning

Tool names, parameters, output fields, CLI flags, and environment variables follow [Semantic Versioning](https://semver.org/). While the version is `0.y.z`, a minor release may change them and a patch release will not. From `1.0.0`, breaking changes require a major release.
