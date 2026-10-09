import copy
import json
import pathlib

import pytest

from llama_qwen import StreamHealer, extract_tool_calls

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _ids() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json"))


def _normalize_calls(calls: list[dict]) -> list[dict]:
    out = []
    for i, tc in enumerate(calls):
        tc = copy.deepcopy(tc)
        tc["id"] = f"call_{i}"  # ids are random; fixture pins the position
        out.append(tc)
    return out


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=_ids())
def test_golden_pair(path: pathlib.Path) -> None:
    case = json.loads(path.read_text())
    expect = copy.deepcopy(case["expect"])

    if case["kind"] == "stream":
        healer = StreamHealer()
        events: list[dict] = []
        for chunk in case["upstream"]["chunks"]:
            events += healer.feed(chunk)
        events += healer.finish()
        result = {"events": events}
        # normalize random call ids inside events
        for ev in result["events"]:
            if ev["type"] == "tool_calls":
                ev["tool_calls"] = _normalize_calls(ev["tool_calls"])
        assert result == expect, f"{path.name}: {result} != {expect}"
        return

    ex = extract_tool_calls(case["upstream"]["content"])
    if ex is None:
        result = {
            "content": case["upstream"]["content"],
            "tool_calls": [],
        }
    else:
        result = {
            "content": ex.content,
            "tool_calls": _normalize_calls(ex.tool_calls),
        }
    assert result == expect, f"{path.name}: {result} != {expect}"
