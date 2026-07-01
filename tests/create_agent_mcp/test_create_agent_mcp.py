"""Unit tests for the Gateway-side create_agent MCP upstream (dry-run, fail-closed).

Covers (per @Ops-new review checklist):
- tool-list / tool_schema shape
- dry-run call success (plan + request_id, no execution)
- missing required field -> fail-closed
- allow_live_create -> fail-closed (this phase)
- machine_id unsafe shape -> fail-closed
- secret-shaped tool args -> rejected
- no secret / raw body / token leakage in any output
- registry identity constants match registry.yaml
"""

from __future__ import annotations

import json
import inspect
import os
import sys
import unittest

# Make the module importable when run from the repo root.
HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "litellm-proxy-extras"))
sys.path.insert(0, PKG_ROOT)

from create_agent_mcp import (  # noqa: E402
    ACCESS_GROUP,
    ALLOW_ALL_KEYS,
    SERVER_NAME,
    TOOL_NAME,
    call_tool,
    create_agent,
    create_agent_live,
    list_tools,
    tool_schema,
    validate_create_intent,
)
from create_agent_mcp.server import (  # noqa: E402
    CreateResult,
    _deep_secret_scan,
)


class TestToolShape(unittest.TestCase):
    def test_tool_name_constant(self):
        self.assertEqual(TOOL_NAME, "create_agent")

    def test_tool_schema_shape(self):
        schema = tool_schema()
        self.assertEqual(schema["name"], TOOL_NAME)
        props = schema["inputSchema"]["properties"]
        for field in ("purpose", "runtime_target", "allow_live_create"):
            self.assertIn(field, props)
        # machine_id must NOT be in the public schema
        self.assertNotIn("machine_id", props)
        self.assertEqual(
            set(schema["inputSchema"]["required"]),
            {"purpose", "runtime_target"},
        )

    def test_list_tools(self):
        listing = list_tools()
        self.assertEqual(len(listing["tools"]), 1)
        self.assertEqual(listing["tools"][0]["name"], TOOL_NAME)


class TestDryRunSuccess(unittest.TestCase):
    def test_dryrun_success_returns_plan_no_execution(self):
        result = create_agent(
            {
                "purpose": "onboard infra-fix throwaway",
                "runtime_target": "infra-e2e-codex-1017",
            }
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "dry-run")
        self.assertFalse(result.executed)
        self.assertTrue(result.no_change)
        self.assertIsNone(result.blocked_reason)
        self.assertTrue(result.request_id.startswith("req-"))
        self.assertNotIn("machine_id", result.candidate_refs)
        self.assertEqual(result.candidate_refs["runtime_target"], "infra-e2e-codex-1017")

    def test_each_call_gets_fresh_request_id(self):
        a = create_agent({"purpose": "x", "runtime_target": "r"})
        b = create_agent({"purpose": "x", "runtime_target": "r"})
        self.assertNotEqual(a.request_id, b.request_id)


class TestFailClosed(unittest.TestCase):
    def _expect_blocked(self, args, reason_fragment=None):
        result = create_agent(args)
        self.assertFalse(result.ok)
        self.assertFalse(result.executed)
        self.assertTrue(result.no_change)
        self.assertIsNotNone(result.blocked_reason)
        if reason_fragment:
            self.assertIn(reason_fragment, result.blocked_reason)

    def test_missing_purpose(self):
        self._expect_blocked({"runtime_target": "r"}, "purpose")

    def test_missing_runtime_target(self):
        self._expect_blocked({"purpose": "p"}, "runtime_target")

    def test_empty_string_field(self):
        self._expect_blocked({"purpose": "  ", "runtime_target": "r"})

    def test_caller_supplied_machine_id_rejected(self):
        self._expect_blocked(
            {"purpose": "p", "runtime_target": "r", "machine_id": "ip-172-31-58-63"},
            "machine_id",
        )

    def test_allow_live_create_bool_rejected(self):
        self._expect_blocked(
            {"purpose": "p", "runtime_target": "r", "allow_live_create": True},
            "owner GO",
        )

    def test_allow_live_create_string_rejected(self):
        self._expect_blocked(
            {"purpose": "p", "runtime_target": "r", "allow_live_create": "true"},
            "owner GO",
        )

    def test_args_not_dict(self):
        ok, reason, _ = validate_create_intent("not-a-dict")  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertIn("object", reason)

    def test_unknown_tool_dispatch(self):
        out = call_tool("not_create_agent", {"purpose": "p"})
        self.assertFalse(out["ok"])
        self.assertIn("unknown tool", out["blocked_reason"])


