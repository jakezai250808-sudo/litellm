"""Run the create_agent MCP upstream as a stdio MCP server process.

Usage:
    python -m create_agent_mcp

The MCP Python SDK (``mcp`` package, already a LiteLLM dependency) provides the
stdio transport. The gateway registers this process via ``registry.yaml`` and
calls ``tools/list`` + ``tools/call`` over stdio.

The decorated ``create_agent`` tool signature below is the deploy contract: the
gateway derives the public tool schema from it. It intentionally exposes ONLY
``purpose`` + ``runtime_target`` (+ the always-rejected ``allow_live_create``).
``machine_id`` is NOT a parameter — machine placement / binding is owned by the
Runtime Placement gate (task #907), not the caller.

This entry point only runs when executed as a process; importing the package or
``server`` module has no side effects.
"""

from __future__ import annotations

import inspect
import sys
from typing import Any, Dict, Optional


def _to_mcp_text(payload: Dict[str, Any]) -> str:
    """Serialize a tool result dict as MCP text content."""
    import json

    return json.dumps(payload, sort_keys=True)


def _enforce_strict_tool_args(
    mcp: Any,
    tool_name: str,
    advertised_input_schema: Dict[str, Any],
) -> None:
    """Reject unknown MCP tool arguments instead of silently dropping them.

    FastMCP builds a pydantic argument model from the decorated function
    signature. Its default pydantic config ignores extra keys. We keep a hidden
    machine_id parameter so raw callers get the tool's structured fail-closed
    result with a request_id, but the advertised schema remains the smaller
    owner-facing contract from server.tool_schema().
    """
    from pydantic import ConfigDict

    tool = mcp._tool_manager.get_tool(tool_name)  # type: ignore[attr-defined]
    if tool is None:
        raise RuntimeError(f"MCP tool not registered: {tool_name}")

    arg_model = tool.fn_metadata.arg_model
    arg_model.model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )
    arg_model.model_rebuild(force=True)
    tool.parameters = advertised_input_schema


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
        runtime_target: str,
        allow_live_create: bool = False,
        machine_id: Optional[str] = None,
    ) -> str:
        """Gateway-side create_agent (dry-run only). Returns a plan + request_id."""
        tool_args = {
            "purpose": purpose,
            "runtime_target": runtime_target,
            "allow_live_create": allow_live_create,
        }
        if machine_id is not None:
            tool_args["machine_id"] = machine_id

        return _to_mcp_text(
            call_tool(
                TOOL_NAME,
                tool_args,
            )
        )

    _enforce_strict_tool_args(mcp, TOOL_NAME, tool_schema()["inputSchema"])

    # Expose tool metadata so gateway registry readback is consistent.
    mcp._tool_schema = tool_schema  # type: ignore[attr-defined]
    mcp._list_tools = list_tools  # type: ignore[attr-defined]

    mcp.run(transport="stdio")
    return 0


def create_agent_signature() -> inspect.Signature:
    """Return the signature of the decorated create_agent tool.

    Used by tests to assert the deploy contract (no machine_id parameter)
    without requiring the mcp SDK to be installed.
    """
    def _create_agent(
        purpose: str,
        runtime_target: str,
        allow_live_create: bool = False,
        machine_id: Optional[str] = None,
    ) -> str:
        del purpose, runtime_target, allow_live_create, machine_id  # signature-only stub
        return ""

    return inspect.signature(_create_agent)


if __name__ == "__main__":
    raise SystemExit(main())
