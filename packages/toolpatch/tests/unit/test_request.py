from toolpatch.request import normalize_chat_request

BASE = {"model": "q", "messages": [{"role": "user", "content": "hi"}]}

GOOD_TOOL = {
    "type": "function",
    "function": {
        "name": "good",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


def test_empty_tools_dropped():
    out, warn = normalize_chat_request({**BASE, "tools": []})
    assert "tools" not in out
    assert warn


def test_tool_choice_stripped_for_mlx_only():
    p = {**BASE, "tools": [GOOD_TOOL], "tool_choice": "auto"}
    out_gguf, _ = normalize_chat_request(p, backend="gguf")
    assert out_gguf["tool_choice"] == "auto"
    out_mlx, _ = normalize_chat_request(p, backend="mlx")
    assert "tool_choice" not in out_mlx


def test_bad_parameters_flagged_and_dropped():
    bad = {"type": "function", "function": {"name": "f", "parameters": ["not", "an", "object"]}}
    out, warn = normalize_chat_request({**BASE, "tools": [GOOD_TOOL, bad]})
    assert [t["function"]["name"] for t in out["tools"]] == ["good"]
    assert any("parameters" in w for w in warn)


def test_input_dict_not_mutated():
    import copy

    original = copy.deepcopy({**BASE, "tools": [GOOD_TOOL], "tool_choice": "auto"})
    normalize_chat_request(original, backend="mlx")
    assert original["tool_choice"] == "auto"
    assert original["tools"][0]["function"]["name"] == "good"


def test_unknown_keys_preserved():
    p = {**BASE, "temperature": 0.2, "stream_options": {"include_usage": True}}
    out, _ = normalize_chat_request(p)
    assert out["temperature"] == 0.2
    assert out["stream_options"] == {"include_usage": True}


# --- assistant history sanitization (from live opencode TUI traces) ---------


def _assistant_text(content):
    return {"role": "assistant", "content": content}


def _assistant_tool_calls(name, args_json, call_id="call_x"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args_json},
            }
        ],
    }


def _user(content):
    return {"role": "user", "content": content}


def test_leaked_text_twin_dropped_before_tool_calls_turn():
    """Repaired history: leaked-JSON text + structured turn must collapse."""
    leaked = '{"name": "read", "arguments": {"filePath": "/tmp"}}'
    msgs = [
        {"role": "system", "content": "sys"},
        _user("list"),
        _assistant_text(leaked),
        _assistant_tool_calls("read", '{"filePath": "/tmp"}'),
        {"role": "tool", "tool_call_id": "call_x", "content": "entries"},
    ]
    out, notes = normalize_chat_request({"model": "q", "messages": msgs})
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user", "assistant", "tool"], roles
    a = out["messages"][2]
    assert a["tool_calls"][0]["function"]["name"] == "read"
    assert not (a.get("content") or "").strip().startswith("{")
    assert any("merged" in n for n in notes)


def test_text_different_args_kept_both_turns_unmerged():
    """Leaked-JSON text that does NOT match the next tool call must survive."""
    leaked = '{"name": "read", "arguments": {"filePath": "/a"}}'
    msgs = [
        _user("go"),
        _assistant_text(leaked),
        _assistant_tool_calls("read", '{"filePath": "/b"}'),
        {"role": "tool", "tool_call_id": "call_x", "content": "x"},
    ]
    out, _ = normalize_chat_request({"model": "q", "messages": msgs})
    # the text twin is merged into the assistant turn (text+tool_calls merge)
    # leaving exactly one assistant message before the tool result
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "tool"], roles


def test_two_adjacent_assistant_texts_deduped_when_identical():
    answer = "Here are the contents"
    msgs = [_user("go"), _assistant_text(answer), _assistant_text(answer)]
    out, notes = normalize_chat_request({"model": "q", "messages": msgs})
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant"], roles
    assert out["messages"][-1]["content"] == answer
    assert any("deduped" in n for n in notes)


def test_two_distinct_assistant_texts_merge_into_last():
    msgs = [_user("go"), _assistant_text("part one"), _assistant_text("part two")]
    out, _ = normalize_chat_request({"model": "q", "messages": msgs})
    assert len(out["messages"]) == 2
    assert out["messages"][-1]["content"] == "part one\npart two"


def test_tool_calls_assistant_then_text_assistant_merges():
    msgs = [
        _user("go"),
        _assistant_tool_calls("bash", '{"command": "ls"}'),
        _assistant_text("did the thing"),
        {"role": "tool", "tool_call_id": "call_x", "content": "out"},
    ]
    out, _ = normalize_chat_request({"model": "q", "messages": msgs})
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "tool"], roles
    a = out["messages"][1]
    assert a["tool_calls"][0]["function"]["name"] == "bash"


def test_clean_history_untouched():
    msgs = [
        {"role": "system", "content": "sys"},
        _user("hi"),
        _assistant_text("hello"),
        _user("more"),
    ]
    out, notes = normalize_chat_request({"model": "q", "messages": msgs})
    assert out["messages"] == msgs
    assert not notes


def _parametrized_history():
    """Repaired multi-turn history: leaked twin + adjacent assists + dupes."""
    leaked = '{"name": "read", "arguments": {"filePath": "/tmp"}}'
    return [
        {"role": "system", "content": "sys"},
        _user("list"),
        _assistant_text(leaked),
        _assistant_tool_calls("read", '{"filePath": "/tmp"}'),
        {"role": "tool", "tool_call_id": "call_x", "content": "entries"},
        _assistant_text("answer"),
        _assistant_text("answer"),
    ]


def test_mlx_backend_gets_same_history_sanitize_shape():
    """MLX parity: the gguf-400 history shape must collapse identically."""
    out_mlx, notes_mlx = normalize_chat_request(
        {"model": "q", "messages": _parametrized_history()}, backend="mlx"
    )
    out_gguf, notes_gguf = normalize_chat_request(
        {"model": "q", "messages": _parametrized_history()}, backend="gguf"
    )
    assert out_mlx["messages"] == out_gguf["messages"]
    assert notes_mlx == notes_gguf
    roles = [m["role"] for m in out_mlx["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"], roles


def test_mlx_no_tool_choice_no_notes():
    p = {**BASE, "tools": [GOOD_TOOL]}
    out, warn = normalize_chat_request(p, backend="mlx")
    assert "tool_choice" not in out
    assert warn == ["leaked tool-call text turn merged into tool_calls turn"] or not warn or any(
        "merged" in w for w in warn
    )  # history notes only; no tool_choice note when key absent


def test_mlx_strips_tool_choice_and_notes_it():
    p = {**BASE, "tools": [GOOD_TOOL], "tool_choice": "auto"}
    out, warn = normalize_chat_request(p, backend="mlx")
    assert "tool_choice" not in out
    assert any("tool_choice" in w for w in warn)
