# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""<tool>: one-line description of what this MCP server offers.

Run with `uv run --script server.py`. Claude Code starts it from the agent
folder, so relative paths are relative to the agent folder.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("<tool>")


@mcp.tool()
def example(text: str, repeat: int = 1) -> str:
    """Say what the tool does and when to use it; this becomes its description."""
    return text * repeat


if __name__ == "__main__":
    mcp.run()
