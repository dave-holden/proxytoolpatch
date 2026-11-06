from __future__ import annotations

OPEN_TAG = bytes.fromhex("3c746f6f6c5f63616c6c3e").decode()      # <tool_call>
CLOSE_TAG = bytes.fromhex("3c2f746f6f6c5f63616c6c3e").decode()   # </tool_call>
LEAKED = OPEN_TAG + '{"name": "list_dir", "arguments": {"path": "."}}' + CLOSE_TAG

TOOL = {
    "type": "function",
    "function": {"name": "list_dir", "parameters": {"type": "object", "properties": {}}},
}


def test_leak_becomes_tool_calls(client, mock_upstream):
    body = {
        "model": "qwen",
        "stream": False,
        "messages": [{"role": "user", "content": "list files"}],
        "tools": [TOOL],
    }
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200,
        json={"choices": [{"message": {"role": "assistant", "content": LEAKED}}]},
    )

    res = client.post("/v1/chat/completions", json=body)
    assert res.status_code == 200
    msg = res.json()["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "list_dir"
    assert not msg.get("content") or "tool_call" not in msg["content"]


def test_clean_content_untouched_with_tools(client, mock_upstream):
    body = {
        "model": "qwen",
        "stream": False,
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [TOOL],
    }
    clean = {"choices": [{"message": {"role": "assistant", "content": "Just chatting."}}]}
    mock_upstream.post("/v1/chat/completions").respond(status_code=200, json=clean)

    res = client.post("/v1/chat/completions", json=body)
    assert res.json() == clean


def test_repair_keeps_existing_tool_calls(client, mock_upstream):
    body = {"model": "qwen", "stream": False,
            "messages": [{"role": "user", "content": "x"}], "tools": [TOOL]}
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": LEAKED,
                        "tool_calls": [
                            {"id": "call_prev", "type": "function",
                             "function": {"name": "other", "arguments": "{}"}}
                        ],
                    }
                }
            ]
        },
    )
    res = client.post("/v1/chat/completions", json=body)
    calls = res.json()["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["other", "list_dir"]
    assert calls[0]["id"] == "call_prev"  # existing call untouched


def test_native_call_is_not_duplicated_by_the_leak(client, mock_upstream):
    """A native tool_calls entry plus the same call in `content` is ONE call.

    mlx-serve's GGUF engine answers with `finish_reason: tool_calls` *and* the
    raw `<tools>` block still in `content`. Healing that content on top of the
    native entry reported the call twice, so a client ran the tool twice.
    """
    body = {"model": "qwen", "stream": False,
            "messages": [{"role": "user", "content": "x"}], "tools": [TOOL]}
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        # Same call as the native entry, spaced differently.
                        "content": (OPEN_TAG + '{"name": "list_dir", '
                                    '"arguments": {"path": "."}}' + CLOSE_TAG),
                        "tool_calls": [
                            {"id": "call_native", "type": "function",
                             "function": {"name": "list_dir",
                                          "arguments": '{"path":"."}'}}
                        ],
                    },
                }
            ]
        },
    )
    res = client.post("/v1/chat/completions", json=body)
    msg = res.json()["choices"][0]["message"]
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["id"] == "call_native"
    assert not msg.get("content") or "tool_call" not in msg["content"]
