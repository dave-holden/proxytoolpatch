"""OpenAI API conformance contract tests for the middleware surface.

These pin the *protocol* contract an OpenAI-compatible client can rely on —
independent of repair logic — against a mocked upstream. The middleware must
either mirror the upstream's OpenAI shapes verbatim or emit equally valid
substitutes.

Contract areas:
  1. non-streaming chat.completion schema (clean passthrough = exact body)
  2. streaming SSE frame grammar (role/meta/usage/finish/[DONE])
  3. tool_calls schema validity (call_<id>, type function, string arguments)
  4. /v1/models passthrough (body + status + content-type)
  5. error contracts (client 400, upstream 4xx/5xx, non-JSON upstream body)
"""

from __future__ import annotations

import json

from test_repair_stream import (
    CLOSE_TAG,
    LEAKED,
    OPEN_TAG,
    TOOL,
    _delta,
    _sse,
)


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_sse(text: str):
    return [
        line[len("data: "):] for line in text.splitlines()
        if line.startswith("data: ")
    ]


def _stream_events(res_text: str):
    out = []
    for raw in _parse_sse(res_text):
        if raw == "[DONE]":
            out.append("DONE")
        else:
            out.append(json.loads(raw))
    return out


def _json_lines(text: str):
    return [json.loads(line[len("data: "):]) for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


# ── 1 · non-streaming chat.completion schema ─────────────────────────────────
def test_nonstream_clean_response_is_verbatim(client, mock_upstream):
    """A clean, schema-valid upstream body must reach the client unchanged."""
    upstream = {
        "id": "chatcmpl-xyz",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "qwen-real",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello there!"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, json=upstream,
    )
    res = client.post("/v1/chat/completions", json={"model": "qwen",
                                                    "messages": [
                                                        {"role": "user",
                                                         "content": "hi"}]})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert json.loads(res.text) == upstream  # byte-shape contract


def test_nonstream_schema_fields_present(client, mock_upstream):
    """Required OpenAI completion members must survive the round trip."""
    upstream = {
        "id": "chatcmpl-ab",
        "object": "chat.completion",
        "created": 1_700_000_001,
        "model": "qwen-real",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, json=upstream)
    res = client.post("/v1/chat/completions", json={
        "model": "my-qwen", "messages": [{"role": "user", "content": "hi"}]})
    data = json.loads(res.text)
    for key in ("id", "object", "created", "model", "choices", "usage"):
        assert key in data
    assert data["object"] == "chat.completion"
    assert isinstance(data["id"], str)
    assert isinstance(data["created"], int)
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["choices"][0]["message"]["role"] == "assistant"


def test_nonstream_tool_call_shape_is_openai_valid(client, mock_upstream):
    """A leaked tool call, once repaired, meets the tool_calls schema."""
    body = LEAKED  # contents of message.content
    upstream = {
        "id": "chatcmpl-tc", "object": "chat.completion",
        "created": 1_700_000_002, "model": "qwen-real",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": body},
            "finish_reason": "tool_calls",
        }],
    }
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, json=upstream)
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "messages": [{"role": "user", "content": "list"}],
        "tools": [TOOL],
    })
    data = json.loads(res.text)
    msg = data["choices"][0]["message"]
    calls = msg["tool_calls"]
    assert len(calls) == 1
    call = calls[0]
    assert call["type"] == "function"
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "list_dir"
    # arguments must be a JSON *string* per the OpenAI schema
    assert isinstance(call["function"]["arguments"], str)
    json.loads(call["function"]["arguments"])  # parses
    # content may be null when the leak fully replaced the prose (schema-valid)
    assert msg["content"] in (None, "")
    dumped = json.dumps(msg)
    assert "tool_call>" not in dumped and "<tool_call" not in dumped
    assert data["choices"][0]["finish_reason"] == "tool_calls"


