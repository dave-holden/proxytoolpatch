import json

import pytest

from toolpatch.fixer import heal_json


def _rt(raw: str) -> dict:
    healed = heal_json(raw)
    if healed is None:
        pytest.fail(f"no heal for {raw!r}")
    return json.loads(healed)  # type: ignore[arg-type]


def test_valid_json_unchanged():
    assert heal_json('{"a": 1}') == '{"a": 1}'


def test_trailing_comma_obj():
    assert _rt('{"a": 1,}') == {"a": 1}


def test_trailing_comma_nested():
    assert _rt('{"p": {"b": [1,2,],},}') == {"p": {"b": [1, 2]}}


def test_unescaped_newline_in_string():
    assert _rt('{"cmd": "ls\nm"}') == {"cmd": "ls\nm"}


def test_smart_quotes():
    raw = "{\u201cname\u201d: \u201cread_file\u201d}"
    assert _rt(raw) == {"name": "read_file"}


def test_fenced_json():
    assert _rt('```json\n{"name":"ls"}\n```') == {"name": "ls"}


def test_top_level_array_with_hanging_comma():
    assert _rt('["a", "b",]') == ["a", "b"]


def test_unrecoverable():
    assert heal_json('{"a": ') is None
    assert heal_json("") is None
