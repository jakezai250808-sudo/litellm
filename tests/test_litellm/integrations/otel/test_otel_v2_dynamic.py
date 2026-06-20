"""Per-tenant tracer routing on admin-owned OTEL destinations.

A trace destination is resolved server-side from a key/team's named credential
into an ``OtelDestination`` (endpoint + headers); the v2 logger routes on that
and never on request-supplied vendor credentials. These tests lock the contract:
the request cannot route a trace, the endpoint follows the destination's host
(cross-host fix), and one tenant's destination never rewrites a co-configured
backend's exporter.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("../../../.."))

from opentelemetry.trace import NoOpTracer

from litellm.integrations.otel.model.config import ExporterSpec, OpenTelemetryV2Config
from litellm.integrations.otel.model.destination import OtelDestination
from litellm.integrations.otel.model.metadata import LLMCallEvent
from litellm.integrations.otel.plumbing.routing import TenantTracerCache


def _cache(callback_name, exporters=None):
    cfg = OpenTelemetryV2Config(exporters=exporters or [ExporterSpec(kind="in_memory")])
    return TenantTracerCache(cfg, callback_name, "litellm")


def _dest(endpoint="https://eu.example/api/public/otel", auth="Basic AAAA"):
    return OtelDestination(endpoint=endpoint, headers={"Authorization": auth})


# --- routing only happens for an admin destination ------------------------- #


def test_no_destination_uses_default_tracer():
    cache = _cache("langfuse_otel")
    default = NoOpTracer()
    assert cache.tracer_for(default, None) is default
    assert cache._providers == {}


def test_provider_cached_per_destination():
    cache = _cache("langfuse_otel")
    default = NoOpTracer()
    a = _dest(endpoint="https://eu.example/api/public/otel", auth="Basic A")
    b = _dest(endpoint="https://eu.example/api/public/otel", auth="Basic B")

    cache.tracer_for(default, a)
    cache.tracer_for(default, a)  # same destination -> reuse
    assert len(cache._providers) == 1
    cache.tracer_for(default, b)  # different creds -> new provider
    assert len(cache._providers) == 2


def test_different_host_is_a_distinct_provider():
    """Two destinations with identical headers but different hosts must not
    collide. The cache key includes the endpoint; a headers-only key (the old
    behavior) would merge them and one tenant's spans would hit the other host."""
    cache = _cache("langfuse_otel")
    default = NoOpTracer()
    eu = _dest(endpoint="https://cloud.langfuse.com/api/public/otel", auth="Basic X")
    us = _dest(endpoint="https://us.cloud.langfuse.com/api/public/otel", auth="Basic X")

    cache.tracer_for(default, eu)
    cache.tracer_for(default, us)
    assert len(cache._providers) == 2


def test_provider_cache_is_bounded_and_evicts_lru(monkeypatch):
    from litellm.integrations.otel.plumbing import routing as routing_mod

    monkeypatch.setattr(routing_mod, "_MAX_CACHED_PROVIDERS", 2)
    shut_down = []
    monkeypatch.setattr(
        routing_mod, "_shutdown_provider", lambda p: shut_down.append(p)
    )

    cache = _cache("langfuse_otel")
    default = NoOpTracer()

    def dest(host):
        return _dest(endpoint=f"https://{host}/api/public/otel", auth="Basic K")

    cache.tracer_for(default, dest("1"))
    cache.tracer_for(default, dest("2"))
    cache.tracer_for(default, dest("1"))  # touch "1" -> "2" is now LRU
    cache.tracer_for(default, dest("3"))  # overflow -> evict "2"

    assert len(cache._providers) == 2
    assert len(shut_down) == 1


# --- the destination sets the endpoint (cross-host fix), scoped to its owner - #


