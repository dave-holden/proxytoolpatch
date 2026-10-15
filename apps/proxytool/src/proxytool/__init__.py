"""proxytool: FastAPI middleware conduit standing between agent clients and Qwen servers."""

import logging as _logging


def configure_logging(*, level: str | None = None, quiet: bool = False) -> None:
    """Configure app-level logging for proxytool modules.

    ``level`` selects the app logger threshold from the standard tiers
    ``debug``, ``info``, ``warning``, or ``error``. When omitted the
    environment variable ``LTP_LOG_LEVEL`` (or its legacy ``LMW_LOG_LEVEL``
    spelling) is used, falling back to ``info``.

    ``quiet`` suppresses non-error app output to stderr by raising the app
    logger threshold to ``error``.

    This sets up a simple stderr handler so app-level log lines (server
    lifecycle, loop-guard notes, toolpatch-internal debug) are emitted
    consistently. Uvicorn/FastAPI server logging is still configured
    separately by the CLI via ``uvicorn.run(log_level=...)``.
    """
    import os

    from toolpatch.logging import LogLevels as _LogLevels

    _tier_map: dict[str, _LogLevels | None] = {
        "debug": "debug",
        "info": "info",
        "warning": "warning",
        "error": None,  # errors-only: hand toolpatch a no-op logger
    }

    if quiet:
        effective: str = "error"
    elif level:
        effective = level
    else:
        effective = os.environ.get("LTP_LOG_LEVEL") or os.environ.get("LMW_LOG_LEVEL") or "info"

    _level = _resolve_level(effective)
    _app = _logging.getLogger("proxytool")
    _app.setLevel(_level)
    _configure_handler(_app, _level)
    _configure_handler(_logging.getLogger("toolpatch"), _level)

    # toolpatch uses its own logger names; mirror the same threshold into the
    # shared library so its internal debug lines are visible at the same tier.
    try:
        from toolpatch.logging import configure_logger as _toolpatch_configure

        _toolpatch_configure("toolpatch", _tier_map.get(effective))
    except Exception:
        # toolpatch logging setup is best-effort; never let it break startup.
        pass

    # Mirror app threshold into toolpatch's shared logger too (it exposes
    # ``get_logger`` which returns a stdlib logger once configured).
    try:
        from toolpatch import _configure_logging as _toolpatch_logging

        _toolpatch_logging(level=effective, quiet=False)
    except Exception:
        pass


def _resolve_level(raw: str) -> int:
    mapping = {
        "debug": _logging.DEBUG,
        "info": _logging.INFO,
        "warning": _logging.WARNING,
        "error": _logging.ERROR,
    }
    normalized = raw.strip().lower()
    if normalized not in mapping:
        raise ValueError(
            f"unsupported log level {raw!r}; expected one of {list(mapping)}"
        )
    return mapping[normalized]


def _configure_handler(logger: _logging.Logger, level: int) -> None:
    import sys

    _ensure_handler(logger, level, stream=sys.stderr)


def _ensure_handler(logger: _logging.Logger, level: int, *, stream) -> None:
    import logging as _logging

    has_target = any(
        isinstance(h, _logging.StreamHandler) and getattr(h, "stream", None) is stream
        for h in logger.handlers
    )
    if has_target:
        return
    handler = _logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(_logging.Formatter(
        "%(levelname)s [%(name)s] %(message)s",
    ))
    logger.addHandler(handler)
