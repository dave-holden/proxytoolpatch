"""Trace: PTP_TRACE=1 dumps payload transit points to stderr for debugging.

Optionally, an app-level logger can be injected (``attach_logger``); when
present, trace payloads are also emitted at the DEBUG level through it so
they show up in the configured logging pipeline.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, TextIO

_enabled = False
_trace_file: TextIO | None = None
_trace_log: Any = None  # set to a real logger via attach_logger()


class _NoopLogger:
    """Silent stand-in used until/independent of an injected logger."""

    def debug(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None


def _noop_logger() -> _NoopLogger:
    return _NoopLogger()


def enable(enabled: bool) -> None:
    global _enabled
    _enabled = enabled


def enabled() -> bool:
    return _enabled or _trace_file is not None


def attach_logger(logger: Any) -> None:
    """Inject an app-level logger into the trace dumper.

    When set, trace payloads are also emitted through the app logger at the
    DEBUG level instead of only the raw stderr sink.
    """
    global _trace_log
    _trace_log = logger


# SIM115 is intentional: the trace sink lives for the process lifetime and is
# cleaned up when the uvicorn process exits; no context manager applies.
def _get_sink() -> TextIO:
    global _trace_file
    if _trace_file is None:
        path = os.environ.get("PTP_TRACE_FILE")
        sink: TextIO = (
            open(path, "a", encoding="utf-8") if path else sys.stderr  # noqa: SIM115
        )
        _trace_file = sink
    return _trace_file


def log(stage: str, payload: object) -> None:
    if not (enabled() or os.environ.get("PTP_TRACE_FILE")):
        return
    body = f"[{stage}]\n{json.dumps(payload, indent=2, default=str)}\n"
    print(body, file=_get_sink(), flush=True)
    if _trace_log is not None:
        # Logging must never break payload forwarding.
        with contextlib.suppress(Exception):
            _trace_log.debug("trace %s: %s", stage, json.dumps(payload, default=str))
