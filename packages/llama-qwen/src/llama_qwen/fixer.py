"""Heal malformed JSON emitted by Qwen inside tool-call payloads.

Repairs, in escalating order:
1. as-is (valid JSON passes through unchanged)
2. code-fence / smart-quote cleanup, then trailing-comma removal
3. trailing commas *and* raw newlines inside string literals
"""

from __future__ import annotations

import json
import re

_TRAILING_RE = re.compile(r",\s*([}\]])")
_FENCE_START = re.compile(r"^\s*```(?:json)?\s*")
_FENCE_END = re.compile(r"\s*```\s*$")
_SMART = str.maketrans({
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
})


def _unescape_stray_newlines_in_strings(s: str) -> str:
    """Escape raw newlines that appear inside JSON string literals."""
    out: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str and not esc and ch == "\n":
            out.append("\\n")
            continue
        out.append(ch)
        if ch == '"' and not esc:
            in_str = not in_str
        esc = in_str and ch == "\\" and not esc
    return "".join(out)


def heal_json(text: str) -> str | None:
    """Return a repaired JSON string, or None when unrecoverable."""
    if not text:
        return None
    c = _FENCE_START.sub("", text.strip())
    c = _FENCE_END.sub("", c)
    c = c.translate(_SMART)

    candidates = (
        c,
        _TRAILING_RE.sub(r"\1", c),
        _TRAILING_RE.sub(r"\1", _unescape_stray_newlines_in_strings(c)),
    )
    for attempt in candidates:
        try:
            json.loads(attempt)
            return attempt
        except json.JSONDecodeError:
            continue
    return None
