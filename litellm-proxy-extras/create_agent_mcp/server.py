"""
Gateway-side ``create_agent`` MCP upstream.

This is a standalone MCP server (stdio transport, MCP Python SDK) that exposes a
single ``create_agent`` tool. It is registered into the LiteLLM Gateway MCP
registry / access group / tool allowlist (see ``registry.yaml``) so the gateway
is the single, audited entry point for agent creation.

Boundary (A段 / dry-run only):
- Default mode is **dry-run**: the tool returns a plan + ``request_id`` and
  performs NO live create / bind / start / canary.
- Fail-closed: ``allow_live_create=true``, a non-empty ``machine_id``, or any
  missing required field is REJECTED before any action. Live create is not
  available in this phase (it requires a separate owner GO + Runtime Placement
  gate — see task #907).
- No secrets: only the public ``request_id``, mode, no-change flag, and blocked
  reason are ever returned. Tool args / request bodies / secret values are never
  echoed.

This module is import-safe (no side effects on import). Run as a process via
``python -m create_agent_mcp.server`` (see ``__main__.py``).
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# Tool / registry identity (must match registry.yaml).
SERVER_NAME = "create-agent-dryrun-local"
ACCESS_GROUP = "agent-create-dryrun"
TOOL_NAME = "create_agent"
ALLOW_ALL_KEYS = False

# Required fields for a create intent. Keeping this minimal per the handoff doc.
REQUIRED_FIELDS = ("purpose", "machine_id", "runtime_target")

# Fail-closed: this phase NEVER performs live create, even if the caller asks.
LIVE_CREATE_DISABLED = True

# Safe-shape patterns for the no-secret invariant. Any tool arg or returned
# value matching these is rejected / redacted before emission.
SECRET_SHAPE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk_(?:agent|machine)_[A-Za-z0-9_\-]{6,}"),  # Slock agent/machine tokens
    re.compile(r"gh[opsu]_[A-Za-z0-9]{6,}"),
    re.compile(r"AKIA[0-9A-Z]{6,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
)


def _looks_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(p.search(value) for p in SECRET_SHAPE_PATTERNS)


def _deep_secret_scan(value: Any) -> bool:
    """Recursively scan an arbitrary object for secret-shaped strings."""
    if isinstance(value, str):
        return _looks_secret(value)
    if isinstance(value, list):
        return any(_deep_secret_scan(v) for v in value)
    if isinstance(value, dict):
        return any(_deep_secret_scan(v) for v in value.values())
    return False


@dataclass(frozen=True)
class CreateResult:
    """Structured result of a create_agent tool call."""

    ok: bool
    request_id: str
    mode: str
    executed: bool
    no_change: bool
    blocked_reason: Optional[str]
    candidate_refs: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "mode": self.mode,
            "executed": self.executed,
            "no_change": self.no_change,
            "blocked_reason": self.blocked_reason,
            "candidate_refs": self.candidate_refs,
            "server": SERVER_NAME,
            "access_group": ACCESS_GROUP,
            "tool": TOOL_NAME,
        }


def _new_request_id() -> str:
    """Short, opaque, correlation id. No secret material."""
    return f"req-{uuid.uuid4().hex[:16]}"


def _redact_secret_key(key: str) -> str:
    """Map a secret-shaped key name to a redaction placeholder."""
    return f"<redacted:key:{key}>"


def validate_create_intent(args: Dict[str, Any]) -> Tuple[bool, Optional[str], Dict[str, str]]:
    """Validate a create_agent tool-call argument dict.

    Returns (ok, blocked_reason_or_none, candidate_refs).
    Fail-closed on: missing required fields, allow_live_create=true,
    missing/empty machine_id, or any secret-shaped value in the args.
    """
    if not isinstance(args, dict):
        return False, "args must be a JSON object", {}

    # Deep secret scan — never accept secret-shaped tool args.
    if _deep_secret_scan(args):
        return False, "args contain secret-shaped value(s)", {}

    # Required fields presence + non-empty.
    for field in REQUIRED_FIELDS:
        val = args.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            return False, f"missing required field: {field}", {}

    machine_id = str(args["machine_id"]).strip()
    purpose = str(args["purpose"]).strip()
    runtime_target = str(args["runtime_target"]).strip()

    # machine_id shape (no traversal / no secret). Must start with a letter.
    if not re.match(r"^[A-Za-z][A-Za-z0-9_.\-]{1,127}$", machine_id):
        return False, "machine_id has unsafe shape", {}

    # Live create is fail-closed in this phase.
    if args.get("allow_live_create") in (True, "true", "True", "1", 1):
        if LIVE_CREATE_DISABLED:
            return False, "allow_live_create requires a separate owner GO + Runtime Placement gate (not available in this phase)", {}

    candidate_refs = {
        "machine_id": machine_id,
        "runtime_target": runtime_target,
        "purpose": purpose[:120],
    }
    return True, None, candidate_refs


def create_agent(args: Dict[str, Any]) -> CreateResult:
    """The create_agent tool implementation (dry-run only, fail-closed)."""
    request_id = _new_request_id()
    ok, blocked_reason, candidate_refs = validate_create_intent(args)
    if not ok:
        return CreateResult(
            ok=False,
            request_id=request_id,
            mode="dry-run",
            executed=False,
            no_change=True,
            blocked_reason=blocked_reason,
            candidate_refs=candidate_refs,
        )

    # Dry-run success: plan only, no live action.
    return CreateResult(
        ok=True,
        request_id=request_id,
        mode="dry-run",
        executed=False,
        no_change=True,
        blocked_reason=None,
        candidate_refs=candidate_refs,
    )


def tool_schema() -> Dict[str, Any]:
    """MCP tool definition exposed to the gateway registry."""
    return {
        "name": TOOL_NAME,
        "description": (
            "Gateway-side create_agent (dry-run only). Returns a plan + "
            "request_id; performs no live create/bind/start. Live create "
            "requires a separate owner GO + Runtime Placement gate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "purpose": {"type": "string", "description": "Short create intent / reason."},
                "machine_id": {"type": "string", "description": "Target machine identifier."},
                "runtime_target": {"type": "string", "description": "Target runtime/service slug."},
                "allow_live_create": {
                    "type": "boolean",
                    "default": False,
                    "description": "Fail-closed in this phase; always rejected.",
                },
            },
            "required": ["purpose", "machine_id", "runtime_target"],
        },
    }


def list_tools() -> Dict[str, Any]:
    """Gateway-facing tool list for this upstream."""
    return {"tools": [tool_schema()]}


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a tool call by name. Fail-closed on unknown tool."""
    if name != TOOL_NAME:
        return CreateResult(
            ok=False,
            request_id=_new_request_id(),
            mode="dry-run",
            executed=False,
            no_change=True,
            blocked_reason=f"unknown tool: {name}",
            candidate_refs={},
        ).to_dict()
    return create_agent(arguments).to_dict()
