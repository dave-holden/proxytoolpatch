"""One-off probe: current engine behavior on the Qwen failure modes from the
community reference (think-tag interference, duplicate tool-call ids, orphan
tool results). Deliberately throwaway — real assertions land in tests/ after
this captures what needs fixing."""

import json

from toolpatch.extractor import extract_tool_calls
from toolpatch.request import normalize_chat_request
from toolpatch.stream import StreamHealer

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

HERMES = '<tool_call>{"name":"list_dir","arguments":{"path":"."}}</tool_call>'

c1 = THINK_OPEN + "I will list it." + THINK_CLOSE + " " + HERMES
c2 = THINK_OPEN + '{"name":"list_dir","arguments":{"path":"."}}'  # no close
c3 = THINK_OPEN + "reasoning..." + THINK_CLOSE + " plain answer."
c4 = THINK_OPEN + "unterminated think, no close, no tool"

print("T1 closed-think-then-hermes     :", extract_tool_calls(c1))
print("T2 hermes-inside-unclosed-think :", extract_tool_calls(c2))
print("T3 closed-think-plain-answer    :", extract_tool_calls(c3))
print("T4 unterminated-think-only      :", extract_tool_calls(c4))


def _chunk_stream(text: str, size: int = 5) -> list[dict]:
    h = StreamHealer()
    events: list[dict] = []
    for i in range(0, len(text), size):
        events += h.feed(text[i : i + size])
    events += h.finish()
    return events


print("T5 stream closed-think-then-hermes:", json.dumps(_chunk_stream(c1)))
print("T6 stream closed-think-plain      :", json.dumps(_chunk_stream(c3)))
print("T7 stream hermes-in-unclosed-think:", json.dumps(_chunk_stream(c2)))

msgs = [
    {"role": "user", "content": "go"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_x", "type": "function", "function": {"name": "ls", "arguments": "{}"}},
            {"id": "call_x", "type": "function", "function": {"name": "ls", "arguments": "{}"}},
        ],
    },
    {"role": "tool", "tool_call_id": "call_x", "content": "out"},
]
out, notes = normalize_chat_request({"model": "q", "messages": msgs})
print("T8 duplicate-id history      :", out["messages"][1]["tool_calls"])
print("T8 notes                     :", notes)

msgs2 = [
    {"role": "user", "content": "go"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "ls", "arguments": "{}"}}
        ],
    },
    {"role": "tool", "tool_call_id": "call_b", "content": "orphan tool result"},
]
out2, notes2 = normalize_chat_request({"model": "q", "messages": msgs2})
print("T9 orphan tool result roles  :", [m["role"] for m in out2["messages"]])
print("T9 notes                     :", notes2)
