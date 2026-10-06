"""Tool list for the LLM — READ-ONLY tools from the MCP client's allow-list.

Safety contract (rules.md §2.9):
- execute_operations is NEVER in this list.
- Enforced by MCPClient.get_llm_tools() + an explicit startup check here.
- Tool descriptions are treated as untrusted data (rules.md §2.7).
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool

from app.mcp_client import MCPClient

logger = logging.getLogger("heatguard.agent.tools")

_WRITE_TOOL_NAME = "execute_operations"


def build_llm_tools(mcp_client: MCPClient) -> list[BaseTool]:
    """Build the LLM tool list from the MCP client's allow-list.

    Raises RuntimeError if execute_operations sneaks in (belt-and-suspenders check).
    This function is called once at startup when the graph is built.

    Args:
        mcp_client: Connected MCPClient with schemas already pinned.

    Returns:
        List of LangChain BaseTool objects, all read-only, allow-listed.
    """
    tools = mcp_client.get_llm_tools()

    # Explicit safety check — belt and suspenders
    write_tools = [t for t in tools if t.name == _WRITE_TOOL_NAME]
    if write_tools:
        raise RuntimeError(
            f"SAFETY VIOLATION: '{_WRITE_TOOL_NAME}' is in the LLM tool list. "
            "This is forbidden by the safety contract (rules.md §2.9 rule 3). "
            "Check LLM_TOOL_ALLOWLIST and MCPClient configuration."
        )

    logger.info(
        "LLM tools verified (%d tools, no write tool): %s",
        len(tools),
        [t.name for t in tools],
    )
    return tools
