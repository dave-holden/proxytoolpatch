from __future__ import annotations

import pytest

from proxytool import main as main_mod

REAL = "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


@pytest.fixture(autouse=True)
def _reset_model_cache(monkeypatch):
    # _resolve_model caches discovery at module scope; each test starts fresh.
    monkeypatch.setattr(main_mod, "_UPSTREAM_MODELS", None)


@pytest.fixture(autouse=True)
def _reset_loop_guard():
    # Identical bodies across tests would otherwise trigger the loop guard
    # nudge and change the posted payload.
    from proxytool.main import loop_guard

    loop_guard.reset()


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
    """Several models, none sharing alias tokens + path edge → alias kept."""
    _models_response(mock_upstream, ["zed-model", "other-model"])
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


# -- the real-world 3-model cache -------------------------------------------

SNAPSHOT = (
    "/Users/x/.cache/huggingface/hub/models--mlx-community--"
    "Qwen2.5-Coder-14B-Instruct-4bit/snapshots/29efdbab55a161237ab1e432a3abaf6c7ae2b477"
)
CACHE_BOARD = [REAL, "mlx-community/Qwen3.5-9B-OptiQ-4bit", SNAPSHOT]


def test_alert_overlaps_pick_path_like_snapshot(client, mock_upstream):
    """Trace-repro: 2 HF repos + 1 launched snapshot → snapshot wins."""
    _models_response(mock_upstream, CACHE_BOARD)
    route = _chat(mock_upstream)

    res = _post(client, "qwen-mlx")
    assert res.status_code == 200
    sent = route.calls.last.request.read().decode("utf-8")
    assert f'"{SNAPSHOT}"' in sent
    # The MLX repo of the SAME architecture is not chosen over the snapshot.
    assert '"mlx-community/Qwen3.5-9B-OptiQ-4bit"' not in sent


def test_no_token_overlap_keeps_alias(client, mock_upstream):
    """An alias sharing no tokens with any upstream id passes through."""
    _models_response(mock_upstream, CACHE_BOARD)
    route = _chat(mock_upstream)

    res = _post(client, "llama-70b")
    assert res.status_code == 200
    sent = route.calls.last.request.read().decode("utf-8")
    assert '"llama-70b"' in sent


def test_tied_overlap_without_path_returns_alias(client, mock_upstream):
    """Two HF repos tie with the same overlap → ambiguous, alias kept."""
    _models_response(
        mock_upstream,
        ["mlx-community/Qwen3.5-9B-OptiQ-4bit", "mlx-community/Qwen2.5-3B-a"],
    )
    route = _chat(mock_upstream)

    res = _post(client, "qwen-mlx")
    assert res.status_code == 200
    sent = route.calls.last.request.read().decode("utf-8")
    assert '"qwen-mlx"' in sent
