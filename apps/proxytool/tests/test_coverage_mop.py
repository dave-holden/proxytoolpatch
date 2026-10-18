"""Mop-up branch tests to lift remaining modules past 90% coverage."""

from __future__ import annotations

import urllib.request

import pytest
from test_cli_cmds import _FakeChild, _FakeResp, _parse

import proxytool.cli as cli
import proxytool.config as config
from proxytool.trace import _NoopLogger


def _load(p):
    return config.load_config(p)


# --------------------------------------------------------------- config.py
def test_find_config_explicit_missing_raises(tmp_path):
    with pytest.raises(ValueError, match="config file not found"):
        config.find_config(str(tmp_path / "nope.toml"))


def test_find_config_env_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PTP_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ValueError, match="missing file"):
        config.find_config(None)


def test_load_invalid_toml_raises(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("[host\n", encoding="utf-8")  # broken TOC
    with pytest.raises(ValueError) as excinfo:
        _load(p)
    assert "invalid TOML" in str(excinfo.value)


def test_search_candidates_explicit_and_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PTP_CONFIG", str(tmp_path / "via-env.toml"))
    assert [p.name for p in config._search_candidates(None)] == ["via-env.toml"]
    monkeypatch.delenv("PTP_CONFIG")
    assert str(tmp_path / "explicit.toml") in str(config._search_candidates(
        str(tmp_path / "explicit.toml")))


def test_detail_for_map():
    assert config._detail_for("PTP_LOOP_ABORT") == config._SEARCH_DETAIL.get(
        "PTP_LOOP_ABORT", "")
    assert config._detail_for("PTP_UPSTREAM_MLX_MODELS") == "routing.mlx.models"
    assert config._detail_for("PTP_UPSTREAM_MLX") == "routing.mlx.url"
    assert config._detail_for("SOME_RANDOM_KEY") == ""


def test_relevant_env_keys_filters(monkeypatch):
    monkeypatch.setenv("PTP_TRACE", "1")
    monkeypatch.setenv("UNRELATED", "x")
    keys = config._relevant_env_keys(["PTP_TRACE", "PTP_CONFIG", "UNRELATED"])
    assert "PTP_TRACE" in keys
    assert "PTP_CONFIG" not in keys
    assert not any(k == "UNRELATED" for k in keys)


# --------------------------------------------------------------- trace.py
def test_noop_logger_methods():
    noop = _NoopLogger()
    assert noop.debug("x") is None
    assert noop.info("x") is None
    assert noop.warning("x") is None
    assert noop.error("x") is None


# --------------------------------------------------------------- cli.py
@pytest.fixture
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PTP_STATE_PATH", str(tmp_path / "state.json"))
    return tmp_path


def test_cmd_serve_bad_config_exits_2(tmp_path, capsys):
    cfg = tmp_path / "broken.toml"
    cfg.write_text("log_level = 'shout'\n", encoding="utf-8")  # invalid choice
    args = _parse(["serve", "--config", str(cfg)])
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_serve(args)
    assert excinfo.value.code == 2


# main()'s `handler is None` branch is unreachable through argparse (the
# subcommand is required and the map covers every command) — not tested.


def test_configure_logging_cli_keyword_only_success():
    cli._configure_logging_cli(level="info", quiet=False)  # runs without raising


def test_cmd_start_argv_carries_all_flags(state_env, monkeypatch):
    captured: dict = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return _FakeChild()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(200))
    args = _parse([
        "start",
        "--upstream-url", "http://127.0.0.1:9998",
        "--model-map", '{"a": "b"}', "--upstream-api-key", "sk-x",
        "--upstream-timeout", "5", "--default-backend", "fake",
        "--upstream", "gguf=http://127.0.0.1:9998",
        "--upstream-models", "gguf=qwen-gguf", "--trace",
        "--loop-nudge", "2", "--loop-abort", "4", "--log-level", "debug",
    ])
    assert cli.cmd_start(args) == 0
    argv = captured["argv"]
    for expected in ("--upstream-url", "--model-map", "--upstream-api-key",
                     "--upstream-timeout", "5", "--default-backend",
                     "--trace", "--loop-nudge", "2",
                     "--loop-abort", "4", "--log-level", "debug"):
        assert expected in argv
    assert "--quiet" not in argv


def test_cmd_start_arg_quiet_flag(state_env, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("subprocess.Popen",
                        lambda argv, **kw: (captured.update(argv=argv),
                                            _FakeChild())[1])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(200))
    args = _parse(["start", "--quiet"])
    assert cli.cmd_start(args) == 0
    assert "--quiet" in captured["argv"]
