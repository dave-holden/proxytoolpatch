from __future__ import annotations

import json

import pytest

BE_A = "http://127.0.0.1:8181"  # conftest's mock_upstream base_url
BE_B = "http://127.0.0.1:8281"


def _chat(sse_or_json: dict) -> bytes:
    return json.dumps(sse_or_json).encode()


def _upstream_reply(model: str) -> dict:
    return {
        "id": "c1", "model": model,
        "choices": [{"message": {"role": "assistant", "content": f"backend:{model}"},
                     "finish_reason": "stop"}],
    }


@pytest.fixture()
def routed_client(mock_upstream, respx_mock_b, monkeypatch):
    """App with a two-backend router registered via PTP_UPSTREAM_* env."""
    monkeypatch.setenv("PTP_UPSTREAM_GGUF", BE_A)
    monkeypatch.setenv("PTP_UPSTREAM_MLX", BE_B)
    monkeypatch.setenv("PTP_UPSTREAM_MLX_MODELS", "qwen-mlx")
    monkeypatch.setenv("PTP_DEFAULT_BACKEND", "mlx")
    # _ROUTER caches at module level: skip the cache per test.
    import proxytool.main as main_mod

    main_mod._ROUTER = None
    main_mod._CONDUITS.clear()
    from proxytool.main import app

    yield app


@pytest.fixture()
def respx_mock_b():
    import respx

    with respx.mock(base_url=BE_B) as mock:
        yield mock


def _reset_router():
    import proxytool.main as main_mod

    main_mod._ROUTER = None
    main_mod._CONDUITS.clear()


def test_model_list_routes_to_second_backend(respx_mock_b, monkeypatch):
    monkeypatch.setenv("PTP_UPSTREAM_GGUF", BE_A)
    monkeypatch.setenv("PTP_UPSTREAM_MLX", BE_B)
    monkeypatch.setenv("PTP_UPSTREAM_MLX_MODELS", "qwen-mlx")
    monkeypatch.setenv("PTP_DEFAULT_BACKEND", "mlx")
    _reset_router()

    respx_mock_b.post("/v1/chat/completions").respond(
        status_code=200, content=_chat(_upstream_reply("mlx-be"))
    )
    # Register a default-backend catch-all on A too: anything routed there fails the test.
    mock_upstream = pytest.importorskip("respx")
    from fastapi.testclient import TestClient

    from proxytool.main import app

    with mock_upstream.mock(base_url=BE_A, assert_all_called=False) as mock_a:
        mock_a.post("/v1/chat/completions").respond(
            status_code=200, content=_chat(_upstream_reply("gguf-be"))
        )
        client = TestClient(app)
        res = client.post(
            "/v1/chat/completions",
            json={"model": "qwen-mlx", "stream": False,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        assert res.json()["choices"][0]["message"]["content"] == "backend:mlx-be"

    _reset_router()


def test_reserved_ptp_upstream_suffixes_are_not_routes(monkeypatch):
    """Regression guard: settings keys under PTP_UPSTREAM_* must not become
    named routes when cmd_serve exports them into the same env namespace
    (they previously made the router front `sk-x`/`42.0` as upstreams)."""
    from proxytool.routing import backends_from_env

    monkeypatch.setenv("PTP_UPSTREAM_API_KEY", "sk-x")
    monkeypatch.setenv("PTP_UPSTREAM_TIMEOUT", "42.0")
    monkeypatch.setenv("PTP_UPSTREAM_URL", "http://127.0.0.1:8181")
    monkeypatch.setenv("PTP_UPSTREAM_GGUF", BE_A)
    monkeypatch.setenv("PTP_UPSTREAM_GGUF_MODELS", "qwen-gguf")
    env = backends_from_env()
    assert set(env) == {"gguf"}
    assert env["gguf"].url == BE_A
    assert env["gguf"].models == ("qwen-gguf",)


def test_default_backend_used_when_no_model_match(respx_mock_b, monkeypatch, mock_upstream):
    monkeypatch.setenv("PTP_UPSTREAM_GGUF", BE_A)
    monkeypatch.setenv("PTP_UPSTREAM_MLX", BE_B)
    monkeypatch.setenv("PTP_UPSTREAM_MLX_MODELS", "qwen-mlx")
    monkeypatch.setenv("PTP_DEFAULT_BACKEND", "mlx")
    _reset_router()
    from fastapi.testclient import TestClient

    from proxytool.main import app

    model_alias = "unknown-alias"  # not in any model list → default backend (mlx)
    respx_mock_b.post("/v1/chat/completions").respond(
        status_code=200, content=_chat(_upstream_reply("mlx-be"))
    )
    client = TestClient(app)
    res = client.post(
        "/v1/chat/completions",
        json={"model": model_alias, "stream": False,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "backend:mlx-be"
    _reset_router()


def test_single_upstream_env_keeps_single_conduit(monkeypatch, mock_upstream):
    """One declared backend → no router built; legacy single-conduit path."""
    monkeypatch.setenv("PTP_UPSTREAM_URL", BE_A)
    _reset_router()
    from proxytool.main import get_router

    assert get_router() is None
    _reset_router()
