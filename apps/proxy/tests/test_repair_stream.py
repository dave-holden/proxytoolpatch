from __future__ import annotations

import json

OPEN_TAG = bytes.fromhex("3c746f6f6c5f63616c6c3e").decode()      # <tool_call>
CLOSE_TAG = bytes.fromhex("3c2f746f6f6c5f63616c6c3e").decode()   # \n
LEAKED = OPEN_TAG + '{"name": "list_dir", "arguments": {"path": "."}}' + CLOSE_TAG

TOOL = {
    "type": "function",
    "function": {"name": "list_dir", "parameters": {"type": "object", "properties": {}}},
}


def _sse(*payloads: dict) -> bytes:
    return b"".join(
        f"data: {json.dumps(p, separators=(',', ':'))}\n\n".encode() for p in payloads
    ) + b"data: [DONE]\n\n"


def _delta(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


def test_stream_leak_repaired(client, mock_upstream):
    body = {
        "model": "qwen",
        "stream": True,
        "messages": [{"role": "user", "content": "list files"}],
        "tools": [TOOL],
    }
    # Simulate Qwen streaming the leak in 3-char chunks like mlx/llama servers do.
    chunks = [LEAKED[i:i + 3] for i in range(0, len(LEAKED), 3)]
    sse = _sse(*(_delta(c) for c in chunks))
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=sse,
        headers={"content-type": "text/event-stream"},
    )

    res = client.post("/v1/chat/completions", json=body)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    tool_call_deltas: list[dict] = []
    content_text = ""
    for line in res.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        data = json.loads(line[6:])
        delta = data["choices"][0]["delta"]
        if "tool_calls" in delta:
            tool_call_deltas += delta["tool_calls"]
        if "content" in delta:
            content_text += delta["content"]

    assert any(c["function"]["name"] == "list_dir" for c in tool_call_deltas)
    assert "tool_call" not in content_text

    # The terminating finish_reason from upstream must survive the repair.
    finish_reasons = [
        json.loads(line[6:])["choices"][0]["finish_reason"]
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert any(f in ("stop", "tool_calls") for f in finish_reasons)


def test_stream_meta_and_usage_forwarded(client, mock_upstream):
    """id/created/model and usage must survive the repair reshaping."""
    body = {
        "model": "qwen",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [TOOL],
    }
    sse = (
        _sse(
            {
                "id": "chatcmpl-x1",
                "created": 1700000000,
                "model": "qwen",
                "choices": [{"delta": {"content": "A"}}],
            },
            {
                "id": "chatcmpl-x1",
                "created": 1700000000,
                "model": "qwen",
                "choices": [
                    {"delta": {}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )
    )
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=sse,
        headers={"content-type": "text/event-stream"},
    )
    res = client.post("/v1/chat/completions", json=body)
    assert res.status_code == 200
    chunks = [
        json.loads(line[6:])
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert all(c["id"] == "chatcmpl-x1" for c in chunks)
    assert all(c["model"] == "qwen" for c in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert "usage" in chunks[-1]
    assert chunks[-1]["usage"]["completion_tokens"] == 1


def test_stream_unterminated_upstream_gets_synthetic_stop(client, mock_upstream):
    """If upstream forgets finish_reason, downstream still gets termination."""
    body = {
        "model": "qwen",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [TOOL],
    }
    sse = _sse(_delta("plain answer"))  # no finish_reason, no usage
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=sse,
        headers={"content-type": "text/event-stream"},
    )
    res = client.post("/v1/chat/completions", json=body)
    assert res.status_code == 200
    fin = [
        json.loads(line[6:])["choices"][0]["finish_reason"]
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert "stop" in fin


def test_stream_plain_answer_not_mangled(client, mock_upstream):
    body = {
        "model": "qwen",
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [TOOL],
    }
    sse = _sse(_delta("Hello "), _delta("there!"))
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=sse,
        headers={"content-type": "text/event-stream"},
    )

    res = client.post("/v1/chat/completions", json=body)
    content = "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    )
    assert content == "Hello there!"
    assert '"tool_calls"' not in res.text
