"""Run the create_agent MCP upstream as a stdio MCP server process.

Usage:
    python -m create_agent_mcp.server

The MCP Python SDK (``mcp`` package, already a LiteLLM dependency) provides the
stdio transport. The gateway registers this process via ``registry.yaml`` and
calls ``tools/list`` + ``tools/call`` over stdio.

This entry point only runs when executed as a process; importing the package or
``server`` module has no side effects.
"""

from __future__ import annotations

import sys
from typing import Any, Dict


def _to_mcp_text(payload: Dict[str, Any]) -> str:
    """Serialize a tool result dict as MCP text content."""
    import json

    return json.dumps(payload, sort_keys=True)


def main() -> int:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - dependency present in LiteLLM env
        sys.stderr.write(f"mcp SDK not available: {exc}\n")
        return 2

    from .server import TOOL_NAME, call_tool, list_tools, tool_schema

    mcp = FastMCP("create-agent-dryrun-local")

    @mcp.tool()
    def create_agent(  # noqa: D401 - MCP tool
        purpose: str,
        machine_id: str,
        runtime_target: str,
        allow_live_create: bool = False,
    ) -> str:
        """Gateway-side create_agent (dry-run only). Returns a plan + request_id."""
        return _to_mcp_text(
            call_tool(
                TOOL_NAME,
                {
                    "purpose": purpose,
                    "machine_id": machine_id,
                    "runtime_target": runtime_target,
                    "allow_live_create": allow_live_create,
                },
            )
        )

    # Expose tool metadata so gateway registry readback is consistent.
    mcp._tool_schema = tool_schema  # type: ignore[attr-defined]
    mcp._list_tools = list_tools  # type: ignore[attr-defined]

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
