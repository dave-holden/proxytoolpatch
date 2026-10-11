from __future__ import annotations

import json

import pytest

from proxy.loopguard import NUDGE_TEXT, GuardDecision, LoopGuard

TOOL = {
    "type": "function",
    "function": {"name": "list_dir", "parameters": {"type": "object", "properties": {}}},
}


def _body(content: str = "list files", model: str = "qwen") -> dict:
    return {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": content}],
        "tools": [TOOL],
    }


def _ok_response() -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


@pytest.fixture(autouse=True)
def _fresh_guard():
    """The guard lives on the imported app module; reset between tests."""
    from proxy.main import loop_guard

    loop_guard.reset()
    yield
    loop_guard.reset()


# -- unit: state machine ----------------------------------------------------


def test_unit_nudge_then_abort_then_reset():
    guard = LoopGuard(nudge_after=3, abort_after=6)
    key = LoopGuard.request_key(_body())
    actions = [guard.check(key).action for _ in range(6)]
    assert actions == ["none", "none", "nudge", "nudge", "nudge", "abort"]
    # A different request resets the counter.
    other = LoopGuard.request_key(_body(content="different"))
    assert guard.check(other) == GuardDecision("none", 1)
    # ...and returning to the first key restarts from 1.
    assert guard.check(key) == GuardDecision("none", 1)


def test_unit_zero_thresholds_disable():
    guard = LoopGuard(nudge_after=0, abort_after=0)
    key = LoopGuard.request_key(_body())
    for _ in range(10):
        assert guard.check(key).action == "none"


def test_unit_identical_payload_yields_identical_key():
    assert LoopGuard.request_key(_body()) == LoopGuard.request_key(_body())
    assert LoopGuard.request_key(_body()) != LoopGuard.request_key(
        _body(content="changed")
    )


# -- proxy: forwarded requests -----------------------------------------------


def test_proxy_nudge_appended_on_third_identical_request(client, mock_upstream):
    route = mock_upstream.post("/v1/chat/completions").respond(json=_ok_response())
    for _ in range(3):
        res = client.post("/v1/chat/completions", json=_body())
        assert res.status_code == 200
    sent = [json.loads(c.request.read()) for c in route.calls]
    # Requests 1 and 2 pass through untouched; request 3 carries the nudge.
    assert NUDGE_TEXT not in sent[0]["messages"][-1]["content"]
    assert NUDGE_TEXT not in sent[1]["messages"][-1]["content"]
    assert sent[2]["messages"][-1] == {"role": "user", "content": NUDGE_TEXT}
    # The nudge is additive: the original conversation is intact.
    assert sent[2]["messages"][:-1] == sent[0]["messages"]


def test_proxy_abort_returns_clean_non_streaming_completion(client, mock_upstream):
    mock_upstream.post("/v1/chat/completions").respond(json=_ok_response())
    for _ in range(5):
        res = client.post("/v1/chat/completions", json=_body())
        assert res.status_code == 200
        assert res.json()["choices"][0]["message"]["content"] == "ok"
    res = client.post("/v1/chat/completions", json=_body())  # 6th identical
    assert res.status_code == 200
    msg = res.json()["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert "LoopGuard" in msg["content"] and "6 identical" in msg["content"]
    assert res.json()["choices"][0]["finish_reason"] == "stop"


def test_proxy_abort_stream_returns_one_chunk_then_done(client, mock_upstream):
    body = _body()
    body["stream"] = True
    mock_upstream.post("/v1/chat/completions").respond(json=_ok_response())
    for _ in range(5):
        res = client.post("/v1/chat/completions", json=body)
        assert res.status_code == 200
    res = client.post("/v1/chat/completions", json=body)  # 6th identical
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[6:])
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert len(events) == 2
    assert "LoopGuard" in events[0]["choices"][0]["delta"]["content"]
    assert events[1]["choices"][0]["finish_reason"] == "stop"
    assert res.text.strip().endswith("data: [DONE]")


def test_proxy_alternating_requests_never_trigger_guard(client, mock_upstream):
    route = mock_upstream.post("/v1/chat/completions").respond(json=_ok_response())
    for i in range(8):
        body = _body(content=f"question {i % 2}")
        res = client.post("/v1/chat/completions", json=body)
        assert res.status_code == 200
    sent = [json.loads(c.request.read()) for c in route.calls]
    assert len(sent) == 8  # everything reached upstream
    assert all(NUDGE_TEXT not in m["messages"][-1]["content"] for m in sent)


def test_proxy_guard_resets_after_new_turn(client, mock_upstream):
    """A loop cut by abort starts counting again once the prompt changes."""
    mock_upstream.post("/v1/chat/completions").respond(json=_ok_response())
    for _ in range(6):
        client.post("/v1/chat/completions", json=_body())  # aborted on 6th
    res = client.post("/v1/chat/completions", json=_body(content="a fresh question"))
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "ok"
    # And the loop resumes counting from scratch, not from 6.
    res = client.post("/v1/chat/completions", json=_body(content="a fresh question"))
    assert res.json()["choices"][0]["message"]["content"] == "ok"
