from mcp_server_everesteer.integrations.langgraph import create_everesteer_agent

# LangGraph Platform supplies its own persistent checkpointer for human-approval interrupts.
graph = create_everesteer_agent("anthropic:claude-sonnet-4-5", checkpointer=None)
