"""Per-request multi-tenant tracer routing.

When a call's key/team is bound to an admin-owned OTEL destination
(``LLMCallEvent.otel_destination``, resolved server-side from a named credential),
its spans must export through a ``TracerProvider`` pointed at that destination's
endpoint with its auth headers. ``TenantTracerCache`` builds and caches one
provider per distinct destination, and otherwise hands back the logger's default
tracer. This lets a single logger fan requests out to many tenants without a
logger per tenant. The destination is never request-derived, so a caller can
neither redirect a trace nor spawn providers.
"""

from collections import OrderedDict

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from litellm._logging import verbose_logger
from litellm.integrations.otel.model.config import OpenTelemetryV2Config
from litellm.integrations.otel.model.destination import OtelDestination
from litellm.integrations.otel.plumbing.providers import (
    build_tracer_provider,
    get_tracer,
)

# Exporter kinds that ignore endpoint/headers — never rewritten with a destination.
_NON_OTLP_KINDS = ("console", "in_memory", "inmemory", "memory")

# Cap on distinct destination-scoped providers held at once. Destinations are
# admin-owned (one per key/team), so this is resource hygiene rather than an
# anti-abuse bound: it keeps the working set of active tenants resident while
# flushing and shutting down evicted providers so their exporter threads are
# reclaimed.
_MAX_CACHED_PROVIDERS = 256


def _shutdown_provider(provider: TracerProvider) -> None:
    """Flush + stop an evicted provider's processors (reclaims their threads).

    ``TracerProvider.shutdown`` force-flushes each ``SpanProcessor`` before
    stopping it, so any spans already handed to a ``BatchSpanProcessor`` are
    exported rather than dropped. Best-effort: a shutdown failure must not break
    the request that triggered the eviction.
    """
    try:
        provider.shutdown()
    except Exception as e:  # pragma: no cover - defensive
        verbose_logger.debug("OTel V2: error shutting down evicted provider: %s", e)


class TenantTracerCache:
    """Destination-scoped ``TracerProvider`` cache keyed by endpoint + headers."""

    def __init__(
        self,
        config: OpenTelemetryV2Config,
        callback_name: str | None,
        tracer_name: str,
    ) -> None:
        self._config = config
        self._callback_name = callback_name
        self._tracer_name = tracer_name
        self._providers: (
            "OrderedDict[tuple[str, tuple[tuple[str, str], ...]], TracerProvider]"
        ) = OrderedDict()

    def tracer_for(
        self, default: Tracer, destination: OtelDestination | None
    ) -> Tracer:
        """Return the tracer for this request.

        Use ``default`` unless an admin-owned destination is bound to the call's
        key/team, in which case build (or reuse) a provider that exports to it. The
        cache is a bounded LRU: the least-recently-used provider is flushed and shut
        down on overflow so its exporter threads don't accumulate.
        """
        if destination is None:
            return default
        cache_key = (destination.endpoint, tuple(sorted(destination.headers.items())))
        provider = self._providers.get(cache_key)
        if provider is not None:
            self._providers.move_to_end(cache_key)
        else:
            provider = build_tracer_provider(self._config_with_destination(destination))
            self._providers[cache_key] = provider
            if len(self._providers) > _MAX_CACHED_PROVIDERS:
                _, evicted = self._providers.popitem(last=False)
                _shutdown_provider(evicted)
        return get_tracer(provider, self._tracer_name)

    def _config_with_destination(
        self, destination: OtelDestination
    ) -> OpenTelemetryV2Config:
        """Clone the config, pointing this integration's own exporter at ``destination``.

        The endpoint and headers apply only to the exporter this integration
        contributed (``spec.owner == self._callback_name``), so a tenant's
        destination never rewrites a co-configured backend's exporter. Setting the
        endpoint per destination (not just the headers) is what makes a tenant on a
        different host export there instead of hitting the env host and being dropped.
        """
        update = {
            "endpoint": destination.endpoint,
            "headers": destination.header_string(),
        }
        exporters = [
            (
                spec.model_copy(update=update)
                if spec.owner == self._callback_name
                and spec.kind.lower() not in _NON_OTLP_KINDS
                else spec
            )
            for spec in self._config.exporters
        ]
        return self._config.model_copy(update={"exporters": exporters})
