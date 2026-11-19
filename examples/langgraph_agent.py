"""Run an Everesteer research & tournament agent with LangGraph.

Usage:
    uv run --extra langgraph --with langchain-anthropic python examples/langgraph_agent.py
    uv run --extra langgraph --with "langchain[mcp]" --with langchain-anthropic \
        python examples/langgraph_agent.py --mcp

Set ANTHROPIC_API_KEY and EVERESTEER_API_KEY (or run `uvx mcp-server-everesteer auth`).
"""

import argparse
import asyncio
from pathlib import Path
from typing import Any

MODEL = "anthropic:claude-sonnet-4-5"
QUESTION = (
    "Call whoami and get_started to confirm my Everesteer scope, read explain_scoring and "
    "get_dataset_schema, and summarize how I should prepare my first submission."
)
PROJECT = str(Path(__file__).resolve().parent.parent)


async def native() -> None:
    from langgraph.types import Command

    from mcp_server_everesteer.integrations.langgraph import create_everesteer_agent

    agent = create_everesteer_agent(MODEL)
    config: Any = {"configurable": {"thread_id": "everesteer-example"}}
    result: dict[str, Any] = await agent.ainvoke({"messages": [("user", QUESTION)]}, config=config)
    while result.get("__interrupt__"):
        pause = result["__interrupt__"][0].value
        raw = await asyncio.to_thread(input, f"\n{pause['message']}\n[y/N] ")
        answer = raw.strip().lower() == "y"
        result = await agent.ainvoke(Command(resume=answer), config=config)
    print(result["messages"][-1].content)


async def over_mcp() -> None:
    from langchain.agents import create_agent  # pyright: ignore[reportMissingImports]
    from langchain.mcp import MCPAdapter  # pyright: ignore[reportMissingImports]

    config: dict[str, Any] = {
        "mcpServers": {
            "everesteer": {
                "command": "uvx",
                "args": ["--from", PROJECT, "mcp-server-everesteer"],
                "timeout": 120_000,
            }
        }
    }
    async with MCPAdapter(config) as adapter:
        agent: Any = create_agent(MODEL, await adapter.list_tools())
        result: dict[str, Any] = await agent.ainvoke({"messages": [("user", QUESTION)]})
    print(result["messages"][-1].content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp", action="store_true", help="connect over MCP instead of in-process")
    asyncio.run(over_mcp() if parser.parse_args().mcp else native())
