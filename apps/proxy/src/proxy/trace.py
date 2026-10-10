"""Trace: LMW_TRACE=1 dumps payload transit points to stderr for debugging."""

from __future__ import annotations

import json
import os
import sys
from typing import TextIO

_enabled = False
_trace_file: TextIO | None = None


def enable(enabled: bool) -> None:
    global _enabled
    _enabled = enabled


def enabled() -> bool:
    return _enabled or _trace_file is not None


# SIM115 is intentional: the trace sink lives for the process lifetime and is
# cleaned up when the uvicorn process exits; no context manager applies.
def _get_sink() -> TextIO:
    global _trace_file
    if _trace_file is None:
        path = os.environ.get("LMW_TRACE_FILE")
        sink: TextIO = (
            open(path, "a", encoding="utf-8") if path else sys.stderr  # noqa: SIM115
        )
        _trace_file = sink
    return _trace_file


def log(stage: str, payload: object) -> None:
    if not (enabled() or os.environ.get("LMW_TRACE_FILE")):
        return
    body = f"[{stage}]\n{json.dumps(payload, indent=2, default=str)}\n"
    print(body, file=_get_sink(), flush=True)
