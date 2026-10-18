"""Regression: opencode TUI history that llama-server rejected with 400.

Captured 2026-10-10 from a live TUI run (mismatched template + repaired tool
call + double-append of the final assistant answer):

    "Cannot have 2 or more assistant messages at the end of the list."
      code=400 type=invalid_request_error

The client history contained BOTH the leaked-JSON assistant text turn and the
repaired ``tool_calls`` turn, plus the final assistant answer appended twice.
``normalize_chat_request`` must collapse this to a template-legal history.
"""

import json
from itertools import pairwise

from toolpatch.request import normalize_chat_request

_TMP_PATH = "/Users/daveholden/Projects/LAM-Proxytool/llama-proxytool"

DIR_LISTING = (
    ".DS_Store\n.freebuff/\n.git/\n.gitignore\n.pytest_cache/\n.python-version\n"
    ".ruff_cache/\n.venv/\napps/\nconfig/\nhistory.txt\nmain.py\noc-stderr.log\n"
    "packages/\nPLAN.md\npyproject.toml\nREADME.md\nRELEASE.md\nRunning.md\n"
    "scripts/\nuv.lock"
)

_ANSWER = f"""Here are the contents of the current directory:

```
{DIR_LISTING}
```"""


def _client_history() -> list[dict]:
    """Exactly the message tail observed in the failing trace (request #5)."""
    return [
        {"role": "system", "content": "You are opencode, an interactive CLI tool."},
        {"role": "user", "content": "list contents of current directory"},
        {
            "role": "assistant",
            "content": (
                '{"name": "read", "arguments": '
                '{"filePath": "/Users/daveholden/Projects/LAM-Proxytool/llama-proxytool"}}'
            ),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_ae7ac26eb03745b095cc44b4",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps(
                            {"filePath": _TMP_PATH},
                            separators=(",", ":"),
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_ae7ac26eb03745b095cc44b4",
            "content": f"<path>/Users/daveholden/Projects/LAM-Proxytool/llama-proxytool</path>"
            f"<type>directory</type><entries>{DIR_LISTING}</entries>",
        },
        {"role": "assistant", "content": _ANSWER},
        {"role": "assistant", "content": _ANSWER},
    ]


def test_failing_tui_history_becomes_template_legal():
    out, notes = normalize_chat_request(
        {"model": "qwen", "messages": _client_history(), "stream": True}
    )
    msgs = out["messages"]

    # No two adjacent assistant messages anywhere in the history.
    for a, b in pairwise(msgs):
        assert not (a["role"] == "assistant" and b["role"] == "assistant"), (
            f"adjacent assistant messages remain: {a['role']},{b['role']}"
        )

    # Exactly one assistant tool_calls turn with the read call intact.
    tc_turns = [m for m in msgs if m.get("tool_calls")]
    assert len(tc_turns) == 1
    assert tc_turns[0]["tool_calls"][0]["function"]["name"] == "read"

    # The final assistant answer survives (deduped to one).
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == _ANSWER

    # The raw leaked-JSON text no longer appears in any content.
    for m in msgs:
        content = m.get("content")
        if isinstance(content, str):
            assert '{"name": "read"' not in content

    assert notes, f"expected sanitization notes, got {notes}"


def test_note_types_reported_for_trace_history():
    _, notes = normalize_chat_request({"model": "qwen", "messages": _client_history()})
    joined = " ".join(notes)
    assert "merged" in joined or "deduped" in joined
