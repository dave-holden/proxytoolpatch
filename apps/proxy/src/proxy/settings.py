"""Proxy settings, driven by LMW_* environment variables."""

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
    raw = os.environ.get("LMW_MODEL_MAP") or ""
    path = os.environ.get("LMW_MODEL_MAP_FILE") or ""
    data: dict[str, str] = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data.update(json.load(f))
    elif raw:
        data.update(json.loads(raw))
    return data


class Settings:
    def __init__(self) -> None:
        self.upstream_url: str = os.environ.get(
            "LMW_UPSTREAM_URL", "http://127.0.0.1:8181"
        )
        # Total seconds the middleware waits on an upstream request. Long
        # model loads / cold-starts (llama-swap spins a backend on demand)
        # need a large read window; SSE reads ride the read timeout too.
        self.upstream_timeout: float = float(
            os.environ.get("LMW_UPSTREAM_TIMEOUT", "300")
        )
        # Optional static bearer token sent as Authorization to the upstream
        # (for llama-swap gateways / llama-server --api-key). Client auth
        # headers are forwarded per-request separately in the conduit.
        self.upstream_api_key: str = os.environ.get("LMW_UPSTREAM_API_KEY", "")
        self.backend: str = os.environ.get("LMW_BACKEND", "gguf")
        self.trace: bool = (os.environ.get("LMW_TRACE", "") == "1"
                            or os.environ.get("LMW_TRACE_FILE", "") != "")
        self.model_map: dict[str, str] = _model_map()
        # LoopGuard thresholds (0 disables that escape entirely).
        self.loop_nudge: int = int(os.environ.get("LMW_LOOP_NUDGE", "3") or 0)
        self.loop_abort: int = int(os.environ.get("LMW_LOOP_ABORT", "6") or 0)


def get_settings() -> Settings:
    return Settings()
