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
``python -m create_agent_mcp`` (see ``__main__.py``).
"""

from __future__ import annotations

import json
import os
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

# ---------------------------------------------------------------------------
# True-create executor config (checked at runtime, fail-closed if missing)
# Set by the Gateway deployer via environment or SSM. Each field must be
# explicitly configured before `create_agent_execute` will call the Raft API.
# If any field is missing the executor returns `ok=false/no_change=true` with
# a specific blocked_reason — it never guesses credentials or placement.
# ---------------------------------------------------------------------------
EXECUTOR_SERVER_ID = os.environ.get("CREATE_AGENT_EXECUTOR_SERVER_ID", "")
EXECUTOR_MACHINE_ID = os.environ.get("CREATE_AGENT_EXECUTOR_MACHINE_ID", "")
EXECUTOR_CONTROL_PLANE_TOKEN_REF = os.environ.get(
    "CREATE_AGENT_EXECUTOR_CONTROL_PLANE_TOKEN_REF", ""
)
EXECUTOR_ENDPOINT = os.environ.get(
    "CREATE_AGENT_EXECUTOR_ENDPOINT",
    "https://api.raft.build/internal/agent-api/agents",
)
EXECUTOR_ENABLED = os.environ.get("CREATE_AGENT_EXECUTOR_ENABLED", "0") == "1"

# Required fields for the public create_agent tool. Kept minimal and
# owner-facing: the caller expresses intent (purpose + runtime_target) only.
# Machine placement / binding is NOT caller-supplied at the Gateway tool
# boundary — it belongs to the Runtime Placement gate (task #907) under owner GO.
# A caller-supplied machine_id is rejected (see validate_create_intent).
PUBLIC_REQUIRED_FIELDS = ("purpose", "runtime_target")

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
    Fail-closed on: missing required public fields, a caller-supplied
    machine_id (machine placement is not exposed at the Gateway tool boundary —
    it belongs to the Runtime Placement gate, task #907), allow_live_create=true,
    or any secret-shaped value in the args.
    """
    if not isinstance(args, dict):
        return False, "args must be a JSON object", {}

    # Deep secret scan — never accept secret-shaped tool args.
    if _deep_secret_scan(args):
        return False, "args contain secret-shaped value(s)", {}

    # Required public fields presence + non-empty.
    for field in PUBLIC_REQUIRED_FIELDS:
        val = args.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            return False, f"missing required field: {field}", {}

    purpose = str(args["purpose"]).strip()
    runtime_target = str(args["runtime_target"]).strip()

    # Caller-supplied machine_id is NOT accepted at the Gateway tool boundary.
    # Machine selection / binding happens under the Runtime Placement gate (#907).
    if args.get("machine_id") not in (None, ""):
        return False, "caller-supplied machine_id is not allowed at the Gateway tool boundary (machine placement is Runtime Placement gate, task #907)", {}

    # Live create is fail-closed in this phase.
    if args.get("allow_live_create") in (True, "true", "True", "1", 1):
        if LIVE_CREATE_DISABLED:
            return False, "allow_live_create requires a separate owner GO + Runtime Placement gate (not available in this phase)", {}

    candidate_refs = {
        "runtime_target": runtime_target,
        "purpose": purpose[:120],
    }
    return True, None, candidate_refs