class TestNoSecretLeak(unittest.TestCase):
    def test_secret_shaped_arg_rejected(self):
        result = create_agent(
            {"purpose": "sk-ant-secretvalue123", "runtime_target": "r"}
        )
        self.assertFalse(result.ok)
        self.assertIn("secret", result.blocked_reason)

    def test_secret_in_nested_arg_rejected(self):
        result = create_agent(
            {
                "purpose": "p",
                "runtime_target": "r",
                "extra": {"token": "gho_leakedtoken1234567"},
            }
        )
        self.assertFalse(result.ok)

    def test_output_never_contains_secret(self):
        # Even when args contain a secret, the result must not echo it.
        result = create_agent(
            {"purpose": "p", "runtime_target": "r", "leak": "AKIAIOSFODNN7EXAMPLE"}
        )
        serialized = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", serialized)

    def test_deep_secret_scan_primitives(self):
        self.assertTrue(_deep_secret_scan("sk-ant-test123"))
        self.assertTrue(_deep_secret_scan({"a": ["gho_abc1234567"]}))
        self.assertFalse(_deep_secret_scan("normal text"))
        self.assertFalse(_deep_secret_scan({"a": ["fine"]}))

    def test_slock_agent_token_rejected(self):
        result = create_agent(
            {"purpose": "sk_agent_redactedtoken123", "runtime_target": "r"}
        )
        self.assertFalse(result.ok)
        self.assertIn("secret", result.blocked_reason)
        self.assertNotIn("sk_agent_redactedtoken123", json.dumps(result.to_dict()))

    def test_slock_machine_token_rejected(self):
        result = create_agent(
            {"purpose": "p", "runtime_target": "sk_machine_redactedabcd"}
        )
        self.assertFalse(result.ok)
        self.assertNotIn("sk_machine_redactedabcd", json.dumps(result.to_dict()))


class TestEntrypoint(unittest.TestCase):
    def test_main_is_callable_without_mcp(self):
        # The registry runs `python -m create_agent_mcp` -> __main__.main().
        # main() must be importable and callable; without the mcp SDK it returns 2
        # cleanly (no crash). This proves the entrypoint resolves.
        from create_agent_mcp import __main__

        self.assertTrue(callable(__main__.main))
        rc = __main__.main()
        self.assertEqual(rc, 2)

    def test_decorated_tool_signature_has_no_machine_id(self):
        # The gateway derives the public tool schema from the decorated
        # create_agent function signature (deploy contract). machine_id must NOT
        # be a parameter — machine placement is owned by the Runtime Placement
        # gate (#907), not the caller. mode= must be present with dry-run default.
        from create_agent_mcp import __main__

        sig = __main__.create_agent_signature()
        params = set(sig.parameters.keys())
        self.assertEqual(params, {"purpose", "runtime_target", "mode", "allow_live_create"})
        self.assertNotIn("machine_id", params)
        # required (no default) params are the public required fields
        required = {
            name for name, p in sig.parameters.items() if p.default is inspect.Parameter.empty
        }
        self.assertEqual(required, {"purpose", "runtime_target"})
        # mode defaults to dry-run
        self.assertEqual(sig.parameters["mode"].default, "dry-run")


class TestRegistryIdentity(unittest.TestCase):
    def test_constants_match_registry_yaml(self):
        self.assertEqual(SERVER_NAME, "create-agent-dryrun-local")
        self.assertEqual(ACCESS_GROUP, "agent-create-dryrun")
        self.assertFalse(ALLOW_ALL_KEYS)

    def test_registry_yaml_file_exists_and_parses(self):
        import yaml  # PyYAML is a LiteLLM dependency

        registry_path = os.path.join(PKG_ROOT, "create_agent_mcp", "registry.yaml")
        self.assertTrue(os.path.isfile(registry_path))
        with open(registry_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        self.assertEqual(data["server_name"], SERVER_NAME)
        self.assertEqual(data["access_group"], ACCESS_GROUP)
        self.assertEqual(data["allowed_tools"], [TOOL_NAME])
        self.assertFalse(data["allow_all_keys"])


class TestLiveCreate(unittest.TestCase):
    def test_live_create_returns_card_not_executed(self):
        result = create_agent_live(
            {"purpose": "onboard test agent", "runtime_target": "infra-agent"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "live")
        self.assertFalse(result.executed)  # #907 executes, not this tool
        self.assertTrue(result.no_change)
        self.assertIsNone(result.blocked_reason)
        self.assertIn("liveCreatePending", result.candidate_refs)
        self.assertTrue(result.candidate_refs["liveCreatePending"] == "true")
        self.assertIn("executionRequires", result.candidate_refs)

    def test_live_create_invalid_rejected(self):
        result = create_agent_live({"runtime_target": "r"})  # missing purpose
        self.assertFalse(result.ok)
        self.assertIn("purpose", result.blocked_reason)

    def test_live_create_with_secret_rejected(self):
        result = create_agent_live(
            {"purpose": "sk-agent-secret123", "runtime_target": "r"}
        )
        self.assertFalse(result.ok)

    def test_call_tool_dispatch_live_mode(self):
        out = call_tool(
            "create_agent",
            {"mode": "live", "purpose": "test", "runtime_target": "r"},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "live")
        self.assertTrue(out["candidate_refs"]["liveCreatePending"] == "true")

    def test_call_tool_dispatch_default_dry_run(self):
        out = call_tool(
            "create_agent",
            {"purpose": "test", "runtime_target": "r"},
        )
        self.assertEqual(out["mode"], "dry-run")

    def test_call_tool_unknown_mode_fail_closed(self):
        out = call_tool(
            "create_agent",
            {"purpose": "test", "runtime_target": "r", "mode": "nonsense"},
        )
        self.assertFalse(out["ok"])
        self.assertIn("unknown mode", out["blocked_reason"])

    def test_tool_schema_has_mode_param(self):
        schema = tool_schema()
        props = schema["inputSchema"]["properties"]
        self.assertIn("mode", props)
        self.assertEqual(props["mode"]["enum"], ["dry-run", "live"])
        self.assertEqual(props["mode"]["default"], "dry-run")


if __name__ == "__main__":
    unittest.main()
