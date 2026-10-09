"""Proxy settings, driven by LMW_* environment variables."""

from __future__ import annotations

import os


class Settings:
    def __init__(self) -> None:
        self.upstream_url: str = os.environ.get(
            "LMW_UPSTREAM_URL", "http://127.0.0.1:8181"
        )
        self.backend: str = os.environ.get("LMW_BACKEND", "gguf")
        self.trace: bool = os.environ.get("LMW_TRACE", "") == "1"


def get_settings() -> Settings:
    return Settings()
