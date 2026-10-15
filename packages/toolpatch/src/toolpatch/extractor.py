"""Detect and extract tool-call payloads leaked into assistant ``content``.

Qwen (with a mismatched chat template) writes its tool call as text inside
``choices[*].message.content`` instead of the structured ``tool_calls`` field.
Recognition families (all merged into one result, document order):

1. Hermes XML:  <tool_call>{...}</tool_call>
2. Fenced JSON:  ```json {...} ```
3. Bare object:  {"name": ..., "arguments": ...} inside prose
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from toolpatch.fixer import heal_json
from toolpatch.logging import get_logger

log = get_logger("toolpatch.extractor")

_HERMES_OPEN = "<tool_call>"
_HERMES_CLOSE = "</tool_call>"

# Group 1 captures the JSON payload.
_HERMES_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_RAW_RE = re.compile(
    r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*(\{[^{}]*\}|\{.*?\}|"[^"]*")\s*\}',
    re.DOTALL,
)

CallSource = Literal["hermes", "fenced", "raw_json", "mixed"]


@dataclass(frozen=True)
class ExtractedToolCalls:
    content: str | None                 # cleaned content; None when nothing remains
    tool_calls: list[dict] = field(default_factory=list)
    source: CallSource | None = None


def _to_tool_call(parsed: dict) -> dict:
    args = parsed.get("arguments", {})
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": parsed["name"],
            "arguments": args if isinstance(args, str) else json.dumps(args),
        },
    }


def _parse_candidate(regex: re.Pattern[str], m: re.Match[str]) -> dict[str, Any] | None:
    """Heal and parse one regex match; return the {"name": ...} dict or None.

    The captured payload may be arguments-only (hermes/raw call): in that
    case fall back to the full match, which is the call object itself.
    """
    payload = m.group(1) if (regex.groups and m.group(1) is not None) else m.group(0)
    healed = heal_json(payload)
    if not healed:
        return None
    try:
        parsed = json.loads(healed)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and "name" in parsed:
        return parsed
    # The group-1 payload was arguments-only (hermes/raw call); the full
    # match is the JSON object to inspect.
    full = heal_json(m.group(0))
    if not full:
        return None
    try:
        parsed2 = json.loads(full)
    except json.JSONDecodeError:
        return None
    return parsed2 if isinstance(parsed2, dict) and "name" in parsed2 else None


def _collect_all(content: str,
                 tool_names: set[str] | None) -> ExtractedToolCalls | None:
    """Run every detector family and merge the distinct calls found.

    Multi-call turns — several Hermes blocks, parallel fenced blocks, or a
    mix of families — all land in ONE promotion frame, in document order.
    Overlapping/nested matches (e.g. a raw {"name"…} object inside a fenced
    block) dedupe to the outermost family, which is probed first.
    """
    found: list[tuple[int, int, dict[str, Any], CallSource]] = []
    detectors: tuple[tuple[CallSource, re.Pattern[str]], ...] = (
        ("hermes", _HERMES_RE),
        ("fenced", _FENCED_RE),
        ("raw_json", _RAW_RE),
    )
    for source, regex in detectors:
        for m in regex.finditer(content):
            span = m.span()
            if any(span[0] < e and s < span[1] for s, e, _, _ in found):
                continue  # nested/overlapping: an outer family already claimed it
            parsed = _parse_candidate(regex, m)
            if parsed is None or (
                tool_names is not None and parsed["name"] not in tool_names
            ):
                continue
            found.append((span[0], span[1], parsed, source))
    if not found:
        return None
    found.sort(key=lambda c: (c[0], c[1]))
    lo = found[0][0]
    hi = found[-1][1]  # nested/overlapping spans are deduped, so ends ascend
    # Whitespace OUTSIDE the leak envelope (pre-first / post-last span) is
    # model padding around the leak — strip it. Whitespace INSIDE the
    # envelope (between two leaked calls) is genuine inter-call prose —
    # keep it verbatim, including its edge spaces.
    head = content[:lo].rstrip()
    tail = content[hi:].lstrip()
    cleaned = head
    pos = lo
    for s, e, _, _ in found:
        cleaned += content[pos:s]
        pos = e
    cleaned += tail
    calls = [_to_tool_call(parsed) for _, _, parsed, _ in found]
    families_src: list[CallSource] = list(dict.fromkeys(
        src for _, _, _, src in found))
    source: CallSource = families_src[0] if len(families_src) == 1 else "mixed"
    log.debug("extract probed: %d call(s) from %s", len(calls),
              "+".join(families_src))
    return ExtractedToolCalls(content=cleaned or None, tool_calls=calls, source=source)


def extract_tool_calls(content: str,
                       tool_names: set[str] | None = None,
                       allow_unterminated_hermes: bool = False) -> ExtractedToolCalls | None:
    """Extract leaked tool calls; None when the content is clean (passthrough).

    Multiple distinct calls — Hermes, fenced, raw-JSON, in any mix — are
    all returned in a single promotion (document order), so a parallel or
    rapid-succession multi-tool turn promotes as one ``tool_calls`` array.

    A hermes open tag with no close tag yet means the call is still forming
    (mid-stream); the bare-JSON rule must not fire early or the open tag
    would leak out as leftover prose.
    """
    if _open_hermes_only(content):
        if not allow_unterminated_hermes:
            return None  # still forming mid-stream: hold, don't half-extract
        content = content + _HERMES_CLOSE  # tolerate the missing close at end
    return _collect_all(content, tool_names)


def _open_hermes_only(content: str) -> bool:
    """True when a hermes open tag is present but not yet closed."""
    first_open = content.find(_HERMES_OPEN)
    if first_open == -1:
        return False
    first_close = content.find(_HERMES_CLOSE)
    return first_close == -1 or first_close < first_open
