"""SRV-OBS: tracing for the api and worker roles, behind a declared attribute allow-list
(spec/modules/SRV-OBS.yaml).

This package imports only the OpenTelemetry API, so any module of it can be imported without the SDK installed
(the CLI canary harness imports `reader.obs.gate_rules` in a bare image). `setup` installs the SDK provider.
"""

from __future__ import annotations

from typing import Any, Final

from opentelemetry import trace

SCOPE: Final = "reader"
_STATE: dict[str, Any] = {"provider": None}


def tracer() -> trace.Tracer:
    """The tracer of the provider `setup` installed, or OTel's no-op tracer when tracing is off or unset."""
    provider = _STATE["provider"]
    if provider is None:
        return trace.NoOpTracer()
    result: trace.Tracer = provider.get_tracer(SCOPE)
    return result


def install(provider: Any) -> None:
    """Make `provider` the one `tracer()` uses (None: tracing off). Called by `reader.obs.setup` only."""
    _STATE["provider"] = provider
