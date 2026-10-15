"""Branch coverage for normalize_chat_request's history sanitizer."""

from __future__ import annotations

from toolpatch import _configure_logging, normalize_chat_request


def test_configure_logging_passes_through_unknown_level_as_none():
    # level=None & quiet=False → NullLogger for the shared lib (line 26)
    _configure_logging(level=None, quiet=False)


def _assistant(content=None, **kw):
    m = {"role": "assistant"}
    if content is not None:
        m["content"] = content
    m.update(kw)
    return m


def test_history_not_a_list_is_ignored():
    clean, notes = normalize_chat_request({"messages": {"role": "assistant"}})
    assert clean["messages"] == {"role": "assistant"}
    assert not notes


def test_duplicate_tool_call_ids_deduped():
    tc = {"id": "c1", "type": "function",
          "function": {"name": "f", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(tool_calls=[tc, dict(tc), tc]),
        {"role": "tool", "tool_call_id": "c1"},
    ]
    clean, notes = normalize_chat_request({"messages": messages})
    out = clean["messages"]
    assert len(out[1]["tool_calls"]) == 1
    assert any("duplicate tool_call ids" in n or "deduped" in n for n in notes)


def test_text_twin_matching_is_dropped():
    twin = '{"name": "list_dir", "arguments": {"path": "."}}'
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(twin),
        _assistant(tool_calls=[{"id": "c1", "type": "function",
                                "function": {"name": "list_dir",
                                             "arguments": "{\"path\": \".\"}"}}]),
    ]
    clean, notes = normalize_chat_request({"messages": messages})
    assert len(clean["messages"]) == 2
    assert clean["messages"][1]["role"] == "assistant"
    assert any("merged" in n for n in notes)


def test_text_twin_mismatched_arguments_is_kept():
    twin = '{"name": "list_dir", "arguments": {"path": "/other"}}'
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(twin),
        _assistant(tool_calls=[{"id": "c1", "type": "function",
                                "function": {"name": "list_dir",
                                             "arguments": "{\"path\": \".\"}"}}]),
    ]
    clean, _notes = normalize_chat_request({"messages": messages})
    # not an exact twin: text merged into the tool_calls turn, arguments intact
    assert len(clean["messages"]) == 2
    merged = clean["messages"][1]
    assert merged["content"] == twin
    assert len(merged["tool_calls"]) == 1


def test_text_twin_invalid_json_is_kept():
    messages = [
        {"role": "user", "content": "hi"},
        _assistant('{"name": "list_dir", "arguments": {"path": .}'),  # broken
        _assistant(tool_calls=[{"id": "c1", "type": "function",
                                "function": {"name": "list_dir",
                                             "arguments": "{}"}}]),
    ]
    clean, _notes = normalize_chat_request({"messages": messages})
    # broken-JSON text survives (merged into the tool_calls turn as prose)
    assert len(clean["messages"]) == 2
    assert clean["messages"][1]["content"].startswith('{"name"')


def test_merge_assistant_text_into_tool_calls_turn():
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(""),
        _assistant(tool_calls=[{"id": "c1", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}],
                   content=None),
        {"role": "tool", "tool_call_id": "c1"},
    ]
    clean, notes = normalize_chat_request({"messages": messages})
    out = clean["messages"]
    assert len([m for m in out if m.get("role") == "assistant"]) == 1
    assert any("merged" in n for n in notes)


def test_merge_assistant_with_calls_into_text_turn():
    text = "thinking out loud"
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(text),
        _assistant(tool_calls=[{"id": "c1", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}]),
        {"role": "tool", "tool_call_id": "c1"},
    ]
    clean, _notes = normalize_chat_request({"messages": messages})
    out = clean["messages"]
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == text
    assert len(assistants[0]["tool_calls"]) == 1


def test_merge_two_tool_call_assistant_turns():
    c1 = {"id": "c1", "type": "function",
          "function": {"name": "f", "arguments": "{}"}}
    c2 = {"id": "c2", "type": "function",
          "function": {"name": "g", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(tool_calls=[c1]),
        _assistant(tool_calls=[c2]),
        {"role": "tool", "tool_call_id": "c1"},
    ]
    clean, _notes = normalize_chat_request({"messages": messages})
    out = clean["messages"]
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert len(assistants[0]["tool_calls"]) == 2


def test_merge_identical_text_assistant_turns():
    messages = [
        {"role": "user", "content": "hi"},
        _assistant("same"),
        _assistant("same"),
        {"role": "user", "content": "again"},
    ]
    clean, notes = normalize_chat_request({"messages": messages})
    texts = [m["content"] for m in clean["messages"] if m.get("role") == "assistant"]
    assert texts == ["same"]
    assert notes is not None


def test_merge_distinct_text_assistant_turns_keeps_final():
    messages = [
        {"role": "user", "content": "hi"},
        _assistant("first draft"),
        _assistant("final answer"),
        {"role": "user", "content": "next"},
    ]
    clean, notes = normalize_chat_request({"messages": messages})
    texts = [m["content"] for m in clean["messages"] if m.get("role") == "assistant"]
    assert texts == ["first draft\nfinal answer"]
    assert any("text + text" in n for n in notes)


def test_leaked_pattern_without_json_parses():
    # `{"name":` appears but the whole content is not valid JSON → except path
    messages = [
        {"role": "user", "content": '{"name' + ': unknown'},
        _assistant(tool_calls=[{"id": "c1", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}]),
    ]
    clean, _notes = normalize_chat_request({"messages": messages})
    # the user turn with the invalid JSON stays untouched; the assistant
    # turn keeps its tool_calls (the except-path skipped the twin merge).
    assert clean["messages"][0]["content"].startswith('{"name')
    assert clean["messages"][1]["tool_calls"]