# ── 2 · streaming SSE frame grammar ─────────────────────────────────────────
def test_stream_basic_grammar(client, mock_upstream):
    """Every line obeys the SSE frame grammar; [DONE] is last and singular."""
    upstream = _sse(
        {"id": "chatcmpl-s1", "created": 123, "model": "qwen-real",
         "choices": [{"delta": {"role": "assistant", "content": ""}, "index": 0,
                      "finish_reason": None}]},
        _delta("Hello "),
        _delta("there!"),
        {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]},
    )
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=upstream,
        headers={"content-type": "text/event-stream"})
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    lines = res.text.splitlines()
    nonempty = [line for line in lines if line]
    assert all(line.startswith("data: ") for line in nonempty)
    assert [line for line in nonempty if line == "data: [DONE]"] == ["data: [DONE]"]
    assert nonempty[-1] == "data: [DONE]"
    chunks = _json_lines(res.text)
    roles = [c["choices"][0]["delta"].get("role") for c in chunks
             if c["choices"][0]["delta"].get("role")]
    assert "assistant" in roles  # role-first delta preserved
    finish = [c["choices"][0]["finish_reason"] for c in chunks if
              c["choices"][0]["finish_reason"]]
    assert finish == ["stop"]
    content = "".join(c["choices"][0]["delta"].get("content") or ""
                      for c in chunks)
    assert content == "Hello there!"
    assert res.headers["content-type"].startswith("text/event-stream")


def test_stream_meta_correlation(client, mock_upstream):
    """All frames share the upstream id/created/model correlation triple."""
    upstream = _sse(
        {"id": "chatcmpl-meta", "created": 555, "model": "qwen-real",
         "system_fingerprint": "fp_1",
         "choices": [{"delta": {"content": "x"}, "index": 0,
                      "finish_reason": None}]},
        {"id": "chatcmpl-meta", "created": 555, "model": "qwen-real",
         "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]},
    )
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=upstream,
        headers={"content-type": "text/event-stream"})
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    chunks = _json_lines(res.text)
    for c in chunks:
        assert c["id"] == "chatcmpl-meta"
        assert c["created"] == 555
        assert c["model"] == "qwen-real"


def test_stream_crlf_line_endings_parsed(client, mock_upstream):
    """Upstreams emitting CRLF-terminated frames must still parse."""
    chunks = [
        {"id": "crlf", "created": 1, "model": "m",
         "choices": [{"delta": {"content": "a"}, "index": 0,
                      "finish_reason": None}]},
        {"id": "crlf", "created": 1, "model": "m",
         "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]},
    ]
    body = b"".join(
        b"data: " + json.dumps(c, separators=(",", ":")).encode() + b"\r\n\r\n"
        for c in chunks) + b"data: [DONE]\r\n\r\n"
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=body,
        headers={"content-type": "text/event-stream"})
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert "".join(c["choices"][0]["delta"].get("content") or ""
                   for c in _json_lines(res.text)) == "a"
    assert res.text.rstrip("\r\n").endswith("data: [DONE]") or \
        "data: [DONE]" in res.text


def test_stream_usage_chunk_forwarded_once(client, mock_upstream):
    """stream_options.include_usage final chunk keeps its usage object."""
    upstream = _sse(
        {"id": "u1", "created": 2, "model": "m",
         "choices": [{"delta": {"content": "hi"}, "index": 0,
                      "finish_reason": None}]},
        {"id": "u1", "created": 2, "model": "m",
         "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 1,
                   "total_tokens": 5}},
    )
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=upstream,
        headers={"content-type": "text/event-stream"})
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    usages = [c.get("usage") for c in _json_lines(res.text) if c.get("usage")]
    assert usages == [{"prompt_tokens": 4, "completion_tokens": 1,
                       "total_tokens": 5}]


