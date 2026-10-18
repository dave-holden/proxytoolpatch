"""Backend routing table: which upstream does a request hit.

Backends can be declared on the CLI via repeatable ``--upstream NAME=URL``
flags or through ``PTP_UPSTREAM_<NAME>`` / ``PTP_UPSTREAM_DEFAULT`` env vars
(environment wins unless the flag is passed explicitly). Each backend carries
an optional comma-separated model list — ``--upstream gguf=http://...:8181
--upstream-models gguf=qwen-gguf,qwen`` — and the client-facing ``model`` id is
matched against it first, then against ``PTP_DEFAULT_BACKEND`` / the flag
default. Exactly one registered backend receives everything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    models: tuple[str, ...] = ()  # client-facing model ids this backend serves
    default: bool = False


@dataclass(frozen=True)
class RouteDecision:
    backend: Backend
    matched_by: str  # "model-list" | "default" | "only-backend"


@dataclass
class BackendRouter:
    backends: dict[str, Backend] = field(default_factory=dict)

    def register(self, backend: Backend) -> None:
        if backend.name in self.backends:
            raise ValueError(f"duplicate backend name: {backend.name}")
        self.backends[backend.name] = backend

    @property
    def default(self) -> Backend | None:
        defaults = [b for b in self.backends.values() if b.default]
        if len(defaults) == 1:
            return defaults[0]
        if defaults:
            raise ValueError("multiple backends flagged default")
        return next(iter(self.backends.values()), None)

    def route(self, model: str | None) -> RouteDecision:
        model = (model or "").strip().lower()
        candidates = [b for b in self.backends.values() if b.models]
        for b in candidates:
            if model in {m.lower() for m in b.models}:
                return RouteDecision(b, "model-list")
        d = self.default
        if d is None:
            raise RuntimeError("no backends registered")
        if len(self.backends) == 1:
            return RouteDecision(d, "only-backend")
        return RouteDecision(d, "default")


# Var suffixes under PTP_UPSTREAM_* that are single-upstream *settings*, not
# named routes: PTP_UPSTREAM_URL / _DEFAULT / _API_KEY / _TIMEOUT.
# Everything else in that namespace is a route name (PTP_UPSTREAM_<NAME>=URL,
# PTP_UPSTREAM_<NAME>_MODELS=m1,m2).
RESERVED_ROUTE_SUFFIXES = ("URL", "DEFAULT", "API_KEY", "TIMEOUT")


def backends_from_env() -> dict[str, Backend]:
    """Build backends from PTP_UPSTREAM_<NAME> / _MODELS / _DEFAULT env vars."""
    out: dict[str, Backend] = {}
    for var, val in os.environ.items():
        if not var.startswith("PTP_UPSTREAM_"):
            continue
        rest = var[len("PTP_UPSTREAM_"):]
        if rest.endswith("_MODELS"):
            continue
        if rest in RESERVED_ROUTE_SUFFIXES:
            continue
        name = rest.lower().replace("-", "_")
        models_var = f"PTP_UPSTREAM_{rest}_MODELS"
        models = tuple(
            m.strip() for m in os.environ.get(models_var, "").split(",") if m.strip()
        )
        out[name] = Backend(
            name=name,
            url=val.rstrip("/"),
            models=models,
            default=os.environ.get("PTP_DEFAULT_BACKEND", "").lower() == name,
        )
    return out


def backends_from_args(upstreams: list[str], models: dict[str, str],
                       default_backend: str | None) -> dict[str, Backend]:
    """Build backends from repeatable --upstream NAME=URL and --upstream-models NAME=a,b."""
    out: dict[str, Backend] = {}
    for spec in upstreams:
        name, _, url = spec.partition("=")
        name = name.strip().lower()
        if not name or not url:
            raise ValueError(f"--upstream expects NAME=URL, got: {spec!r}")
        out[name] = Backend(
            name=name,
            url=url.rstrip("/"),
            models=tuple(
                m.strip() for m in models.get(name, "").split(",") if m.strip()
            ),
            default=(default_backend or "").lower() == name,
        )
    return out
