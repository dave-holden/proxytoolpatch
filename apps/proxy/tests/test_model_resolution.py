from __future__ import annotations

import pytest

from proxy import main as main_mod

REAL = "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


@pytest.fixture(autouse=True)
def _reset_model_cache(monkeypatch):
    # _resolve_model caches discovery at module scope; each test starts fresh.
    monkeypatch.setattr(main_mod, "_UPSTREAM_MODELS", None)


def _models_response(mock_upstream, models: list[str]) -> None:
    mock_upstream.get("/v1/models").respond(
        json={"object": "list", "data": [{"id": m} for m in models]}
    )


def _chat(mock_upstream):
    return mock_upstream.post("/v1/chat/completions").respond(
        status_code=200,
        json={
            "id": "c1",
            "model": REAL,
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
        },
    )


def _post(client, model: str):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "stream": False,
              "messages": [{"role": "user", "content": f"hello from {model}"}]},
    )


def test_alias_resolved_via_v1_models_discovery(client, mock_upstream):
    """Unknown alias + exactly one upstream model → use that model's id."""
    _models_response(mock_upstream, [REAL])
    route = _chat(mock_upstream)

    res = _post(client, "qwen-mlx")
    assert res.status_code == 200
    assert route.called
    assert route.calls.last.request.read().decode("utf-8").count(f'"{REAL}"') >= 1
    body = res.json()
    assert body["model"] == REAL  # response echoes upstream's model field


def test_exact_upstream_id_passes_through(client, mock_upstream):
    """If the client already uses the real id, forward it unchanged."""
    _models_response(mock_upstream, [REAL])
    route = _chat(mock_upstream)

    res = _post(client, REAL)
    assert res.status_code == 200
    sent = route.calls.last.request.read().decode("utf-8")
    assert f'"{REAL}"' in sent


def test_lmw_model_map_takes_precedence(client, mock_upstream, monkeypatch):
    """An explicit LMW_MODEL_MAP entry wins — no upstream discovery is attempted."""
    monkeypatch.setattr(
        main_mod.settings, "model_map", {"qwen-mlx": "mapped-explicit/id"}
    )
    route = _chat(mock_upstream)  # only the chat route is registered

    res = _post(client, "qwen-mlx")
    assert res.status_code == 200
    sent = route.calls.last.request.read().decode("utf-8")
    assert '"mapped-explicit/id"' in sent


def test_multi_model_upstream_keeps_unknown_alias(client, mock_upstream):
    """With several models loaded we cannot guess; alias is forwarded as-is."""
    _models_response(mock_upstream, [REAL, "other-model"])
    route = _chat(mock_upstream)

    res = _post(client, "qwen-mlx")
    assert res.status_code == 200
    sent = route.calls.last.request.read().decode("utf-8")
    assert '"qwen-mlx"' in sent


def test_discovery_unreachable_keeps_alias(client, mock_upstream):
    """No /v1/models route available → alias forwarded unchanged, no 500."""
    route = _chat(mock_upstream)  # chat works; models endpoint unregistered

    res = _post(client, "qwen-mlx")
    assert res.status_code == 200
    sent = route.calls.last.request.read().decode("utf-8")
    assert '"qwen-mlx"' in sent