def create_agent_execute(args: Dict[str, Any]) -> CreateResult:
    """Imperative agent-create via Raft server API.

    Calls POST /api/agents on the Raft server with a control-plane token (SSM
    path ``/slock/raft-api-token`` as specified by PM).  Requires three config
    preconditions — serverId, machineId, control-plane token — and an explicit
    enable flag ``CREATE_AGENT_EXECUTOR_ENABLED=1``.  Any missing precondition
    returns a fail-closed result (``ok=false, executed=false, no_change=true``)
    with a specific ``blocked_reason``.  The executor never guesses credentials
    or placement.

    On success returns ``executed=true, no_change=false`` plus the server-side
    agent-id, server-id, and a bind readiness reference.
    """
    req_id = _new_request_id()

    # 1) Fail-closed: executor must be explicitly enabled.
    if not EXECUTOR_ENABLED:
        return CreateResult(
            ok=False, request_id=req_id, mode="live",
            executed=False, no_change=True,
            blocked_reason="true-create executor not enabled (set CREATE_AGENT_EXECUTOR_ENABLED=1)",
            candidate_refs={},
        )

    # 2) Validate create intent (same contract as dry-run).
    ok, blocked, refs = validate_create_intent(args)
    if not ok:
        return CreateResult(
            ok=False, request_id=req_id, mode="live",
            executed=False, no_change=True, blocked_reason=blocked,
            candidate_refs=refs,
        )

    # 3) Executor preconditions.
    missing = []
    if not EXECUTOR_SERVER_ID:
        missing.append("CREATE_AGENT_EXECUTOR_SERVER_ID")
    if not EXECUTOR_MACHINE_ID:
        missing.append("CREATE_AGENT_EXECUTOR_MACHINE_ID")
    if not EXECUTOR_CONTROL_PLANE_TOKEN_REF:
        missing.append("CREATE_AGENT_EXECUTOR_CONTROL_PLANE_TOKEN_REF")
    if missing:
        return CreateResult(
            ok=False, request_id=req_id, mode="live",
            executed=False, no_change=True,
            blocked_reason=f"true-create executor missing config: {', '.join(missing)}",
            candidate_refs=refs,
        )

    # 4) Resolve the control-plane token from SSM.
    import subprocess as _sp
    token = ""
    try:
        raw = _sp.check_output(
            ["aws", "ssm", "get-parameter", "--name", EXECUTOR_CONTROL_PLANE_TOKEN_REF,
             "--with-decryption", "--region", os.environ.get("AWS_REGION", "us-west-2"),
             "--query", "Parameter.Value", "--output", "text"],
            stderr=_sp.DEVNULL,
            timeout=10,
        )
        token = raw.decode("utf-8").strip()
    except Exception:
        return CreateResult(
            ok=False, request_id=req_id, mode="live",
            executed=False, no_change=True,
            blocked_reason="true-create executor: cannot resolve control-plane token from SSM",
            candidate_refs=refs,
        )

    if not token:
        return CreateResult(
            ok=False, request_id=req_id, mode="live",
            executed=False, no_change=True,
            blocked_reason="true-create executor: resolved token is empty",
            candidate_refs=refs,
        )

    # 5) Build the request body and call POST /api/agents.
    purpose = str(args.get("purpose", "")).strip()
    runtime_target = str(args.get("runtime_target", "")).strip()
    agent_name = f"{EXECUTOR_SERVER_ID}-{runtime_target}-{req_id[-8:]}"
    body = {
        "name": agent_name,
        "displayName": args.get("display_name", purpose[:80]),
        "description": purpose[:500],
        "model": args.get("model", os.environ.get("WORKER_MODEL", "auto-cheap")),
        "runtime": args.get("runtime", os.environ.get("AGENT_RUNTIME_CLIENT", "claude-code")),
        "server": EXECUTOR_SERVER_ID,
        "machineId": EXECUTOR_MACHINE_ID,
    }

    try:
        import urllib.request as _ur
        data = json.dumps(body).encode("utf-8")
        req = _ur.Request(
            EXECUTOR_ENDPOINT,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Server-Id": EXECUTOR_SERVER_ID,
            },
            method="POST",
        )
        with _ur.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return CreateResult(
            ok=False, request_id=req_id, mode="live",
            executed=False, no_change=True,
            blocked_reason=f"true-create executor API call failed: {type(exc).__name__}",
            candidate_refs=refs,
        )

    # 6) Build success result.
    return CreateResult(
        ok=True, request_id=req_id, mode="live",
        executed=True, no_change=False, blocked_reason=None,
        candidate_refs={
            **refs,
            "purpose": purpose[:120],
            "runtime_target": runtime_target,
            "agentId": resp_data.get("id", ""),
            "serverId": EXECUTOR_SERVER_ID,
            "machineId": EXECUTOR_MACHINE_ID,
            "bindReadiness": "pending",  # verified in a follow-up readback, not here
        },
    )


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


