from __future__ import annotations

import json


def test_clean_request_response_byte_passthrough(client, mock_upstream):
    body = {
        "model": "qwen",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }
    upstream_body = {"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]}
    route = mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, json=upstream_body
    )

    res = client.post("/v1/chat/completions", json=body)
    assert res.status_code == 200
    assert res.json() == upstream_body

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "qwen"
    assert sent["messages"] == body["messages"]


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_models_passthrough(client, mock_upstream):
    mock_upstream.get("/v1/models").respond(
        status_code=200, json={"data": [{"id": "qwen"}]}
    )
    res = client.get("/v1/models")
    assert res.status_code == 200
    assert res.json() == {"data": [{"id": "qwen"}]}
