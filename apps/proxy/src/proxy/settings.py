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
        self.backend: str = os.environ.get("LMW_BACKEND", "gguf")
        self.trace: bool = (os.environ.get("LMW_TRACE", "") == "1"
                            or os.environ.get("LMW_TRACE_FILE", "") != "")
        self.model_map: dict[str, str] = _model_map()


def get_settings() -> Settings:
    return Settings()