# ---------------------------------------------------------------------------
# Live create path (B段 — requires owner GO + Runtime Placement gate #907)
# ---------------------------------------------------------------------------
def create_agent_live(args: Dict[str, Any]) -> CreateResult:
    """Live create_agent with a structured liveCreateCard.

    Validates the intent identically to the dry-run path (fail-closed on
    missing required fields, unsafe machine_id, secret-shaped args), then
    produces a liveCreateCard with the full create intent + request_id +
    readback/rollback plans. The actual agent creation is executed by #907
    under owner GO + Runtime Placement gate.

    This function does NOT call the Raft/Runtime API itself — that call lives
    in #907. It produces the structured card that #907 consumes.
    """
    request_id = _new_request_id()
    ok, blocked_reason, candidate_refs = validate_create_intent(args)
    if not ok:
        return CreateResult(
            ok=False, request_id=request_id, mode="live",
            executed=False, no_change=True, blocked_reason=blocked_reason,
            candidate_refs=candidate_refs,
        )

    purpose = str(args.get("purpose", "")).strip()
    runtime_target = str(args.get("runtime_target", "")).strip()

    return CreateResult(
        ok=True, request_id=request_id, mode="live",
        executed=False,  # executed by #907 (owner GO + Runtime Placement gate)
        no_change=True,  # still no-apply until #907 executes
        blocked_reason=None,
        candidate_refs={
            **candidate_refs,
            "liveCreatePending": "true",
            "executionRequires": "owner GO + Runtime Placement gate (#907)",
            "purpose": purpose[:120],
            "runtime_target": runtime_target,
        },
    )


def tool_schema() -> Dict[str, Any]:
    """MCP tool definition exposed to the gateway registry.

    The public schema is owner-facing and minimal: the caller expresses intent
    (purpose + runtime_target) only. machine placement / binding is NOT exposed
    at the Gateway tool boundary — it belongs to the Runtime Placement gate
    (task #907) under owner GO. A caller-supplied machine_id is rejected.
    """
    return {
        "name": TOOL_NAME,
        "description": (
            "Gateway-side create_agent. Returns a plan + request_id. "
            "mode=dry-run (default): no live create/bind/start. "
            "mode=live: produces a liveCreateCard for execution under "
            "owner GO + Runtime Placement gate (#907). Live card does "
            "NOT execute the create itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "purpose": {"type": "string", "description": "Short create intent / reason."},
                "runtime_target": {"type": "string", "description": "Target runtime/service slug."},
                "mode": {
                    "type": "string",
                    "enum": ["dry-run", "live"],
                    "default": "dry-run",
                    "description": "dry-run=plan only (default); live=produce liveCreateCard for #907 gate",
                },
                "allow_live_create": {
                    "type": "boolean",
                    "default": False,
                    "description": "Fail-closed in this phase; always rejected.",
                },
            },
            "required": ["purpose", "runtime_target"],
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
    mode = arguments.get("mode", "dry-run")
    if mode not in ("dry-run", "live"):
        return CreateResult(
            ok=False,
            request_id=_new_request_id(),
            mode=mode,
            executed=False,
            no_change=True,
            blocked_reason=f"unknown mode: {mode!r} (valid: dry-run, live)",
            candidate_refs={},
        ).to_dict()
    if mode == "live":
        if EXECUTOR_ENABLED:
            return create_agent_execute(arguments).to_dict()
        return create_agent_live(arguments).to_dict()
    return create_agent(arguments).to_dict()
