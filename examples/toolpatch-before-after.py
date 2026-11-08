#!/usr/bin/env python3
"""Before/after: toolpatch used directly, no proxy process involved.

A local model answers a tools request as ordinary text (Hermes markup in
`content`). The coding agent sees prose — nothing executes. toolpatch turns
that text into a structured OpenAI `tool_calls` entry, streaming or not.

Run from the repo root:   python examples/toolpatch-before-after.py
"""

import json
import sys

sys.path.insert(0, "packages/toolpatch/src")  # or: import toolpatch (PyPI)

from toolpatch.extractor import extract_tool_calls
from toolpatch.stream import StreamHealer

# ── BEFORE: the model leaked the call into its text ─────────────────────────
MODEL_REPLY = (
    "I'll list the directory now.\n"
    '<tool_call>{"name":"list_dir","arguments":{"path":"."}}</tool_call>'
)
#                 ^ the client shows this as prose — no tool runs

print("BEFORE (raw model content):")
print("   ", MODEL_REPLY)

# ── AFTER 1: non-streaming repair ───────────────────────────────────────────
fixed = extract_tool_calls(MODEL_REPLY, tool_names={"list_dir"})

print("\nAFTER (extract_tool_calls):")
assert fixed is not None, "a leak was expected"
print("    content    →", fixed.content)
print("    tool_calls →", json.dumps(fixed.tool_calls[0]["function"]))

# ── AFTER 2: streaming repair — same call arriving as SSE deltas ────────────
DELTAS = [
    "I'll list the directory now.\n",
    "<tool_call>",
    '{"name":',
    '"lis',
    't_dir"',
    ', "ar',
    "guments",
    '": {"path": "."}}',
    "\n</tool_call>",
]

healer = StreamHealer(tool_names={"list_dir"})
for delta in DELTAS:
    for event in healer.feed(delta):
        print("\nAFTER (StreamHealer, per-delta events):")
        if event["type"] == "tool_calls":
            print("    tool_calls →", json.dumps(event["tool_calls"][0]["function"]))
        else:
            print("    content    →", event["text"][:40])
for event in healer.finish():
    if event["type"] == "tool_calls":
        print("    tool_calls →", json.dumps(event["tool_calls"][0]["function"]))
