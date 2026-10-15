"""toolpatch logging: configure_logger, NullLogger, handler plumbing."""

from __future__ import annotations

import io
import logging

from toolpatch.logging import (
    LogLevels,
    NullLogger,
    _configure_app_handler,
    _configure_handler,
    _emit,
    _resolve_level,
    configure_logger,
    get_logger,
)


def test_resolve_level_matrix():
    assert _resolve_level("debug") == logging.DEBUG
    assert _resolve_level("info") == logging.INFO
    assert _resolve_level("warning") == logging.WARNING
    assert _resolve_level("error") == logging.ERROR


def test_configure_logger_none_returns_null_logger():
    assert isinstance(configure_logger("toolpatch.tmp-any", None), NullLogger)


def test_configure_logger_sets_threshold():
    logger = configure_logger("toolpatch.tmp-threshold", "error")
    assert logger.level == logging.ERROR
    logging.getLogger("toolpatch.tmp-threshold")  # it is a real logger


def test_null_logger_swallows_everything():
    nl = NullLogger()
    assert nl.debug("x", y=1) is None
    assert nl.info("x") is None
    assert nl.warning("x") is None
    assert nl.error("x") is None
    assert nl.exception("x") is None


def test_get_logger_returns_stdlib_logger():
    assert isinstance(get_logger("toolpatch.get-logger-test"), logging.Logger)


def test_configure_app_handler_idempotent():
    # _configure_app_handler is pinned to the shared "toolpatch" logger
    name = "toolpatch"
    logger = logging.getLogger(name)
    saved = logger.handlers[:]
    try:
        logger.handlers.clear()
        _configure_app_handler(level=logging.WARNING)
        assert len(logger.handlers) == 1
        assert logger.handlers[0].level == logging.WARNING
        # second call: handler already present → no duplicate
        _configure_app_handler(level=logging.DEBUG)
        assert len(logger.handlers) == 1
    finally:
        logger.handlers = saved


def test_configure_handler_dedupes_stderr_target():
    name = "toolpatch.tmp-dedupe"
    logger = logging.getLogger(name)
    saved = logger.handlers[:]
    try:
        logger.handlers.clear()
        _configure_handler(logger, logging.INFO)
        first = logger.handlers[:]
        _configure_handler(logger, logging.INFO)  # has_target → early return
        assert logger.handlers == first
    finally:
        logger.handlers = saved


def test_emit_returns_without_handlers():
    name = "toolpatch.emit"
    logger = logging.getLogger(name)
    saved = logger.handlers[:]
    try:
        logger.handlers.clear()
        record = logging.LogRecord(
            name=name, level=logging.INFO, pathname="p", lineno=1,
            msg="not emitted", args=(), exc_info=None)
        assert _emit(record) is None
    finally:
        logger.handlers = saved


def test_configure_handler_appends_without_dedup_across_loggers():
    name = "toolpatch.tmp-configure-handler"
    logger = logging.getLogger(name)
    saved = logger.handlers[:]
    try:
        logger.handlers.clear()
        _configure_handler(logger, logging.INFO)
        assert len(logger.handlers) == 1
    finally:
        logger.handlers = saved


def test_emit_routes_record_to_real_handler():
    stream = io.StringIO()
    name = "toolpatch.emit"
    logger = logging.getLogger(name)
    saved = logger.handlers[:]
    try:
        logger.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        record = logging.LogRecord(
            name="toolpatch.emit", level=logging.WARNING, pathname="p",
            lineno=1, msg="emitted via _emit", args=(), exc_info=None)
        _emit(record)
        assert "emitted via _emit" in stream.getvalue()
    finally:
        logger.handlers = saved


def test_loglevels_literal_members():
    # exported tier strings used by proxytool too
    assert set(LogLevels.__args__) == {"debug", "info", "warning", "error"}
