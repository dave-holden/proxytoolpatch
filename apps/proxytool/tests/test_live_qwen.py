"""Live E2E test against a real Qwen backend (llama-server GGUF or mlx_lm.server).

Opt-in: skipped unless ``PTP_LIVE=1``. Requires an upstream already running on
``PTP_UPSTREAM_URL`` (default ``http://127.0.0.1:8181``) that can actually run
the model — start it with ``scripts/dev-rig.sh gguf <model.gguf>`` / ``mlx <model>``,
or run the whole rig and test together with:

    PTP_LIVE=1 scripts/dev-rig.sh gguf $MPATH/qwen2.5-...gguf uv-test

Checks the real failure mode end-to-end through the proxytool, streaming and
non-streaming, using a prompt that reliably triggers tool use on Qwen:
- request side: request accepted (tools/tool_choice sanitized OK)
- response side: no raw hermes tags / leaked JSON anywhere in the client-visible
  payload — every tool call must arrive structured in ``tool_calls``
- if a leak was detected and repaired, print a capture block to stderr in
  regression-fixture form, ready to paste into
  ``packages/toolpatch/tests/regression/fixtures/``
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

OPEN_TAG = bytes.fromhex("3c746f6f6c5f63616c6c3e").decode()
CLOSE_TAG = bytes.fromhex("3c2f746f6f6c5f63616c6c3e").decode()

UPSTREAM = os.environ.get("PTP_UPSTREAM_URL", "http://127.0.0.1:8181")
PROXY = os.environ.get("PTP_PROXY_URL", "http://127.0.0.1:8180")
MODEL = os.environ.get("PTP_LIVE_MODEL", "qwen2.5-coder-14b-instruct")

TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the files in a directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "directory path"}},
            "required": ["path"],
        },
    },
}

PROMPT = "List the files in the current directory using the list_dir tool."


def _upstream_is_live() -> bool:
    try:
        r = httpx.get(f"{UPSTREAM}/health", timeout=2.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("PTP_LIVE") != "1" or not _upstream_is_live(),
    reason="PTP_LIVE=1 not set or no live upstream on PTP_UPSTREAM_URL",
)


def _assert_clean(msg: dict) -> None:
    """Client-visible payload must be leak-free: no tags, no raw JSON blobs."""
    content = msg.get("content")
    if content:
        assert OPEN_TAG not in content, f"leaked hermes open tag: {content!r}"
        assert CLOSE_TAG not in content, f"leaked hermes close tag: {content!r}"
        assert '"name"' not in content or not any(
            t["function"]["name"] in content for t in msg.get("tool_calls") or []
        ), f"raw leaked JSON next to repaired tool_calls: {content!r}"
    calls = msg.get("tool_calls") or []
    assert calls, "expected the model to call list_dir — got no tool calls at all"
    for c in calls:
        assert c["function"]["name"] == "list_dir"
        json.loads(c["function"]["arguments"])  # arguments must be valid JSON


def test_live_nonstream_repair():
    body = {
        "model": MODEL,
        "stream": False,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [TOOL],
    }
    r = httpx.post(f"{PROXY}/v1/chat/completions", json=body, timeout=180.0)
    assert r.status_code == 200, r.text
    msg = r.json()["choices"][0]["message"]
    print("\n[capture:nonstream]", json.dumps({"content": msg.get("content"),
                                               "tool_calls": msg.get("tool_calls")}))
    _assert_clean(msg)


def test_live_stream_repair():
    body = {
        "model": MODEL,
        "stream": True,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [TOOL],
    }
    with httpx.stream(
        "POST", f"{PROXY}/v1/chat/completions", json=body, timeout=180.0
    ) as r:
        assert r.status_code == 200, r.text
        events, content_deltas, tool_call_chunks = [], [], []
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            evt = json.loads(data)
            events.append(evt)
            delta = evt.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                content_deltas.append(delta["content"])
            if delta.get("tool_calls"):
                tool_call_chunks.append(delta["tool_calls"])
                assert delta["tool_calls"][0]["function"]["name"] == "list_dir"

    joined = "".join(content_deltas)
    assert OPEN_TAG not in joined, f"leaked hermes open tag in stream: {joined!r}"
    assert CLOSE_TAG not in joined, f"leaked hermes close tag in stream: {joined!r}"
    assert tool_call_chunks, "expected streamed tool_calls from the proxytool"
    print("\n[capture:stream]", json.dumps({"chunks": [e for e in events[:3]]}, indent=2))
