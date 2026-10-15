from toolpatch.stream import StreamHealer

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


# -- the replayed mlx failure ------------------------------------------------

BARE = '{"name": "webfetch", "arguments": {"url": "https://pypi.org/project/pypi/"}}'


def test_bare_json_object_leak_becomes_tool_calls():
    """Trace-repro: bare {"name"...} content, no Hermes tag, no fence."""
    events = _feed_chunks(BARE, 2)  # mlx streams in tiny deltas
    tool_events = [e for e in events if e["type"] == "tool_calls"]
    assert len(tool_events) == 1
    call = tool_events[0]["tool_calls"][0]
    assert call["function"]["name"] == "webfetch"
    assert "pypi.org" in call["function"]["arguments"]
    assert not any(e["type"] == "content" and e.get("text") for e in events)


def test_bare_json_after_prose_prose_first_then_call():
    """Realistic turn: prose streamed first, then a bare JSON call appended.

    The healer releases the prose; once the object is complete a later delta
    lets the extractor fire and the call is emitted (mirrors the trace where
    mlx sent 'I'll check PyPI for that now.' then the object).
    """
    obj = BARE.replace("https://pypi.org/project/pypi/", "https://x.dev/")
    prose = "I'll check PyPI for that now. "
    h = StreamHealer()
    events = h.feed(prose)          # pure prose — released immediately
    events += h.feed(obj)           # complete object in one delta
    events += h.finish()
    released = "".join(e.get("text", "") for e in events if e["type"] == "content")
    assert "check PyPI for that now" in released
    assert '"name"' not in released
    tools = [e for e in events if e["type"] == "tool_calls"]
    assert len(tools) == 1 and tools[0]["tool_calls"][0]["function"]["name"] == "webfetch"


def test_partial_object_then_completion_recover():
    """Object split across deltas: held while incomplete, fires when complete."""
    obj = BARE
    h = StreamHealer()
    events = h.feed(obj[:39])       # '{"name": "webfetch", "arguments": {"ur'
    # Incomplete: held, nothing released
    assert not any(e["type"] == "content" and e.get("text") for e in events)
    events += h.feed(obj[39:])
    events += h.finish()
    tools = [e for e in events if e["type"] == "tool_calls"]
    assert len(tools) == 1 and tools[0]["tool_calls"][0]["function"]["name"] == "webfetch"
    assert not any(e["type"] == "content" and e.get("text") for e in events)


# -- multi-call scenarios (review: mid-buffer interception with multi-tool)

HERMES_B = '<tool_call>{"name": "webfetch", "arguments": {"url": "https://x.dev/"}}</tool_call>'


def _tool_calls(events: list[dict]) -> list[dict]:
    """All calls across a single promotion frame (there must be exactly one)."""
    promotions = [e for e in events if e["type"] == "tool_calls"]
    assert len(promotions) == 1, f"expected ONE promotion frame, got {len(promotions)}"
    assert promotions[0].get("stop") is True
    return promotions[0]["tool_calls"]


def test_two_hermes_blocks_same_buffer_single_promotion():
    """Prose + two leaked calls in one buffer → one frame, two tool_calls."""
    text = f"Here goes. {HERMES}{HERMES_B}"
    events = _feed_chunks(text, 4)
    calls = _tool_calls(events)
    assert [c["function"]["name"] for c in calls] == ["list_dir", "webfetch"]
    joined = "".join(e.get("text", "") for e in events if e["type"] == "content")
    assert "Here goes." in joined
    assert "tool_call" not in joined


def test_sequential_calls_rapid_succession_not_suppressed():
    """Call #1 completes while call #2 hasn't started streaming: the healer
    must NOT promote-and-lock early — call #2 must still be promoted."""
    h = StreamHealer()
    events = h.feed("checking… ")
    events += h.feed(HERMES)        # call #1 complete, #2 not yet started
    events += h.feed(" and ")       # inter-call gap
    events += h.feed(HERMES_B)      # call #2 completes later
    events += h.finish()
    calls = _tool_calls(events)     # assert exactly one promotion frame
    assert [c["function"]["name"] for c in calls] == ["list_dir", "webfetch"]
    joined = "".join(e.get("text", "") for e in events if e["type"] == "content")
    assert "checking…" in joined and " and " in joined


def test_mixed_family_hermes_plus_fenced_both_promoted():
    """Parallel blocks in different families both land in the one frame."""
    fenced = "```json\n{\"name\": \"read_file\", \"arguments\": {\"f\": \"a.py\"}}\n```"
    text = HERMES + fenced
    events = _feed_chunks(text, 6)
    calls = _tool_calls(events)
    assert [c["function"]["name"] for c in calls] == ["list_dir", "read_file"]
    assert "```" not in "".join(e.get("text", "") for e in events if e["type"] == "content")


def test_collecting_mode_bounded_by_cap():
    """A call followed by a rambling stream must still promote by the cap."""
    filler = "word " * 400      # > 4 * default max_buffer (512*4 = 2048)
    h = StreamHealer()
    events = h.feed(HERMES + filler)
    promotions = [e for e in events if e["type"] == "tool_calls"]
    assert len(promotions) == 1           # cap forced one promotion
    assert promotions[0]["tool_calls"][0]["function"]["name"] == "list_dir"
    assert h.finish() == []  # _done: nothing further after the cap promotion


def test_leading_brace_json_value_stream_released():
    """A leading '{' that opens a plain JSON *value* (not a tool call) stills
    releases content once the buffer passes max_buffer (not stuck forever)."""
    filler = "x" * 600
    payload = '{"answer": "' + filler + '"}'
    h = StreamHealer()
    events = h.feed(payload)
    assert any(e["type"] == "content" for e in events)  # max_buffer released it
    events += h.finish()
    assert events  # finished without hanging
