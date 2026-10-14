"""Lightweight logging support for toolpatch.

toolpatch is a pure protocol-repair library. It does not configure the
Python logging system itself; consumers (proxytool) do that at app startup.

This module provides:

* ``configure_logger(name, level)`` — create/return a logger for ``name`` at
  an explicit level, or a no-op logger when ``level`` is None.
* ``NullLogger`` — a drop-in logger whose methods are no-ops.
* ``LogLevels`` — the accepted tier strings shared by proxytool and toolpatch.
"""

from __future__ import annotations

from typing import Any, Literal

LogLevels = Literal["debug", "info", "warning", "error"]


def configure_logger(name: str, level: LogLevels | None) -> Any:
    """Return a logger for ``name`` when ``level`` is set, else a no-op logger.

    When a level is supplied, the underlying stdlib logger is created and its
    threshold is set. It is intentionally not attached to any handler here —
    the host app (proxytool) owns handler/transport configuration so that
    logging can be routed alongside Uvicorn/FastAPI output whichever way the
    app wants.
    """
    if level is None:
        return NullLogger()

    import logging

    logger = logging.getLogger(name)
    logger.setLevel(_resolve_level(level))
    return logger


def get_logger(name: str = "toolpatch") -> Any:
    """Shared logger for toolpatch internals.

    Always returns the real stdlib logger for ``name``. Before the host app
    calls ``configure_logger``, it has no handlers and the default root
    level drops debug/info lines silently; afterwards, the app-configured
    level and handlers take over.
    """
    import logging

    return logging.getLogger(name)


def _configure_app_handler(*, level: int) -> None:
    """Best-effort handler setup for the shared ``toolpatch`` logger.

    Called by proxytool's ``configure_logging`` so toolpatch debug lines are
    routed through the same stderr stream/progress as app output. When
    logging is already configured elsewhere, this is a no-op.
    """
    import logging as _logging

    logger = _logging.getLogger("toolpatch")
    if logger.handlers:
        return
    _configure_handler(logger, level)


def _configure_handler(logger: Any, level: int) -> None:
    """Attach a stderr handler to ``logger`` when one is not already present."""
    import logging as _logging
    import sys as _sys

    has_target = any(
        isinstance(h, _logging.StreamHandler) and getattr(h, "stream", None) is _sys.stderr
        for h in logger.handlers
    )
    if has_target:
        return
    handler = _logging.StreamHandler(_sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_logging.Formatter(
        "%(levelname)s [%(name)s] %(message)s",
    ))
    logger.addHandler(handler)


def _emit(record: Any) -> None:
    """Best-effort emit through the real stdlib logger when it has handlers."""
    import logging as _logging

    logger = _logging.getLogger("toolpatch.emit")
    if not logger.handlers:
        return
    logger.handle(record)


class NullLogger:
    """No-op logger used by toolpatch when no app-level logger has been injected.

    Callers can freely call ``debug``/``info``/``warning``/``error`` without
    guarding on ``isEnabledFor``.
    """

    def debug(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None

    def exception(self, *args: Any, **kwargs: Any) -> None:
        return None


def _resolve_level(level: LogLevels) -> int:
    import logging

    mapping = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    return mapping[level]
