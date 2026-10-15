from __future__ import annotations

import os

import pytest

from proxytool import cli as cli_mod
from proxytool.routing import Backend


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Strip any host-side LMW_/LTP_ leakage so tests only see what they set.
    for k in list(os.environ):
        if k.startswith(("LTP_", "LMW_")):
            monkeypatch.delenv(k, raising=False)
    yield


class TestFlagOrEnv:
    def test_flag_wins_over_env(self, monkeypatch) -> None:
        monkeypatch.setenv("LTP_UPSTREAM_URL", "http://env:1")
        args = type("A", (), {"upstream_url": "http://flag:2"})()
        assert cli_mod._flag_or_env(args, ["LTP_UPSTREAM_URL"], "upstream_url") == "http://flag:2"

    def test_env_used_when_flag_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("LTP_UPSTREAM_URL", "http://env:1")
        args = type("A", (), {"upstream_url": None})()
        assert cli_mod._flag_or_env(args, ["LTP_UPSTREAM_URL"], "upstream_url") == "http://env:1"

    def test_default_empty_when_neither(self) -> None:
        args = type("A", (), {"upstream_url": None})()
        assert cli_mod._flag_or_env(args, ["LTP_UPSTREAM_URL"], "upstream_url") == ""


class TestPickSingleBackendUrl:
    def test_flag_url_beats_backends(self) -> None:
        args = type("A", (), {"upstream_url": "http://flag:9"})()
        backends = {"gguf": Backend(name="gguf", url="http://gguf:1")}
        assert cli_mod._pick_single_backend_url(backends, args, "") == "http://flag:9"

    def test_default_backend_url_chosen(self) -> None:
        args = type("A", (), {"upstream_url": None})()
        backends = {
            "gguf": Backend(name="gguf", url="http://gguf:1"),
            "mlx": Backend(name="mlx", url="http://mlx:2", default=True),
        }
        assert cli_mod._pick_single_backend_url(backends, args, "") == "http://mlx:2"

    def test_first_backend_when_no_default(self) -> None:
        args = type("A", (), {"upstream_url": None})()
        backends = {"gguf": Backend(name="gguf", url="http://gguf:1")}
        assert cli_mod._pick_single_backend_url(backends, args, "") == "http://gguf:1"


class TestMergeEnv:
    def test_flags_written_into_env_when_unset(self, monkeypatch, clean_env) -> None:
        args = type(
            "A", (), {
                "upstream_api_key": "sk-x",
                "upstream_timeout": 42.0,
                "model_map": '{"a": "b"}',
                "model_map_file": None,
                "trace": True,
                "loop_nudge": 2,
                "loop_abort": 4,
            },
        )()
        cli_mod._merge_env_into_process_env(args, {})
        assert os.environ["LMW_UPSTREAM_API_KEY"] == "sk-x"
        assert os.environ["LMW_UPSTREAM_TIMEOUT"] == "42.0"
        assert os.environ["LMW_MODEL_MAP"] == '{"a": "b"}'
        assert os.environ["LMW_TRACE"] == "1"
        assert os.environ["LMW_LOOP_NUDGE"] == "2"
        assert os.environ["LMW_LOOP_ABORT"] == "4"

    def test_existing_env_not_clobbered(self, monkeypatch, clean_env) -> None:
        monkeypatch.setenv("LMW_TRACE", "1")
        args = type("A", (), {"upstream_api_key": None,
                              "upstream_timeout": None, "model_map": None,
                              "model_map_file": None, "trace": True,
                              "loop_nudge": None, "loop_abort": None})()
        cli_mod._merge_env_into_process_env(args, {})
        assert os.environ["LMW_TRACE"] == "1"  # not clobbered to "1" again


class TestPickSingleBackendUrlEnvFallback:
    def test_flag_upstream_url_wins_over_backends(self) -> None:
        """Regression: --upstream-url was ignored when backends were registered."""
        args = type("A", (), {"upstream_url": "http://flag:9"})()
        backends = {"gguf": Backend(name="gguf", url="http://gguf:1", default=True)}
        assert cli_mod._pick_single_backend_url(backends, args, "") == "http://flag:9"


class TestVersion:
    def test_version_command_exits_zero(self, capsys) -> None:
        assert cli_mod.main(["version"]) == 0
        assert "proxytool" in capsys.readouterr().out


class TestParser:
    def test_serve_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            cli_mod.main([])

    def test_repeatable_upstream_flags(self) -> None:
        parser = cli_mod.build_parser()
        args = parser.parse_args([
            "serve",
            "--upstream", "gguf=http://127.0.0.1:8181",
            "--upstream", "mlx=http://127.0.0.1:8281",
            "--upstream-models", "gguf=qwen-gguf,qwen",
            "--default-backend", "mlx",
        ])
        assert args.upstream == ["gguf=http://127.0.0.1:8181", "mlx=http://127.0.0.1:8281"]
        assert args.upstream_models == ["gguf=qwen-gguf,qwen"]
        assert args.default_backend == "mlx"

