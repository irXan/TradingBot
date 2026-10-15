"""Agno agent connected to the Agno docs MCP server.

Follows https://docs.agno.com/mcp (Model Context Protocol integration):

    from agno.agent import Agent
    from agno.tools.mcp import MCPTools

    agent = Agent(
        model=...,
        tools=[MCPTools(transport="streamable-http", url="https://docs.agno.com/mcp")],
    )

Usage:
    python -m crypto.mcp_agent --list-tools
    python -m crypto.mcp_agent "What is MCP support in Agno?"
"""

from __future__ import annotations

import argparse
import asyncio
import os

from agno.agent import Agent
from agno.tools.mcp import MCPTools

AGNO_MCP_URL = os.getenv("AGNO_MCP_URL", "https://docs.agno.com/mcp")
MODEL_ID = os.getenv("CRYPTO_MODEL_ID", "gpt-5.2")


def build_mcp_tools() -> MCPTools:
    """Initialize the MCP integration for the Agno docs server."""
    return MCPTools(
        transport="streamable-http",
        url=AGNO_MCP_URL,
    )


def _build_model():
    """Build the LLM model, importing the provider lazily (needs an API key)."""
    try:
        from agno.models.openai import OpenAIResponses
    except ImportError as exc:
        raise ImportError(
            "No LLM provider installed. Install one (e.g. `uv add 'agno[openai]'`) "
            "and set its API key (e.g. OPENAI_API_KEY) to run the agent."
        ) from exc
    return OpenAIResponses(id=MODEL_ID)


def build_agent(mcp_tools: MCPTools | None = None) -> Agent:
    """Build the agent with the MCP tools attached."""
    return Agent(
        name="crypto",
        model=_build_model(),
        tools=[mcp_tools or build_mcp_tools()],
        instructions=(
            "You are a helpful assistant that answers questions about Agno using "
            "the Agno documentation MCP server. Prefer information returned by the "
            "server over prior knowledge, and cite the relevant doc pages when possible."
        ),
        markdown=True,
    )


async def list_tools() -> list[str]:
    """Connect to the MCP server and return the discovered tool names."""
    mcp_tools = build_mcp_tools()
    await mcp_tools.connect()
    try:
        return sorted(mcp_tools.functions.keys())
    finally:
        await mcp_tools.close()


async def run(message: str) -> None:
    """Connect to the MCP server, run the agent, and always close the connection."""
    mcp_tools = build_mcp_tools()
    await mcp_tools.connect()
    try:
        agent = build_agent(mcp_tools)
        await agent.aprint_response(message, stream=True)
    finally:
        # Always close the connection when done
        await mcp_tools.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto agent backed by the Agno docs MCP server")
    parser.add_argument("message", nargs="?", help="Question to ask the agent")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Connect to the MCP server and list the available tools, then exit",
    )
    args = parser.parse_args()

    if args.list_tools:
        tools = asyncio.run(list_tools())
        print("Available MCP tools:")
        for tool in tools:
            print(f"  - {tool}")
        return

    if not args.message:
        parser.error("a message is required unless --list-tools is used")

    asyncio.run(run(args.message))


if __name__ == "__main__":
    main()
