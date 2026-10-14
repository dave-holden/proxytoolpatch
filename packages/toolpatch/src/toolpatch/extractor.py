"""Detect and extract tool-call payloads leaked into assistant ``content``.

Qwen (with a mismatched chat template) writes its tool call as text inside
``choices[*].message.content`` instead of the structured ``tool_calls`` field.
Recognition order (first hit wins):

1. Hermes XML:  <tool_call>{...}</tool_call>
2. Fenced JSON:  ```json {...} ```
3. Bare object:  {"name": ..., "arguments": ...} inside prose
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Literal

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

CallSource = Literal["hermes", "fenced", "raw_json"]


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


def _run(regex: re.Pattern[str], content: str, source: CallSource,
         tool_names: set[str] | None) -> ExtractedToolCalls | None:
    matches = list(regex.finditer(content))
    if not matches:
        return None
    log.debug("extractor probed %s: %d candidate(s)", source, len(matches))
    cleaned, calls = content, []
    for m in matches:
        # _HERMES_RE / _RAW_RE capture only the inner payload (group 1);
        # _FENCED_RE has a single capture and matches m[0] == payload.
        payload = m.group(1) if (regex.groups and m.group(1) is not None) else m.group(0)
        healed = heal_json(payload)
        if not healed:
            continue
        try:
            parsed = json.loads(healed)
        except json.JSONDecodeError:
            continue
        if not (isinstance(parsed, dict) and "name" in parsed):
            # The group-1 payload was arguments-only (hermes/raw call); the full
            # match is the JSON object to inspect.
            full = heal_json(m.group(0))
            parsed2 = json.loads(full) if full else None
            if isinstance(parsed2, dict) and "name" in parsed2:
                parsed = parsed2
            else:
                continue
        if tool_names is not None and parsed["name"] not in tool_names:
            continue
        calls.append(_to_tool_call(parsed))
        cleaned = cleaned.replace(m.group(0), "")
    if not calls:
        return None
    leftover = cleaned.strip()
    return ExtractedToolCalls(content=leftover or None, tool_calls=calls, source=source)


def extract_tool_calls(content: str,
                       tool_names: set[str] | None = None,
                       allow_unterminated_hermes: bool = False) -> ExtractedToolCalls | None:
    """Extract leaked tool calls; None when the content is clean (passthrough).

    A hermes open tag with no close tag yet means the call is still forming
    (mid-stream); the bare-JSON rule must not fire early or the open tag
    would leak out as leftover prose.
    """
    if _open_hermes_only(content):
        if not allow_unterminated_hermes:
            return None  # still forming mid-stream: hold, don't half-extract
        content = content + _HERMES_CLOSE  # tolerate the missing close at end
    for name, regex in (("hermes", _HERMES_RE), ("fenced", _FENCED_RE), ("raw_json", _RAW_RE)):
        hit = _run(regex, content, name, tool_names)  # type: ignore[arg-type]
        if hit is not None:
            return hit
    return None


def _open_hermes_only(content: str) -> bool:
    """True when a hermes open tag is present but not yet closed."""
    first_open = content.find(_HERMES_OPEN)
    if first_open == -1:
        return False
    first_close = content.find(_HERMES_CLOSE)
    return first_close == -1 or first_close < first_open
