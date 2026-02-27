"""h-cli Memory Proxy — stdio-to-SSE bridge for memory_search.

Thin proxy that forwards memory_search calls from Claude (stdio MCP)
to the memory server running in the core container (SSE on port 8084).
Same pattern as firewall.py's _forward_to_core, but no gating needed.
"""

import asyncio
import os

from mcp.server.fastmcp import FastMCP
from mcp.client.sse import sse_client
from mcp import ClientSession

from hcli_logging import get_logger

logger = get_logger(__name__, service="memory-proxy")

MEMORY_SSE_URL = os.environ.get("MEMORY_SSE_URL", "http://h-cli-core:8084/sse")

mcp = FastMCP("h-cli-memory")


async def _forward_to_memory(query: str, limit: int) -> str:
    """Forward search request to core's memory server via SSE."""
    try:
        async with sse_client(MEMORY_SSE_URL) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await asyncio.wait_for(
                    session.call_tool("memory_search", {"query": query, "limit": limit}),
                    timeout=30,
                )
                texts = []
                for block in result.content:
                    if hasattr(block, "text"):
                        texts.append(block.text)
                return "\n".join(texts) if texts else "No results."
    except asyncio.TimeoutError:
        logger.error("Memory server timed out for query: %s", query[:100])
        return "Error: memory search timed out"
    except Exception as e:
        logger.exception("Failed to forward to memory server")
        return f"Error: could not reach memory server — {e}"


@mcp.tool()
async def memory_search(query: str, limit: int = 5) -> str:
    """Search curated Q&A memory for relevant past knowledge.

    Use this to find answers from previous conversations that were
    curated into long-term memory. Returns the most relevant entries.

    Args:
        query: Natural language search query.
        limit: Maximum number of results (default 5, max 20).
    """
    limit = max(1, min(limit, 20))
    return await _forward_to_memory(query, limit)


if __name__ == "__main__":
    logger.info("Memory proxy starting, forwarding to %s", MEMORY_SSE_URL)
    mcp.run(transport="stdio")
