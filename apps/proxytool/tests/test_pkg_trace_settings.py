"""Branch tests for proxytool/__init__ logging config, trace module, settings."""

from __future__ import annotations

import json
import logging
import sys

import pytest

import proxytool.settings as settings
import proxytool.trace as trace


# --------------------------------------------------------------- __init__.py
def _saved_handlers(*names):
    """(logger, saved-handlers) pairs for save/restore around a test."""
    return [(logging.getLogger(n), logging.getLogger(n).handlers[:])
            for n in names]


def _restore_all(pairs):
    for lg, saved in pairs:
        lg.handlers = saved


def test_configure_logging_quiet_forces_error_level():
    pairs = _saved_handlers("proxytool", "toolpatch")
    for lg, _ in pairs:
        lg.handlers = []
    try:
        from proxytool import configure_logging
        configure_logging(quiet=True)
        assert logging.getLogger("proxytool").level == logging.ERROR
        assert logging.getLogger("toolpatch").level == logging.ERROR
    finally:
        _restore_all(pairs)


def test_configure_logging_env_falls_back(monkeypatch):
    monkeypatch.setenv("LTP_LOG_LEVEL", "warning")
    pairs = _saved_handlers("proxytool", "toolpatch")
    for lg, _ in pairs:
        lg.handlers = []
    try:
        from proxytool import configure_logging
        configure_logging(level=None, quiet=False)
        assert logging.getLogger("proxytool").level == logging.WARNING
    finally:
        _restore_all(pairs)


def test_configure_logging_invalid_env_level_raises(monkeypatch):
    monkeypatch.setenv("LTP_LOG_LEVEL", "bogus")
    pairs = _saved_handlers("proxytool", "toolpatch")
    for lg, _ in pairs:
        lg.handlers = []
    try:
        from proxytool import configure_logging
        with pytest.raises(ValueError, match="unsupported log level"):
            configure_logging(level=None, quiet=False)
    finally:
        _restore_all(pairs)


def test_configure_logging_survives_toolpatch_failures(monkeypatch):
    pairs = _saved_handlers("proxytool", "toolpatch")
    for lg, _ in pairs:
        lg.handlers = []
    import toolpatch.logging as tplog

    real_cfg = tplog.configure_logger
    monkeypatch.setattr(tplog, "configure_logger",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        from proxytool import configure_logging
        configure_logging(level="debug", quiet=False)  # must not raise
        assert logging.getLogger("proxytool").level == logging.DEBUG
    finally:
        monkeypatch.setattr(tplog, "configure_logger", real_cfg)
        _restore_all(pairs)


def test_resolve_level_unknown_raises():
    from proxytool import _resolve_level
    with pytest.raises(ValueError):
        _resolve_level("banana")


# --------------------------------------------------------------- trace.py
def test_trace_disabled_by_default_logs_nothing(monkeypatch, capsys):
    monkeypatch.delenv("LMW_TRACE_FILE", raising=False)
    trace.enable(False)
    trace._trace_log = None
    trace._trace_file = None
    trace.log("stage", {"x": 1})
    assert capsys.readouterr().out == ""


def test_trace_enabled_prints_payload_and_via_logger(monkeypatch, capsys):
    trace.enable(True)
    trace._trace_file = None
    logger = logging.getLogger("trace-shot")
    logger.handlers.clear()
    received: list[logging.LogRecord] = []

    class Rec(logging.Handler):
        def emit(self, record):
            received.append(record)

    logger.addHandler(Rec(level=logging.DEBUG))
    logger.setLevel(logging.DEBUG)
    trace.attach_logger(logger)
    try:
        trace.log("stage-x", {"a": "b"})
        assert "stage-x" in capsys.readouterr().err  # default sink is stderr
        assert received  # the app logger saw a record too
    finally:
        trace.enable(False)
        trace._trace_log = None


def test_trace_file_sink_created(monkeypatch, tmp_path):
    sink = tmp_path / "trace.log"
    monkeypatch.setenv("LMW_TRACE_FILE", str(sink))
    trace.enable(True)
    trace._trace_file = None
    trace._trace_log = None
    try:
        trace.log("file-shot", {"z": 2})
        assert "file-shot" in sink.read_text()
    finally:
        trace.enable(False)
        # close our next-file sink if created
        if trace._trace_file not in (None, sys.stderr):
            trace._trace_file.close()
        trace._trace_file = None


def test_trace_enabled_flag_toggle():
    trace.enable(False)
    trace._trace_file = None
    assert trace.enabled() is False
    trace.enable(True)
    assert trace.enabled() is True
    trace.enable(False)


# --------------------------------------------------------------- settings.py
def test_model_map_from_env(monkeypatch):
    monkeypatch.setenv("LTP_MODEL_MAP", '{"alias": "real-id"}')
    monkeypatch.delenv("LTP_MODEL_MAP_FILE", raising=False)
    monkeypatch.delenv("LMW_MODEL_MAP_FILE", raising=False)
    assert settings._model_map() == {"alias": "real-id"}


def test_model_map_file_wins(monkeypatch, tmp_path):
    f = tmp_path / "map.json"
    f.write_text(json.dumps({"from-file": "model-x"}), encoding="utf-8")
    monkeypatch.setenv("LTP_MODEL_MAP_FILE", str(f))
    monkeypatch.setenv("LTP_MODEL_MAP", '{"from-env": "ignored"}')
    assert settings._model_map() == {"from-file": "model-x"}


def test_model_map_none():
    import os as _os
    saved = {k: _os.environ.get(k) for k in
             ("LTP_MODEL_MAP", "LMW_MODEL_MAP", "LTP_MODEL_MAP_FILE", "LMW_MODEL_MAP_FILE")}
    for k in saved:
        _os.environ.pop(k, None)
    try:
        assert settings._model_map() == {}
    finally:
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v


def test_settings_loop_flags_zero_disables():
    import os as _os
    saved = {k: _os.environ.get(k) for k in ("LTP_LOOP_NUDGE", "LTP_LOOP_ABORT")}
    _os.environ["LTP_LOOP_NUDGE"] = "0"
    _os.environ["LTP_LOOP_ABORT"] = "0"
    try:
        s = settings.get_settings()
        assert s.loop_nudge == 0 and s.loop_abort == 0
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