def test_stream_repaired_call_terminates_with_tool_calls(client, mock_upstream):
    """Leaked tool calls promote as one frame ending with finish_reason
    'tool_calls', never leaving a role-unclosed turn."""
    chunks = [LEAKED[i:i + 3] for i in range(0, len(LEAKED), 3)]
    upstream = _sse(*(_delta(c) for c in chunks))
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, content=upstream,
        headers={"content-type": "text/event-stream"})
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "stream": True,
        "messages": [{"role": "user", "content": "list"}],
        "tools": [TOOL]})
    events = _stream_events(res.text)
    assert "DONE" in events
    finish_reasons = [c["choices"][0]["finish_reason"] for c in events
                      if c != "DONE" and c["choices"][0]["finish_reason"]]
    assert "tool_calls" in finish_reasons
    assert events[-1] == "DONE"
    # no unterminated tag text anywhere
    assert not any(OPEN_TAG in json.dumps(c) or CLOSE_TAG in json.dumps(c)
                   for c in events if c != "DONE")


# ── 4 · /v1/models passthrough ───────────────────────────────────────────────
def test_v1_models_passthrough_identity(client, mock_upstream):
    upstream = {"object": "list", "data": [
        {"id": "qwen-real", "object": "model", "owned_by": "local"},
        {"id": "qwen2.5-coder", "object": "model", "owned_by": "local"},
    ]}
    mock_upstream.get("/v1/models").respond(status_code=200, json=upstream)
    res = client.get("/v1/models")
    assert res.status_code == 200
    assert json.loads(res.text) == upstream


def test_v1_models_upstream_error_passthrough(client, mock_upstream):
    mock_upstream.get("/v1/models").respond(
        status_code=404, json={"error": {"message": "no models",
                                         "type": "invalid_request_error"}})
    res = client.get("/v1/models")
    assert res.status_code == 404
    assert json.loads(res.text)["error"]["message"] == "no models"


# ── 5 · error contracts ─────────────────────────────────────────────────────
def test_client_bad_json_body_is_400_object(client):
    res = client.post("/v1/chat/completions",
                      content=b"definitely not json",
                      headers={"content-type": "application/json"})
    assert res.status_code == 400
    data = json.loads(res.text)
    assert "error" in data


def test_upstream_500_keeps_status_and_becomes_openai_error(
        client, mock_upstream):
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=500, text="boom")
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 500
    err = json.loads(res.text)["error"]
    assert err["type"] == "upstream_error"
    assert err["code"] == 500
    assert "boom" in err["message"]


def test_upstream_401_status_propagates(client, mock_upstream):
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=401, text="unauthorized")
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 401
    err = json.loads(res.text)["error"]
    assert err["code"] == 401


def test_upstream_error_in_stream_is_clean_openai_sse(
        client, mock_upstream):
    """A failed stream becomes ONE syntactically valid SSE stream, status 200
    per the streaming contract, with a [DONE] terminator and no raw HTML."""
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=500, text="<html>Internal Server Error</html>")
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    nonempty = [line for line in res.text.splitlines() if line]
    assert all(line.startswith("data: ") for line in nonempty)
    assert nonempty[-1] == "data: [DONE]"
    single = json.loads(nonempty[0][len("data: "):])
    assert "Internal Server Error" in \
        single["choices"][0]["delta"]["content"]
    assert single["choices"][0]["finish_reason"] == "stop"
    # "clean" contract: the error appears ONLY inside valid JSON deltas —
    # no lines outside the SSE grammar, no [DONE]-less truncation.
    for line in nonempty[:-1]:
        json.loads(line[len("data: "):])  # every payload is one JSON object


def test_upstream_non_json_nonstream_passthrough_raw(client, mock_upstream):
    """A non-JSON upstream body is not silently mangled on the way out."""
    mock_upstream.post("/v1/chat/completions").respond(
        status_code=200, text="<html>odd upstream</html>",
        headers={"content-type": "text/html"})
    res = client.post("/v1/chat/completions", json={
        "model": "qwen", "messages": [{"role": "user", "content": "hi"}]})
    assert "odd upstream" in res.text  # byte passthrough, not JSONified
