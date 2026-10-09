"""StreamHealer: repair tool-call leaks inside SSE content deltas.

Bounded streaming repair: deltas accumulate in a small buffer. Clean prose is
released quickly; text that *might* be forming a tool call is held until the
extractor can decide. Emits plain-dict events consumed by the proxy without
further translation.
"""

from __future__ import annotations

from llama_qwen.extractor import extract_tool_calls

# Markers that suggest a leak may be forming; while any prefix-match lingers we hold.
_HERMES_OPEN = "<tool_call>"
_FENCE_MARK = "```"
_JSON_BRACE = '{"name"'
_BRACE = "{"


class StreamHealer:
    """Buffers streamed content until a tool-call leak is decided; emits events.

    Event shapes (plain dicts, consumed verbatim by the proxy):

    - ``{"type": "content", "text": str}``                       safe to forward
    - ``{"type": "tool_calls", "tool_calls": [...], "stop": True}``  replace stream
    - ``{"type": "content", "text": <remaining buffer>}``        flush at finish
    """

    def __init__(self, max_buffer: int = 512,
                 tool_names: set[str] | None = None) -> None:
        self._max = max_buffer
        self._tool_names = tool_names
        self._buf = ""
        self._holding = False
        self._leak_open = False  # a partial leak prefix is in flight
        self._done = False  # a leak was extracted; suppress plain content after

    # -- public ------------------------------------------------------------
    def feed(self, content_delta: str) -> list[dict]:
        if self._done or not content_delta:
            return []
        self._buf += content_delta
        events: list[dict] = []

        # Try to decide with what we have (cheap probe: full extractor on buffer).
        ex = extract_tool_calls(self._buf, self._tool_names)
        if ex is not None:
            events.append({
                "type": "tool_calls",
                "tool_calls": ex.tool_calls,
                "stop": True,
            })
            self._buf = ""
            self._done = True
            self._holding = False
            self._leak_open = False
            return events

        if self._should_release():
            events.append({"type": "content", "text": self._buf})
            self._buf = ""
        else:
            self._holding = True
        return events

    def finish(self) -> list[dict]:
        """Flush the stream end: decide on whatever buffer remains."""
        if self._done:
            return []
        if not self._buf:
            return []
        ex = extract_tool_calls(self._buf, self._tool_names)
        buf = self._buf
        self._buf = ""
        self._done = True
        if ex is None:
            return [{"type": "content", "text": buf}]
        return [
            {"type": "tool_calls", "tool_calls": ex.tool_calls, "stop": True},
            *(
                [{"type": "content", "text": ex.content}]
                if ex.content else
                []
            ),
        ]

    # -- internals ---------------------------------------------------------
    def _should_release(self) -> bool:
        """Release only when we're sure no leak prefix is in flight."""
        if self._leak_open:
            # A leak candidate started; hold until it resolves (or max size).
            return len(self._buf) >= self._max
        if self._tail_holds():
            # Mark that a leak candidate is now in flight.
            self._leak_open = True
            return False
        return True

    def _tail_holds(self) -> bool:
        buf = self._buf
        tail = buf[-len(_HERMES_OPEN):] if len(buf) >= len(_HERMES_OPEN) else buf
        if _HERMES_OPEN.startswith(tail) or _HERMES_OPEN in buf[-32:]:
            return True
        fence = buf[-len(_FENCE_MARK):] if len(buf) >= len(_FENCE_MARK) else buf
        if _FENCE_MARK.startswith(fence) or buf.count(_FENCE_MARK) % 2 == 1:
            return True
        raw_open = bool(buf) and buf.rstrip().endswith(_BRACE) and _JSON_BRACE in buf[-64:]
        return raw_open
