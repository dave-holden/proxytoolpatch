from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from proxytool import cli as cli_mod
from proxytool.cli import _apply_toml_config
from proxytool.config import (
    apply_config,
    describe_config,
    find_config,
    load_config,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start from a clean LTP/LMW env AND undo the bridge's own mutations.

    The bridge mutates ``os.environ`` directly (that is its job at runtime);
    monkeypatch alone cannot undo that, so snapshot and restore explicitly to
    keep polluting later test modules.
    """
    for k in list(os.environ):
        if k.startswith(("LTP_", "LMW_", "XDG_CONFIG_HOME", "LTP_CONFIG")):
            monkeypatch.delenv(k, raising=False)
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _write_toml(tmp_path, text: str):
    p = tmp_path / "proxytool.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _args(**kw: Any):
    base: dict[str, Any] = {
        "config": None, "host": None, "port": None, "quiet": False,
        "log_level": None, "upstream_url": None, "backend": None,
        "model": None, "upstream_api_key": None, "upstream_timeout": None,
        "model_map": None, "model_map_file": None, "trace": False,
        "loop_nudge": None, "loop_abort": None, "default_backend": None,
        "upstream": [], "upstream_models": [], "managed": [], "no_managed": False,
    }
    base.update(kw)
    return type("A", (), base)()


class TestFindConfig:
    def test_explicit_path(self, tmp_path):
        p = _write_toml(tmp_path, "port = 1")
        assert find_config(str(p)) == p

    def test_explicit_missing_raises(self):
        with pytest.raises(ValueError, match="not found"):
            find_config("/nope/proxytool.toml")

    def test_ltp_config_env(self, tmp_path, monkeypatch):
        p = _write_toml(tmp_path, "port = 1")
        monkeypatch.setenv("LTP_CONFIG", str(p))
        assert find_config(None) == p

    def test_xdg_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "proxytool").mkdir()
        target = tmp_path / "proxytool" / "config.toml"
        target.write_text("port = 1", encoding="utf-8")
        monkeypatch.chdir(tmp_path)  # no cwd file, so XDG wins
        assert find_config(None) == target

    def test_cwd_beats_xdg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        (tmp_path / "xdg" / "proxytool").mkdir(parents=True)
        (tmp_path / "xdg" / "proxytool" / "config.toml").write_text("port = 1")
        p = _write_toml(tmp_path, "port = 2")
        monkeypatch.chdir(tmp_path)
        assert find_config(None) == p

    def test_none_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
        assert find_config(None) is None


class TestLoadConfig:
    def test_none_yields_empty(self):
        assert load_config(None) == {}

    def test_invalid_toml_raises(self, tmp_path):
        p = _write_toml(tmp_path, "not = [toml")
        with pytest.raises(ValueError, match="invalid TOML"):
            load_config(p)


class TestBridgeSettings:
    def test_bridge_fills_unset_values(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, (
            'host = "0.0.0.0"\n'
            "port = 9999\n"
            'log_level = "debug"\n'
            "[upstream]\n"
            'url = "http://127.0.0.1:8080/"\n'
            'backend = "mlx"\n'
            "timeout = 600\n"
            'api_key = "sk-t"\n'
            "[model_map]\n"
            '"a" = "b"\n'
            "[loopguard]\n"
            "nudge = 2\n"
            "abort = 4\n"
        ))
        path, _cfg, info = _apply_toml_config(_args(), serve=True)
        assert path == tmp_path / "proxytool.toml"
        assert os.environ["LTP_HOST"] == "0.0.0.0"
        assert os.environ["LTP_PORT"] == "9999"
        assert os.environ["LTP_LOG_LEVEL"] == "debug"
        assert os.environ["LTP_UPSTREAM_URL"] == "http://127.0.0.1:8080"
        assert os.environ["LTP_BACKEND"] == "mlx"
        assert os.environ["LTP_UPSTREAM_TIMEOUT"] == "600"
        assert os.environ["LTP_UPSTREAM_API_KEY"] == "sk-t"
        assert json.loads(os.environ["LTP_MODEL_MAP"]) == {"a": "b"}
        assert os.environ["LTP_LOOP_NUDGE"] == "2"
        assert os.environ["LTP_LOOP_ABORT"] == "4"
        assert "LTP_UPSTREAM_URL" in info["applied"]

    def test_flag_wins_over_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, 'port = 9999\n[upstream]\nurl = "http://cfg:1"')
        _path, _c, _i = _apply_toml_config(_args(port=8180, upstream_url="http://flag:2"),
                                          serve=True)
        assert os.environ.get("LTP_PORT", "8180") == "8180"
        assert "LTP_PORT" not in os.environ
        assert os.environ.get("LTP_UPSTREAM_URL") in (None, "http://flag:2")

    def test_env_wins_over_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LTP_UPSTREAM_TIMEOUT", "77")
        _write_toml(tmp_path, "[upstream]\ntimeout = 600")
        _apply_toml_config(_args(), serve=True)
        assert os.environ["LTP_UPSTREAM_TIMEOUT"] == "77"

    def test_legacy_lmw_env_also_blocks_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LMW_BACKEND", "gguf")
        _write_toml(tmp_path, '[upstream]\nbackend = "mlx"')
        _apply_toml_config(_args(), serve=True)
        assert os.environ["LMW_BACKEND"] == "gguf"

    def test_quiet_flag_blocks_config_log_level(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, 'log_level = "debug"')
        _apply_toml_config(_args(quiet=True), serve=True)
        assert "LTP_LOG_LEVEL" not in os.environ


class TestBridgeRouting:
    def test_routing_table_bridged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # NOTE: a top-level key placed after a [table] header becomes part of
        # that table — default_backend must come before the routing sections.
        _write_toml(tmp_path, (
            'default_backend = "mlx"\n'
            '[routing.gguf]\nurl = "http://127.0.0.1:8181/"\n'
            'models = ["qwen-gguf", "qwen"]\n'
            '[routing.mlx]\nurl = "http://127.0.0.1:8281"\n'
        ))
        _apply_toml_config(_args(), serve=True, routing=True)
        assert os.environ["LTP_UPSTREAM_GGUF"] == "http://127.0.0.1:8181"
        assert os.environ["LTP_UPSTREAM_GGUF_MODELS"] == "qwen-gguf,qwen"
        assert os.environ["LTP_UPSTREAM_MLX"] == "http://127.0.0.1:8281"
        assert os.environ["LTP_DEFAULT_BACKEND"] == "mlx"

    def test_per_entry_default_true(self, tmp_path, monkeypatch):
        """`default = true` on exactly one [routing.*] entry works standalone."""
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, (
            '[routing.gguf]\nurl = "http://127.0.0.1:8181"\n'
            '[routing.mlx]\nurl = "http://127.0.0.1:8281"\n'
            "default = true\n"
        ))
        _apply_toml_config(_args(), serve=True, routing=True)
        assert os.environ["LTP_DEFAULT_BACKEND"] == "mlx"

    def test_multiple_defaults_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, (
            '[routing.gguf]\nurl = "http://a"\ndefault = true\n'
            '[routing.mlx]\nurl = "http://b"\ndefault = true\n'
        ))
        with pytest.raises(ValueError, match="more than one routing entry"):
            apply_config(_args(), serve=True, routing=True)

    def test_flag_backends_block_config_routing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, '[routing.gguf]\nurl = "http://cfg:1"')
        _apply_toml_config(_args(upstream=["xx=http://flag:1"]), serve=True,
                           routing=True)
        assert "LTP_UPSTREAM_GGUF" not in os.environ

    def test_env_backends_block_config_routing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LTP_UPSTREAM_MLX", "http://env:1")
        _write_toml(tmp_path, '[routing.gguf]\nurl = "http://cfg:1"')
        _apply_toml_config(_args(), serve=True, routing=True)
        assert "LTP_UPSTREAM_GGUF" not in os.environ

    def test_routing_entry_without_url_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, "[routing.gguf]\nmodels = ['x']")
        with pytest.raises(ValueError, match="needs at least a url"):
            apply_config(_args(), serve=True, routing=True)


class TestBridgeManaged:
    def test_managed_specs_converted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, (
            "[managed.gguf]\n"
            'command = "llama-server --port 8181"\n'
            "port = 8181\n"
            'health_path = "/v1/models"\n'
            "startup_timeout = 15\n"
        ))
        _path, _c, info = _apply_toml_config(_args(), serve=True)
        assert info["managed_specs"] == \
            ["gguf=http://127.0.0.1:8181::llama-server --port 8181"]
        assert os.environ["LTP_PROCESS_GGUF_HEALTH_PATH"] == "/v1/models"
        assert os.environ["LTP_PROCESS_GGUF_STARTUP_TIMEOUT"] == "15.0"

    def test_managed_skipped_when_cli_manages(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, "[managed.gguf]\ncommand = 'true'\nport = 9999")
        _path, _c, info = _apply_toml_config(_args(managed=["x=http://y:1::true"]),
                                             serve=True)
        assert info["managed_specs"] == []

    def test_managed_requires_health_target(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, "[managed.gguf]\ncommand = 'true'")
        with pytest.raises(ValueError, match="base_url or port"):
            apply_config(_args(), serve=True)


class TestTrace:
    def test_trace_bridged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, "[trace]\nenabled = true")
        _apply_toml_config(_args(), serve=True)
        assert os.environ["LTP_TRACE"] == "1"

    def test_trace_flag_wins(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, "[trace]\nenabled = true")
        _apply_toml_config(_args(trace=False), serve=True)  # flag false == unset
        assert os.environ["LTP_TRACE"] == "1"
        # an explicit --trace CLI store_true cannot be distinguished from unset
        # before parsing, so the env bridge keeps config authoritative when the
        # flag was simply absent; this documents that behavior.


class TestCmdServingPieces:
    def test_no_config_empty_info(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
        path, cfg, info = apply_config(_args(), serve=True)
        assert path is None and cfg == {} and info == {"applied": [],
                                                       "managed_specs": []}

    def test_apply_env_and_config_coexist(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LTP_PORT", "5000")
        _write_toml(tmp_path, "port = 9999")
        _apply_toml_config(_args(), serve=True)
        assert os.environ["LTP_PORT"] == "5000"


class TestValidateConfig:
    """Strict schema validation: unknown keys/sections and bad types fail
    fast at load time with a pointed message instead of becoming indirect
    runtime symptoms."""

    _REPO_EXAMPLE = Path(__file__).resolve().parents[3] / "config" / \
        "proxytool-example.toml"

    def _expect_error(self, tmp_path, text: str, match: str) -> None:
        p = _write_toml(tmp_path, text)
        with pytest.raises(ValueError, match=match):
            load_config(p)

    def test_unknown_section_typo_suggested(self, tmp_path):
        self._expect_error(tmp_path, '[upstreams]\nurl = "http://a"',
                           "unknown key 'upstreams'.*did you mean 'upstream'")

    def test_unknown_top_level_key_suggested(self, tmp_path):
        self._expect_error(tmp_path, "prot = 8180",
                           r"unknown key 'prot' \(top level\).*did you mean 'port'")

    def test_unknown_key_in_upstream(self, tmp_path):
        self._expect_error(tmp_path, "[upstream]\ntimeot = 600",
                           r"unknown key 'timeot' in \[upstream\].*did you mean 'timeout'")

    def test_port_wrong_type(self, tmp_path):
        self._expect_error(tmp_path, 'port = "high"', "valid integer")

    def test_port_out_of_range(self, tmp_path):
        self._expect_error(tmp_path, "port = 99999", "port")

    def test_port_rejects_bool(self, tmp_path):
        self._expect_error(tmp_path, "port = true", "port")

    def test_unknown_backend_choice(self, tmp_path):
        self._expect_error(tmp_path, '[upstream]\nbackend = "ggff"', "backend")

    def test_bad_url_rejected(self, tmp_path):
        self._expect_error(tmp_path, '[upstream]\nurl = "not-a-url"', "valid URL")

    def test_invalid_log_level(self, tmp_path):
        self._expect_error(tmp_path, 'log_level = "loud"', "log_level")

    def test_errors_aggregated(self, tmp_path):
        p = _write_toml(tmp_path, 'port = "high"\n[upstream]\nprot = 1')
        with pytest.raises(ValueError) as exc:
            load_config(p)
        msg = str(exc.value)
        assert "valid integer" in msg and "unknown key 'prot'" in msg

    def test_repo_example_config_validates(self):
        cfg = load_config(self._REPO_EXAMPLE)
        assert cfg["port"] == 8180


class TestDescribeConfig:
    """`proxytool config print` dry-run: discovery + validation + provenance,
    and it must restore the environment exactly (bridge under a snapshot)."""

    def test_found_and_config_sourced(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        _write_toml(tmp_path, "port = 9999\n[upstream]\nbackend = 'mlx'")
        info = describe_config(_args())
        assert info["valid"]
        assert info["file"] == str(tmp_path / "proxytool.toml")
        rows = {r["key"]: r for r in info["settings"]}
        assert rows["LTP_PORT"]["source"] == "config file"
        assert rows["LTP_PORT"]["value"] == "9999"
        assert rows["LTP_PORT"]["detail"] == "port"
        assert rows["LTP_BACKEND"]["detail"] == "upstream.backend"
        assert "LTP_PORT" in info["applied"]

    def test_env_pre_existing_beats_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LTP_PORT", "5000")
        _write_toml(tmp_path, "port = 9999")
        info = describe_config(_args())
        rows = {r["key"]: r for r in info["settings"]}
        assert rows["LTP_PORT"]["source"] == "env (pre-existing)"
        assert rows["LTP_PORT"]["value"] == "5000"
        assert "LTP_PORT" not in info["applied"]

    def test_env_restored_after_call(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path, "port = 9999\n[trace]\nenabled = true")
        before = dict(os.environ)
        describe_config(_args())
        assert os.environ == before  # dry-run must not leak bridge exports

    def test_invalid_config_reports_errors(self, tmp_path):
        p = _write_toml(tmp_path, "[upstream]\nprot = 1")
        info = describe_config(_args(config=str(p)))
        assert not info["valid"]
        assert any("prot" in e for e in info["errors"])
        assert info["file"] == str(p)

    def test_no_config_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
        info = describe_config(_args())
        assert info["valid"] and info["file"] is None
        assert info["settings"] == [] or all(
            r["source"] == "env (pre-existing)" for r in info["settings"])


class TestConfigCommand:
    """The `proxytool config` subcommand end to end through cli.main()."""

    def test_print_report_and_no_env_mutation(self, tmp_path, capsys):
        p = _write_toml(tmp_path, "port = 9999")
        rc = cli_mod.main(["config", "print", "--config", str(p)])
        out = capsys.readouterr().out
        assert rc == 0
        assert f"config file: {p}" in out
        assert "validation: ok" in out
        assert "LTP_PORT" in out
        assert os.environ.get("LTP_PORT") != "9999"  # dry-run, env untouched

    def test_json_output(self, tmp_path, capsys):
        p = _write_toml(tmp_path, "port = 9999")
        assert cli_mod.main(["config", "print", "--json", "--config", str(p)]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["file"] == str(p)
        assert any(r["key"] == "LTP_PORT" for r in data["settings"])

    def test_check_ok_exits_zero(self, tmp_path, capsys):
        p = _write_toml(tmp_path, "port = 9999")
        rc = cli_mod.main(["config", "check", "--config", str(p)])
        assert rc == 0
        assert "validation: ok" in capsys.readouterr().out

    def test_invalid_exits_two_with_message(self, tmp_path, capsys):
        p = _write_toml(tmp_path, "[upstream]\nprot = 1")
        rc = cli_mod.main(["config", "check", "--config", str(p)])
        err = capsys.readouterr().err
        assert rc == 2
        assert "config error" in err and "prot" in err

    def test_no_config_still_prints(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
        rc = cli_mod.main(["config", "print"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "config file: (none found)" in out
