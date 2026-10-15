"""StreamHealer: repair tool-call leaks inside SSE content deltas.

Bounded streaming repair: deltas accumulate in a small buffer. Clean prose is
released quickly; text that *might* be forming a tool call is held until the
extractor can decide. Emits plain-dict events consumed by the proxy without
further translation.

The hold rule is NOT tail-only: models like Qwen2.5 stream a reasoning block
(<tool_call>-style tag) that appears BEFORE the tool-call payload in the same
buffer, and a tail-prefix-only check releases the whole buffer as "prose"
because the leak prefix sits mid-buffer. The hold rule therefore also fires
when a leak prefix appears **inside** the buffer, not just at the end.
"""

from __future__ import annotations

from typing import Any

from toolpatch.extractor import extract_tool_calls
from toolpatch.logging import get_logger

log = get_logger("toolpatch.stream")

# Markers that suggest a leak may be forming; while any prefix-match lingers we hold.
# ALL of these are prefix-triggered hold signals — a tail that could still grow
# into one of them keeps the buffer held. This covers reasoning/thinking tags
# (Qwen gpt-think, provider variations) that stream RIGHT BEFORE a leak: the
# whole 'reasoning + Hermes' sequence holds until the extractor resolves it.
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

    Multi-call aggregation: once any tool-call leak has been seen, the
    healer enters *collecting* mode — no further prose is released and the
    promotion is deferred to the stream end (or a 4x``max_buffer`` cap), so
    rapid-succession or parallel blocks (Hermes/fenced/raw, in any mix) all
    land in ONE ``tool_calls`` frame instead of promoting the first call
    and suppressing the rest.
    """

    def __init__(self, max_buffer: int = 512,
                 tool_names: set[str] | None = None,
                 logger: Any | None = None) -> None:
        self._max = max_buffer
        self._tool_names = tool_names
        self._buf = ""
        self._holding = False
        self._leak_open = False  # a partial leak prefix is in flight
        self._done = False  # a leak was extracted; suppress plain content after
        self._seen_call = False  # collecting mode: aggregate until stream end
        self._cap = max_buffer * 4  # collecting-mode bound
        self._log = logger or log

    # -- public ------------------------------------------------------------
    def feed(self, content_delta: str) -> list[dict]:
        if self._done or not content_delta:
            return []
        self._buf += content_delta
        events: list[dict] = []

        # Try to decide with what we have (cheap probe: full extractor on buffer).
        ex = extract_tool_calls(self._buf, self._tool_names)
        if ex is not None:
            self._seen_call = True
        if self._seen_call:
            # Collecting mode: more calls (rapid succession, parallel
            # blocks, mixed families) may still be streaming downstream, so
            # the promotion waits for the stream end — they all land in ONE
            # ``tool_calls`` frame. Bounded by ``_cap`` for streams that
            # rack up endless trailing content after a call.
            if len(self._buf) >= self._cap:
                self._log.debug("StreamHealer cap reached; promoting buffer")
                return self._promote(allow_unterminated=False)
            return events

        if self._should_release():
            # Retain a small suffix window, NOT an empty buffer: a leak opener
            # straddling two releases (think>… <tool_ca | ll>{"name") must be
            # seen whole once the next delta completes it. Everything BEFORE
            # the window is emitted; the window itself stays buffered and is
            # re-judged with the next delta (so it is never emitted twice).
            keep = max(len(_HERMES_OPEN), len(_JSON_BRACE)) - 1
            emit, self._buf = self._buf[:-keep], self._buf[-keep:]
            if emit:
                # A leak callee can start inside the release slice itself:
                # with a partial open tag at the slice boundary the extractor
                # cannot yet resolve a call, so re-judge the WHOLE slice
                # before emitting it as prose. If an open/unresolvable tag is
                # now in the slice, un-release: re-buffer and hold.
                if _HERMES_OPEN in emit or self._midbuf_leak(emit):
                    self._buf = emit + self._buf
                    self._leak_open = True
                    self._log.debug("StreamHealer un-releasing slice with leak shape")
                else:
                    events.append({"type": "content", "text": emit})
        return events

    def finish(self) -> list[dict]:
        """Flush the stream end: decide on whatever buffer remains."""
        if self._done:
            return []
        if not self._buf:
            return []
        return self._promote(allow_unterminated=True)

    def _promote(self, allow_unterminated: bool) -> list[dict]:
        """Resolve the buffer: emit residual content once, then the calls.

        Single promotion point for both feed-time cap overflow and the
        stream end. An unterminated Hermes tag is only tolerated at the
        very end of the stream; mid-stream caps flush what resolved.
        """
        ex = extract_tool_calls(self._buf, self._tool_names,
                                allow_unterminated_hermes=allow_unterminated)
        buf = self._buf
        self._buf = ""
        self._done = True
        self._seen_call = False
        if ex is None:
            if buf:
                return [{"type": "content", "text": buf}]
            return []
        events: list[dict] = []
        # Residual prose (retained window, reasoning tags' text,
        # pre/post-object preamble) must survive — emitted BEFORE the
        # promotion frame so a client renders it around the same turn.
        if ex.content:
            events.append({"type": "content", "text": ex.content})
        events.append({
            "type": "tool_calls",
            "tool_calls": ex.tool_calls,
            "stop": True,
        })
        self._log.debug("StreamHealer extracted tool calls: %d call(s)",
                        len(ex.tool_calls))
        return events

    # -- internals ---------------------------------------------------------
    def _should_release(self) -> bool:
        """Release only when we're sure no leak prefix is in flight."""
        if self._leak_open:
            # A leak candidate started; hold until it resolves (or max size).
            if len(self._buf) >= self._max:
                self._log.debug("StreamHealer releasing held buffer at max size")
                return True
            return False
        if self._tail_holds():
            # Mark that a leak candidate is now in flight.
            self._leak_open = True
            self._log.debug("StreamHealer holding: possible leak opener detected")
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
        # Any leading '{' opens a candidate tool-call object until either the
        # extractor resolves it (feed re-checks each delta) or max_buffer
        # forces release. The replayed mlx stream showed a bare {"name"...}
        # object released before its arguments were complete.
        stripped = buf.lstrip()
        # Hold while the buffer is *only* a candidate JSON object (nothing but
        # whitespace before the opening brace) and still under max_buffer.
        if (
            stripped.startswith(_BRACE)
            and len(stripped) < self._max
            and not buf[: len(buf) - len(stripped)].strip()
        ):
            return True
        # Mid-buffer leak start (reasoning-tag-wrapped leaks, live Qwen2.5
        # mlx trace): a Hermes open / bare-JSON object that BEGINS inside
        # already-released prose, not at the tail. Tail-only rules let the
        # whole reasoning+leak sequence release as prose; hold until the
        # extractor resolves (bounded by max_buffer, as above).
        return self._midbuf_leak(buf)

    @staticmethod
    def _midbuf_leak(buf: str) -> bool:
        """True when a leak-shape opener sits anywhere inside the buffer.

        Cheaper and more conservative than running the full extractor: any
        Hermes open tag or bare `{"name"` object beginning mid-buffer is a
        hold signal. Fences are already covered by the tail rule's parity
        check.
        """
        return _HERMES_OPEN in buf or _JSON_BRACE in buf
