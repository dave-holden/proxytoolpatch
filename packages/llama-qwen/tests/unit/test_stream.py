from llama_qwen.stream import StreamHealer

HERMES = '<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>'


def _feed_chunks(text: str, size: int = 3, **kw) -> list[dict]:
    h = StreamHealer(**kw)
    events: list[dict] = []
    for i in range(0, len(text), size):
        events += h.feed(text[i : i + size])
    events += h.finish()
    return events


def test_hermes_leak_becomes_single_tool_calls_event():
    events = _feed_chunks(HERMES, 3)
    tool_events = [e for e in events if e["type"] == "tool_calls"]
    assert len(tool_events) == 1
    assert tool_events[0]["tool_calls"][0]["function"]["name"] == "list_dir"
    # No leaked tool syntax forwarded as content
    content = "".join(e.get("text", "") for e in events if e["type"] == "content")
    assert "tool_call" not in content


def test_plain_text_reassembles_byte_identical():
    text = "word " * 240  # 1200 chars, no tool syntax
    events = _feed_chunks(text, 7)
    assert not any(e["type"] == "tool_calls" for e in events)
    joined = "".join(e["text"] for e in events if e["type"] == "content")
    assert joined == text


def test_finish_flushes_partial_buffer():
    h = StreamHealer()
    events = h.feed("just a normal ")
    events += h.finish()
    assert "".join(e.get("text", "") for e in events if e["type"] == "content") == "just a normal "


def test_leak_in_prose_stream():
    text = f"Sure, checking now. {HERMES} done."
    events = _feed_chunks(text, 5)
    tool_events = [e for e in events if e["type"] == "tool_calls"]
    assert len(tool_events) == 1
    assert tool_events[0]["tool_calls"][0]["function"]["name"] == "list_dir"
    joined = "".join(e.get("text", "") for e in events if e["type"] == "content")
    assert "Sure, checking now." in joined
    assert "tool_call" not in joined
