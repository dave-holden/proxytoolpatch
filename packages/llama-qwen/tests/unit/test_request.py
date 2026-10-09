from llama_qwen.request import normalize_chat_request

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
