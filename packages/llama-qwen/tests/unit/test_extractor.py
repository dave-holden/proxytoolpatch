from llama_qwen.extractor import extract_tool_calls


def test_hermes_single():
    r = extract_tool_calls(
        '<tool_call>{"name": "list_dir", "arguments": {}}</tool_call>'
    )
    assert r is not None
    assert r.source == "hermes"
    assert r.tool_calls[0]["function"]["name"] == "list_dir"
    assert r.tool_calls[0]["function"]["arguments"] == "{}"
    assert r.content in (None, "")


def test_fenced_with_broken_json_heals():
    r = extract_tool_calls(
        '```json\n{"name": "ls", "arguments": {"a": 1,}}\n```'
    )
    assert r is not None
    assert r.source == "fenced"
    assert r.tool_calls[0]["function"]["name"] == "ls"
    assert r.tool_calls[0]["function"]["arguments"] == '{"a": 1}'


def test_raw_json_in_prose():
    r = extract_tool_calls(
        'Let me look. {"name":"list_dir","arguments":{"path":"."}} Going now.'
    )
    assert r is not None
    assert r.source == "raw_json"
    assert "list_dir" in r.tool_calls[0]["function"]["name"]
    assert r.content is not None
    assert "Going now." in r.content


def test_tool_name_filter_blocks_foreign_names():
    r = extract_tool_calls(
        '{"name":"whatever","arguments":{}}', tool_names={"list_dir"}
    )
    assert r is None


def test_clean_content_passthrough():
    assert extract_tool_calls("No tool here.") is None


def test_multiple_hermes_calls():
    payload = (
        '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
        '<tool_call>{"name": "b", "arguments": {}}</tool_call>'
    )
    r = extract_tool_calls(payload)
    assert r is not None
    assert [tc["function"]["name"] for tc in r.tool_calls] == ["a", "b"]
