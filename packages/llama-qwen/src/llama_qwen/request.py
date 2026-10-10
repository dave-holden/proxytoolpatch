"""Outbound request normalization: give the backend its best shot at matching
its chat template, without mutating the caller's payload.

Backend-template history rules (from live opencode traces):

- Llama-server rejects histories ending in 2+ assistant messages
  ("Cannot have 2 or more assistant messages at the end of the list.").
- When the extractor repairs a leaked tool call, the client records BOTH the
  leaked text turn and the repaired ``tool_calls`` turn — the text twin must be
  re-attached to the structured turn to form one assistant message.
- Identical consecutive assistant turns (client double-append) are deduped.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

Backend = Literal["gguf", "mlx"]


def normalize_chat_request(
    payload: dict, backend: Backend = "gguf"
) -> tuple[dict, list[str]]:
    """Return (sanitized_payload, operator_notes). Input is deep-copied, never mutated."""
    out = copy.deepcopy(payload)
    notes: list[str] = []

    # Rule 1: tool_choice unsupported by mlx_lm.server — strip for MLX.
    if backend == "mlx" and "tool_choice" in out:
        del out["tool_choice"]
        notes.append("tool_choice stripped for mlx backend")

    # Rule 2: empty/null tools drop the key entirely.
    tools = out.get("tools")
    if tools is not None and not tools:
        del out["tools"]
        notes.append("empty tools array dropped")

    # Rule 3: parameters must be an object schema; drop offending tools.
    if isinstance(tools, list):
        good, for_name = [], {t.get("function", {}).get("name", "?") for t in tools}
        for t in tools:
            fn = t.get("function") or {}
            params = fn.get("parameters")
            if params is not None and not isinstance(params, dict):
                notes.append(f"tool '{fn.get('name', '?')}' parameters not object-schema; dropped")
                continue
            good.append(t)
        if tools and len(good) != len(tools):
            out["tools"] = good
        elif not tools:
            for_name = set()
        _ = for_name  # silence ruff if unused in future edits

    _sanitize_history(out["messages"], notes)
    return out, notes


def _sanitize_history(messages: list[dict[str, Any]], notes: list[str]) -> None:
    """Fix assistant-message history shapes llama-server rejects, in place.

    1. Merge an assistant *text* turn that is a leaked tool call (matches the
       raw-JSON shape the extractor targets) into the following assistant
       ``tool_calls`` turn when the tool name + arguments agree.
    2. Merge consecutive assistant messages into one (template allows only one
       assistant turn per generation).
    """
    if not isinstance(messages, list):
        return

    import json
    import re

    leaked = re.compile(r'\{\s*"name"\s*:')

    def _stem(m: dict[str, Any]) -> tuple | None:
        """(name, arguments-json) of a message's first tool call, else None."""
        if m.get("role") != "assistant":
            return None
        tcs = m.get("tool_calls") or []
        if not tcs:
            return None
        fn = (tcs[0] or {}).get("function") or {}
        return fn.get("name"), fn.get("arguments")

    merged = 0
    i = 1  # keep index 0 (system) untouched
    while i < len(messages):
        m = messages[i]
        if m.get("role") != "assistant":
            i += 1
            continue

        # 1. text-twins of a repaired tool_calls turn: `leaked` text immediately
        #    before an assistant tool_calls turn whose call matches.
        content = m.get("content")
        if (
            isinstance(content, str)
            and leaked.search(content)
            and i + 1 < len(messages)
        ):
            nxt = messages[i + 1]
            nxt_stem = _stem(nxt)
            if nxt_stem and not m.get("tool_calls"):
                try:
                    parsed = json.loads(content)
                    name, args_json = nxt_stem
                    args_norm = (
                        json.dumps(json.loads(args_json), sort_keys=True)
                        if isinstance(args_json, str)
                        else json.dumps(args_json, sort_keys=True)
                    )
                    parsed_args_norm = json.dumps(parsed.get("arguments"), sort_keys=True)
                    if parsed.get("name") == name and parsed_args_norm == args_norm:
                        # drop the text twin; the tool_calls turn already carries it.
                        messages.pop(i)
                        merged += 1
                        notes.append("leaked tool-call text turn merged into tool_calls turn")
                        continue
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    pass

        # 2. two assistant messages in a row: try to merge
        if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
            nxt = messages[i + 1]
            a_calls = m.get("tool_calls") or []
            b_calls = nxt.get("tool_calls") or []
            if a_calls or b_calls:
                if not b_calls:
                    # m carries the structured call; keep it, adopt nxt's text
                    # as the turn content when m has none.
                    if not (m.get("content") or "").strip():
                        m["content"] = (nxt.get("content") or "").strip()
                    messages.pop(i + 1)
                    merged += 1
                    notes.append("assistant turns merged (text into tool_calls)")
                    continue
                if not a_calls:
                    # nxt carries the structured call; adopt m's text as content
                    # when nxt has none.
                    if not (nxt.get("content") or "").strip():
                        nxt["content"] = (m.get("content") or "").strip()
                    messages.pop(i)
                    merged += 1
                    notes.append("assistant turns merged (text into tool_calls)")
                    continue
                # both have tool_calls: merge into the second (tool results follow)
                nxt["tool_calls"] = a_calls + b_calls
                messages.pop(i)
                merged += 1
                notes.append("assistant turns merged (tool_calls + tool_calls)")
                continue
            # plain text duplicates
            if (m.get("content") or "") == (nxt.get("content") or ""):
                messages.pop(i)
                merged += 1
                notes.append("duplicate adjacent assistant turns deduped")
                continue
            # distinct assistant texts: keep the LAST one (the final generation)
            nxt["content"] = (
                (m.get("content") or "") + "\n" + (nxt.get("content") or "")
            ).strip()
            messages.pop(i)
            merged += 1
            notes.append("assistant turns merged (text + text)")
            continue

        i += 1

    if merged:
        notes.append(f"assistant history sanitized: {merged} merge(s)")
