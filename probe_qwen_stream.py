"""Throwaway probe #2: isolate why StreamHealer holds closed-think-then-Hermes.

Feeds the payload in small chunks; prints every event so the exact chunk
that trips the hold rule is visible."""

import json

from toolpatch.stream import StreamHealer

# Built by concatenation so no editor layer mangles the literal.
THINK_OPEN = "<" + "think" + ">"
THINK_CLOSE = "<" + "/think" + ">"
HERMES_OPEN = "<" + "tool_call" + ">"
HERMES_CLOSE = "<" + "/tool_call" + ">"

HERMES = HERMES_OPEN + '{"name": "list_dir", "arguments": {"path": "."}}' + HERMES_CLOSE

cases = {
    "closed-think-then-hermes": THINK_OPEN + "I will list it." + THINK_CLOSE + " " + HERMES,
    "closed-think-plain": THINK_OPEN + "reasoning..." + THINK_CLOSE + " plain answer.",
    "unclosed-think-with-hermes": THINK_OPEN + HERMES,
}

for label, payload in cases.items():
    h = StreamHealer()
    print(f"--- {label} ---")
    for i in range(0, len(payload), 5):
        evs = h.feed(payload[i : i + 5])
        if evs:
            print(" feed", repr(payload[i : i + 5]), "->", json.dumps(evs))
    fin = h.finish()
    if fin:
        print(" finish ->", json.dumps(fin))