@pytest.mark.parametrize("owner", ["langfuse_otel", "arize", "weave_otel"])
def test_destination_sets_endpoint_and_headers_on_owned_exporter_only(owner):
    # Exporter-invariant by construction: every backend's resolved destination sets
    # BOTH endpoint and headers on its own exporter. Parametrized so the cross-host
    # fix is asserted for langfuse, arize, and weave, not just one.
    cache = _cache(
        owner,
        exporters=[
            ExporterSpec(
                kind="otlp_http",
                endpoint="https://env-host.example/v1",
                headers="Authorization=Basic ENV",
                owner=owner,
            ),
            ExporterSpec(kind="in_memory", owner=owner),
        ],
    )
    new_cfg = cache._config_with_destination(
        _dest(endpoint="https://resolved-host.example/v1", auth="Basic TEAM")
    )
    otlp, in_mem = new_cfg.exporters
    # The endpoint follows the resolved host, not the env-pinned one -> no 401.
    assert otlp.endpoint == "https://resolved-host.example/v1"
    assert otlp.headers == "Authorization=Basic TEAM"
    assert in_mem.headers is None and in_mem.endpoint is None


def test_destination_does_not_leak_to_other_owners_exporter():
    cache = _cache(
        "langfuse_otel",
        exporters=[
            ExporterSpec(
                kind="otlp_http",
                endpoint="http://self-hosted-collector:4318",
                headers="x=base-collector",
                owner=None,
            ),
            ExporterSpec(
                kind="otlp_grpc",
                endpoint="https://otlp.arize.com/v1",
                headers="space_id=base,api_key=base",
                owner="arize",
            ),
            ExporterSpec(
                kind="otlp_http",
                endpoint="https://us.cloud.langfuse.com/api/public/otel",
                headers="Authorization=Basic ENV",
                owner="langfuse_otel",
            ),
        ],
    )
    new_cfg = cache._config_with_destination(
        _dest(endpoint="https://cloud.langfuse.com/api/public/otel", auth="Basic TEAM")
    )
    by_owner = {e.owner: e for e in new_cfg.exporters}
    assert (
        by_owner["langfuse_otel"].endpoint
        == "https://cloud.langfuse.com/api/public/otel"
    )
    assert by_owner["langfuse_otel"].headers == "Authorization=Basic TEAM"
    # Co-configured backends are untouched.
    assert by_owner[None].endpoint == "http://self-hosted-collector:4318"
    assert by_owner[None].headers == "x=base-collector"
    assert by_owner["arize"].headers == "space_id=base,api_key=base"


# --- the security lock: request credentials never route a trace ------------- #


@pytest.mark.parametrize(
    "callback_name, request_creds",
    [
        (
            "langfuse_otel",
            {
                "langfuse_public_key": "pk-attacker",
                "langfuse_secret_key": "sk-attacker",
                "langfuse_host": "https://attacker.example",
            },
        ),
        ("arize", {"arize_api_key": "K-attacker", "arize_space_id": "S-attacker"}),
        (
            "weave_otel",
            {
                "wandb_api_key": "w-attacker",
                "weave_project_id": "p/attacker",
                "weave_endpoint": "https://attacker.example/otel",
            },
        ),
    ],
)
def test_request_credentials_are_inert_on_v2(callback_name, request_creds):
    """Any backend's credentials in the request's dynamic params (no admin
    destination) must NOT produce a per-tenant destination, so the trace is not
    redirected. Parametrized across langfuse, arize, and weave."""
    event = LLMCallEvent.from_dict(
        {
            "standard_callback_dynamic_params": request_creds,
            "call_type": "acompletion",
            "model": "gpt-4o",
        }
    )
    assert event.otel_destination is None
    cache = _cache(callback_name)
    default = NoOpTracer()
    assert cache.tracer_for(default, event.otel_destination) is default
    assert cache._providers == {}


def test_admin_destination_routes():
    event = LLMCallEvent.from_dict(
        {
            "standard_callback_dynamic_params": {
                "otel_destination": {
                    "endpoint": "https://cloud.langfuse.com/api/public/otel",
                    "headers": {"Authorization": "Basic ADMIN"},
                }
            },
            "call_type": "acompletion",
            "model": "gpt-4o",
        }
    )
    assert event.otel_destination is not None
    assert (
        event.otel_destination.endpoint == "https://cloud.langfuse.com/api/public/otel"
    )
    cache = _cache("langfuse_otel")
    cache.tracer_for(NoOpTracer(), event.otel_destination)
    assert len(cache._providers) == 1
