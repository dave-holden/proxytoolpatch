"""Outbound request normalization: give the backend its best shot at matching
its chat template, without mutating the caller's payload."""

from __future__ import annotations

import copy
from typing import Literal

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

    return out, notes
