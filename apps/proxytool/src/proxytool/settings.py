"""Proxy settings, driven by LMW_* / LTP_* environment variables + CLI flags."""

from __future__ import annotations

import json
import os


def _model_map() -> dict[str, str]:
    """Client-facing model id → real upstream model id.

    Sources: LMW_MODEL_MAP (inline JSON object) or LMW_MODEL_MAP_FILE (path
    to a JSON object). The bundled opencode configs use friendly ids
    (`qwen-gguf`, `qwen-mlx`) that llama-server ignores but mlx_lm.server
    resolves against HF — producing "Repository Not Found". With no mapping
    configured, the alias passes through unchanged.
    """
    raw = os.environ.get("LTP_MODEL_MAP") or os.environ.get("LMW_MODEL_MAP") or ""
    path = os.environ.get("LTP_MODEL_MAP_FILE") or os.environ.get("LMW_MODEL_MAP_FILE") or ""
    data: dict[str, str] = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data.update(json.load(f))
    elif raw:
        data.update(json.loads(raw))
    return data


def _env(name: str, default: str) -> str:
    """LTP_* wins over the legacy LMW_* spelling."""
    return os.environ.get(f"LTP_{name}") or os.environ.get(f"LMW_{name}", default)


class Settings:
    def __init__(self) -> None:
        self.host: str = _env("HOST", "127.0.0.1")
        self.port: int = int(_env("PORT", "8180"))
        self.upstream_url: str = _env("UPSTREAM_URL", "http://127.0.0.1:8181")
        # Total seconds the middleware waits on an upstream request. Long
        # model loads / cold-starts (llama-swap spins a backend on demand)
        # need a large read window; SSE reads ride the read timeout too.
        self.upstream_timeout: float = float(_env("UPSTREAM_TIMEOUT", "300"))
        # Optional static bearer token sent as Authorization to the upstream
        # (for llama-swap gateways / llama-server --api-key). Client auth
        # headers are forwarded per-request separately in the conduit.
        self.upstream_api_key: str = _env("UPSTREAM_API_KEY", "")
        self.backend: str = _env("BACKEND", "gguf")
        self.trace: bool = (_env("TRACE", "") == "1" or _env("TRACE_FILE", "") != "")
        self.model_map: dict[str, str] = _model_map()
        # LoopGuard thresholds (0 disables that escape entirely).
        self.loop_nudge: int = int(_env("LOOP_NUDGE", "3") or 0)
        self.loop_abort: int = int(_env("LOOP_ABORT", "6") or 0)

    def upstream_url_changed_by_flag(self, flag_value: str | None) -> bool:
        """True when a --upstream-url CLI flag (or its env) already set a URL."""
        return bool(flag_value)


def get_settings() -> Settings:
    """Build Settings from env.

    Deliberately does NOT touch logging configuration: the CLI (or an
    explicit ``configure_logging`` call by the embedder) owns that, and
    re-applying an env default here would clobber a CLI ``--log-level``.
    """
    return Settings()
