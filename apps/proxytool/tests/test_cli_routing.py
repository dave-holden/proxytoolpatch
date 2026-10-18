from __future__ import annotations

import pytest

from proxytool.routing import Backend, BackendRouter, backends_from_args, backends_from_env


class TestBackendRouter:
    def test_single_backend_routes_everything(self) -> None:
        r = BackendRouter()
        r.register(Backend(name="gguf", url="http://127.0.0.1:8181"))
        d = r.route("any-alias")
        assert d.backend.name == "gguf"
        assert d.matched_by == "only-backend"

    def test_model_list_match_wins(self) -> None:
        r = BackendRouter()
        r.register(Backend(name="gguf", url="http://127.0.0.1:8181",
                           models=("qwen-gguf",)))
        r.register(Backend(name="mlx", url="http://127.0.0.1:8281",
                           models=("qwen-mlx",), default=True))
        d = r.route("qwen-mlx")
        assert d.backend.name == "mlx"
        assert d.matched_by == "model-list"

    def test_default_used_when_no_model_list_match(self) -> None:
        r = BackendRouter()
        r.register(Backend(name="gguf", url="http://127.0.0.1:8181",
                           models=("qwen-gguf",)))
        r.register(Backend(name="mlx", url="http://127.0.0.1:8281",
                           models=("qwen-mlx",), default=True))
        d = r.route("something-else")
        assert d.backend.name == "mlx"
        assert d.matched_by == "default"

    def test_routing_is_case_insensitive(self) -> None:
        r = BackendRouter()
        r.register(Backend(name="gguf", url="http://127.0.0.1:8181",
                           models=("Qwen-GGUF",)))
        assert r.route("qwen-gguf").backend.name == "gguf"

    def test_no_backends_raises(self) -> None:
        with pytest.raises(RuntimeError, match="no backends"):
            BackendRouter().route("anything")

    def test_multiple_defaults_raises(self) -> None:
        r = BackendRouter()
        r.register(Backend(name="a", url="http://a", default=True))
        r.register(Backend(name="b", url="http://b", default=True))
        with pytest.raises(ValueError, match="multiple backends"):
            _ = r.default

    def test_duplicate_name_raises(self) -> None:
        r = BackendRouter()
        r.register(Backend(name="a", url="http://a"))
        with pytest.raises(ValueError, match="duplicate backend"):
            r.register(Backend(name="a", url="http://a2"))


class TestBackendsFromEnv:
    def test_env_backends_built_and_flagged_default(self, monkeypatch) -> None:
        monkeypatch.setenv("PTP_UPSTREAM_GGUF", "http://127.0.0.1:8181")
        monkeypatch.setenv("PTP_UPSTREAM_GGUF_MODELS", "qwen-gguf, qwen")
        monkeypatch.setenv("PTP_DEFAULT_BACKEND", "gguf")
        b = backends_from_env()
        assert b["gguf"].url == "http://127.0.0.1:8181"
        assert b["gguf"].models == ("qwen-gguf", "qwen")
        assert b["gguf"].default is True

    def test_env_default_backends_skip_meta_vars(self, monkeypatch) -> None:
        monkeypatch.setenv("PTP_UPSTREAM_DEFAULT", "gguf")
        monkeypatch.setenv("PTP_UPSTREAM_MLX_MODELS", "qwen-mlx")
        assert backends_from_env() == {}


class TestBackendsFromArgs:
    def test_args_build_backends_with_models(self) -> None:
        b = backends_from_args(
            ["gguf=http://127.0.0.1:8181/", "mlx=http://127.0.0.1:8281"],
            {"gguf": "qwen-gguf", "mlx": "qwen-mlx"},
            "mlx",
        )
        assert b["gguf"].url == "http://127.0.0.1:8181"
        assert b["mlx"].models == ("qwen-mlx",)
        assert b["mlx"].default is True
        assert b["gguf"].default is False

    def test_malformed_upstream_flag_raises(self) -> None:
        with pytest.raises(ValueError, match="NAME=URL"):
            backends_from_args(["nonsense"], {}, None)
