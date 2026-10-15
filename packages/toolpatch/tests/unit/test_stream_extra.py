"""Extra StreamHealer branch tests: cap promotion and un-release of slices."""

from __future__ import annotations

import runpy
import sys

import pytest

from toolpatch.stream import StreamHealer

OPEN = chr(0x3C) + "tool_call" + chr(0x3E)
CLOSE = chr(0x3C) + "/tool_call" + chr(0x3E)


def _hermes_call(name):
    return OPEN + f'{{"name": "{name}", "arguments": {{}}}}' + CLOSE


def test_collecting_mode_cap_promotes_midstream():
    h = StreamHealer()
    events = h.feed(_hermes_call("list_dir"))
    filler = "w " * 1100  # > 512 * 4 cap
    events += h.feed(filler)
    promotions = [e for e in events if e["type"] == "tool_calls"]
    assert promotions, "cap must force a midstream promotion"
    assert promotions[0]["tool_calls"][0]["function"]["name"] == "list_dir"


def test_release_slice_with_leak_shape_is_unreleased():
    """A release slice containing a whole open tag must be re-held."""
    h = StreamHealer()
    _ = [e for e in h.feed("x" * 40)]
    events = h.feed(OPEN)
    leaked = "".join(e.get("text", "") for e in events if e["type"] == "content")
    assert not leaked.startswith(OPEN)


def test_unreleased_slice_then_finish_promotes_call():
    h = StreamHealer()
    h.feed("x" * 40)
    h.feed(OPEN)
    h.feed('{"name": "webfetch", "arguments": {}}')
    events = h.feed(CLOSE)
    events += h.finish()
    calls = [e for e in events if e["type"] == "tool_calls"]
    assert calls and calls[0]["tool_calls"][0]["function"]["name"] == "webfetch"


def test_release_at_max_size_while_leak_open():
    h = StreamHealer()
    h._leak_open = True
    h._buf = "x" * 600
    events = h.feed("y")
    # max_size release fires: the released prose may finally escape.
    assert any(e["type"] == "content" for e in events)


def test_fence_open_at_tail_holds():
    h = StreamHealer()
    events = h.feed("x" * 40 + "```")
    # odd fence parity at the tail: hold, do not release as prose
    assert "```" not in "".join(e.get("text", "") for e in events
                                  if e["type"] == "content")


# --------------------------------------------------------------- __main__.py
def test_main_module_runs_cli(capsys, monkeypatch):
    """Exercise proxytool/__main__.py's guard in-process (runpy)."""
    saved_argv = sys.argv
    try:
        sys.argv = ["proxytool", "--help"]
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("proxytool", run_name="__main__")
        assert exc.value.code == 0
        assert "serve" in capsys.readouterr().out
    finally:
        sys.argv = saved_argv
