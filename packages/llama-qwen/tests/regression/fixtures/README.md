# Regression fixtures

Each `*.json` file is one **golden pair**: (upstream response) → (expected repaired result).

## When to add one

Whenever the real Qwen/GGUF/MLX upstream produces a *new* kind of misformatting
(a leak shape the extractor mishandles, new JSON damage, etc.), capture the raw
upstream `content` (or SSE chunks) via `LMW_TRACE=1` logs and add it here.

## Format

```json
{
  "kind": "nonstream",            // or "stream"
  "upstream": { "content": "..." },          // nonstream: full message content
  //   or "upstream": { "chunks": ["...", "..."] }   // stream: content deltas
  "expect": {                     // nonstream
    "content": null,              // cleaned content (null/"" = nothing left)
    "tool_calls": [ { "id": "call_0", "type": "function",
                      "function": { "name": "...", "arguments": "{...}" } } ]
  }
  // stream expects { "events": [ ...StreamHealer event dicts... ] }
}
```

Tool-call `id`s are pinned by position (`call_0`, `call_1`, ...) because the
extractor generates random ids. Run `uv run pytest packages/llama-qwen/tests/regression -q`
after adding — the test prints the diverging result on failure.
