"""Trace: LMW_TRACE=1 dumps payload transit points to stderr for debugging."""

from __future__ import annotations

import json
import sys

_enabled = False


def enable(enabled: bool) -> None:
    global _enabled
    _enabled = enabled


def enabled() -> bool:
    return _enabled


def log(stage: str, payload: object) -> None:
    if not _enabled:
        return
    body = json.dumps(payload, indent=2, default=str)
    print(f"[{stage}]\n{body}\n", file=sys.stderr, flush=True)
