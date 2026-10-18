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


def test_stream_upstream_error_becomes_clean_sse_finish(client, mock_upstream):
    """An upstream 4xx/5xx must arrive as ONE clean terminating SSE chunk."""
    body = {
        "model": "qwen",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [TOOL],
    }
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=404, content='{"error": "Repository Not Found for url: m"}',
        headers={"content-type": "application/json"},
    )
    res = client.post("/v1/chat/completions", json=body)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[6:])
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert len(events) == 1
    delta = events[0]["choices"][0]["delta"]
    assert "Repository Not Found" in delta["content"]
    assert events[0]["choices"][0]["finish_reason"] == "stop"
    assert res.text.strip().endswith("data: [DONE]")


def test_mlx_backend_strips_tool_choice_and_maps_model(client, monkeypatch):
    """Dialect is auto-detected per request: an mlx model alias → tool_choice
    dropped upstream, and PTP_MODEL_MAP remaps the alias upward."""
    import importlib

    monkeypatch.setenv(
        "PTP_MODEL_MAP",
        '{"qwen-mlx": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}',
    )
    from proxytool import settings as settings_mod
    importlib.reload(settings_mod)
    import proxytool.main as main_mod
    importlib.reload(main_mod)  # pick up reloaded settings + fresh conduit settings

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def __init__(self) -> None:
            self.headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self):
            yield _sse(_delta("ok"))

        async def aclose(self):
            pass

        async def aread(self):
            return b""

    async def fake_forward_stream(path, payload, headers=None):
        captured.update(payload)
        if headers is not None:
            captured["__headers"] = headers
        return _FakeResp()

    monkeypatch.setattr(main_mod.conduit, "forward_stream", fake_forward_stream)

    body = {
        "model": "qwen-mlx",
        "stream": True,
        "messages": [{"role": "user", "content": "list files"}],
        "tools": [TOOL],
        "tool_choice": "auto",
        "stream_options": {"include_usage": True},
    }
    res = client.post("/v1/chat/completions", json=body)
    assert res.status_code == 200
    assert "tool_choice" not in captured, "tool_choice must be stripped for mlx"
    assert captured["model"] == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"
    assert "tools" in captured  # tools survive the mlx backend for repair


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


def _split_response(*chunks: bytes):
    """Upstream fake whose ``aiter_bytes`` yields arbitrary byte chunks."""

    class _SplitResp:
        status_code = 200

        def __init__(self) -> None:
            self.headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self):
            for c in chunks:
                yield c

        async def aclose(self):
            pass

        async def aread(self):
            return b""

    return _SplitResp()


def test_stream_frame_split_mid_json(client, monkeypatch):
    """An SSE data line split mid-JSON across HTTP chunks must reassemble."""
    import proxytool.main as main_mod

    body_text = json.dumps(
        {"choices": [{"delta": {"content": "plain"}}]},
        separators=(",", ":"),
    )
    sse = f"data: {body_text}\n\ndata: [DONE]\n\n".encode()
    # Slice inside the JSON payload, after a brace: the first chunk is an
    # unparseable half-line by itself.
    cut = sse.index(b"delta") + len(b"de")
    half1, half2 = sse[:cut], sse[cut:]
    assert b"\n" not in half1 and not half2.startswith(b"data")

    async def fake_forward_stream(path, payload, headers=None):
        return _split_response(half1, half2)

    monkeypatch.setattr(main_mod.conduit, "forward_stream", fake_forward_stream)
    res = client.post(
        "/v1/chat/completions",
        json={"model": "qwen", "stream": True,
              "messages": [{"role": "user", "content": "hi"}], "tools": [TOOL]},
    )
    assert res.status_code == 200
    content = "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    )
    assert content == "plain"


def test_stream_multibyte_char_split_across_chunks(client, monkeypatch):
    """A multibyte UTF-8 char split across chunks must survive intact."""
    import proxytool.main as main_mod

    text = "héllo 地球"
    # ensure_ascii=False: emit raw multi-byte UTF-8, as llama-server/mlx do
    # (\u escapes would make byte-splitting unreachable).
    payload = "data: " + json.dumps(
        {"choices": [{"delta": {"content": text}}]},
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n\ndata: [DONE]\n\n"
    raw = payload.encode("utf-8")
    # Split exactly inside a multibyte character: locate 'é's 0xC3 lead byte
    # and cut between it and its continuation byte.
    first_nonascii = next(i for i, b in enumerate(raw) if b >= 0x80)
    cut = first_nonascii + 1  # inside 'é' (0xC3 0xA9)
    half1, half2 = raw[:cut], raw[cut:]
    try:
        half1.decode("utf-8")
    except UnicodeDecodeError:
        pass  # the cut landed mid-character, as required
    else:
        raise AssertionError("cut must land inside a multibyte char")

    async def fake_forward_stream(path, payload, headers=None):
        return _split_response(half1, half2)

    monkeypatch.setattr(main_mod.conduit, "forward_stream", fake_forward_stream)
    res = client.post(
        "/v1/chat/completions",
        json={"model": "qwen", "stream": True,
              "messages": [{"role": "user", "content": "hi"}], "tools": [TOOL]},
    )
    assert res.status_code == 200
    assert "\ufffd" not in res.text, "replacement char must not leak into content"
    content = "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in res.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    )
    assert content == text
