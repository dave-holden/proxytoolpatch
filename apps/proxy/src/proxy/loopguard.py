"""LoopGuard: detect and break identical-prompt repetition loops.

When a client re-prompts with a byte-identical message history (after
sanitization), llama-server tends to replay the same continuation — a loop.
The middleware counters this in two steps:

1. **nudge** after ``nudge_after`` identical consecutive requests: append a
   short user message asking the model to finish the turn and stop.
2. **abort** after ``abort_after``: stop forwarding upstream entirely and
   return a clean, short OpenAI-style completion telling the user how to
   break the loop (rephrase or start a new session). This bounds the loop
   at ``abort_after`` requests instead of 100+.

State is per-process and deliberately simple: a counter keyed by the request
fingerprint, reset whenever a *different* request (a new user message or
changed history) arrives. Long-identity loops are the only failure mode we
guard against.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

NUDGE_TEXT = (
    "[system-reminder] You have produced the same answer several times in a row. "
    "That previous answer is already recorded. Do NOT repeat it. Either call a "
    "tool you have not called yet, or give a short final answer that completes "
    "the user's request, then stop."
)

ABORT_TEXT = (
    "**LoopGuard: request aborted after {count} identical attempts.**\n\n"
    "The model kept producing the same reply for the same prompt, so the "
    "middleware stopped forwarding to avoid burning tokens. To continue:\n"
    "- Rephrase your message (even slightly), or\n"
    "- Start a new session.\n\n"
    "This is a safety cut-off, not a model answer."
)


@dataclass(frozen=True)
class GuardDecision:
    action: str  # "none" | "nudge" | "abort"
    count: int


class LoopGuard:
    """Track consecutive identical requests per conversation fingerprint."""

    def __init__(self, nudge_after: int = 3, abort_after: int = 6) -> None:
        self.nudge_after = nudge_after
        self.abort_after = abort_after
        self._state: dict[str, int] = {}
        self._last_key: str | None = None

    # -- fingerprinting ----------------------------------------------------
    @staticmethod
    def request_key(payload: dict[str, Any]) -> str:
        """Stable fingerprint of the *conversation* the model will see."""
        return hashlib.sha256(
            json.dumps(
                {
                    "messages": payload.get("messages"),
                    "tools": payload.get("tools"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    # -- decision flow ------------------------------------------------------
    def check(self, key: str) -> GuardDecision:
        """Record the incoming request and return the action to take."""
        if key != self._last_key:
            # A genuinely new request arrived: reset.
            self._last_key = key
            self._state = {key: 1}
            return GuardDecision("none", 1)
        count = self._state.get(key, 0) + 1
        self._state[key] = count
        if self.abort_after and count >= self.abort_after:
            return GuardDecision("abort", count)
        if self.nudge_after and count >= self.nudge_after:
            return GuardDecision("nudge", count)
        return GuardDecision("none", count)

    def reset(self) -> None:
        self._last_key = None
        self._state.clear()
