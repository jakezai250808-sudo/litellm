"""The resolved, admin-owned OTLP destination.

A trace destination is admin-owned infrastructure config, never request data.
The proxy resolves a key/team's bound named credential into this typed,
backend-agnostic target (an endpoint plus auth headers) server-side, and the v2
logger exports through it. Every OTEL backend -- Langfuse, Arize, Weave, a
self-hosted collector -- reduces to this shape; the per-backend field mapping
lives in ``litellm.integrations.otel.destinations``.
"""

from pydantic import BaseModel, ConfigDict, Field


class OtelDestination(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: str
    headers: dict[str, str] = Field(default_factory=dict)

    def header_string(self) -> str:
        """Render headers as the ``k=v,k2=v2`` form an ``ExporterSpec`` expects."""
        return ",".join(f"{key}={value}" for key, value in self.headers.items())
