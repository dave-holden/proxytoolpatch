<p align="center">
  <img src="public/assets/images/toolpatch.png" alt="toolpatch" width="200">
</p>

# toolpatch

**In plain terms:** this is the *fix-it library*. Local AI models sometimes
send their tool commands as garbled text inside the chat message instead of
the structured format your software can execute. Feed the mumbled reply to
toolpatch and get back a clean, structured tool call. It doesn't serve
anything by itself — you embed it in your own Python code, or let
**proxytool** use it for you.

A **pure, zero-I/O Python library** for repairing LLM tool-calling
protocol breakage: no sockets, no event loop, no CLI, nothing runs on its
own — it is meant to be embedded by any server, proxy, or script that talks
to local LLMs over an OpenAI-compatible interface.

This is the repair "brick" that **proxytool** (`apps/proxytool`) embeds for all
of its protocol-repair logic:

```
proxytool  (the runnable app: HTTP serving, routing, supervision)
    └─ imports
toolpatch (this library: pure text repair, no I/O)
```

Any Python program can do the same `import` — see [Embedding](#embedding).

## Install

```bash
pip install toolpatch          # once published to PyPI
# or
uv add toolpatch               # into an existing uv project
```

In this workspace (development install, editable):

```bash
uv sync --all-packages         # one shared root .venv, both modules included
```

## Quick use

One completed (non-streaming) model reply in, clean result out:

```python
from toolpatch import extract_tool_calls

reply = 'Sure. \u003ctool_call\u003e{"name": "list_dir", "arguments": {"path": "."}}\u003c/tool_call\u003e'

ex = extract_tool_calls(reply, tool_names={"list_dir"})
if ex:
    ex.content      # "Sure."  (clean prose, leak gone)
    ex.tool_calls   # [{'id': 'call_…', 'type': 'function',
                    #   'function': {'name': 'list_dir', 'arguments': '{"path": "."}'}}]
```

That is the whole loop for a completed reply. For streaming replies use
`StreamHealer.feed()`/`.finish()`, and for outbound request sanitization use
`normalize_chat_request` — all three below in [API](#api).

## What it repairs

When a model's chat template is missing or mismatched, some local servers
emit the tool call as **text inside** `choices[*].message.content` instead
of the structured `tool_calls` field. The library recognizes three leaked
payload shapes (all merged into one result, in document order):

1. **Hermes-style XML function tags** — an XML tag block wrapping a JSON
   payload, the form Qwen and several other open models emit when they leak.
2. **Fenced JSON** — ```json … ``` code blocks.
3. **Bare JSON object** — a raw `{"name": …, "arguments": …}` embedded in
   prose (the extractor checks the keys and heals the payload before
   accepting it).

Plus the supporting repairs:

- **JSON healing** — trailing commas, raw newlines inside string literals,
  smart quotes, code fences.
- **History normalization** — merges assistant-turn shapes that
  llama-server-style chat templates reject with 4xx (leaked-JSON "twins" of
  repaired tool calls, adjacent assistant turns).
- **Streaming** — `StreamHealer` does the same work on SSE content deltas
  with bounded buffering (time-to-first-token is preserved). It is an
  in-flight accumulator, not "buffer until the first complete match": clean
  prose is released as soon as no leak-shape opener is in flight, with a
  10-char suffix window so an opener straddling two deltas is re-judged
  whole. Reasoning-tag-wrapped leaks (`gpt-think … <tool_call>` mid-buffer) are held
  by a mid-buffer opener check, not just tail rules — the Qwen2.5 mlx
  pattern that motivated it. Once a leak resolves, the healer switches to
  *collecting* mode and defers promotion to stream end (bounded by a
  4×`max_buffer` cap), so rapid-succession or parallel blocks — mixed
  families included — all land in **one** `tool_calls` frame instead of
  promoting the first call and suppressing the rest. Whitespace between two
  leaked calls survives as real inter-call prose; only padding outside the
  leak envelope is stripped.

## API

The public surface is deliberately small — three callables plus the stream
helper and one dataclass, all re-exported from `toolpatch`:

```python
from toolpatch import extract_tool_calls, heal_json, StreamHealer, normalize_chat_request
from toolpatch import ExtractedToolCalls  # dataclass: content, tool_calls, source
```

### `extract_tool_calls(content, tool_names=None,
                        allow_unterminated_hermes=False)`

`-> ExtractedToolCalls | None`

Runs all three detectors over the text and merges every distinct call into
one result, in document order; overlapping/nested matches dedupe to the
outer family. Returns `None` for clean prose (passthrough); on a hit
returns the cleaned content plus the structured tool calls. Pass
`tool_names={...}` to only accept calls whose `name` is in the set
(anything else stays prose). `allow_unterminated_hermes=True` is the
streaming path's opt-in: it tolerates one still-forming Hermes block at the
end of the text.

`ExtractedToolCalls.source` names the matching family (`hermes` / `fenced`
/ `raw_json`) or `mixed` when families differ across the calls.

```python
ex = extract_tool_calls('Sure. ' + hermes_block)
ex.content                                  # 'Sure.' (leak text removed)
ex.source                                  # 'hermes'
ex.tool_calls[0]['function']['name']        # 'list_dir'
```

### `heal_json(text) -> str | None`

Repairs malformed JSON from a model payload: strips code fences, un-smarts
quotes, drops trailing commas, escapes raw newlines that broke a string
literal. Returns the repaired JSON string, or `None` when unrecoverable.

### `StreamHealer(max_buffer=512, tool_names=None)`

Streaming variant, for SSE content deltas. Feed it partial text as it
arrives; it buffers only while a tool-call leak might be forming (bounded by
`max_buffer`) and yields plain-dict events. Boundary safety is built in:
fragments are re-judged over the whole buffer each delta, with the last
`max(11, 8)−1 = 10` trailing chars held back (the 11-char `<tool_call>` opener
vs the 8-char `{"name"` probe, minus one), so a leak opener split across
two deltas is seen whole once the next delta completes it. A release slice
 containing a leak shape is re-held instead of emitted — the raw leak text
is never forwarded, so clients can't render duplicate content. After the
first call resolves, promotion defers to stream end (4×`max_buffer` cap so
an endless trailing ramble still promotes): all subsequent calls land in
the one frame; residual prose between them comes out in the same batch,
before the `tool_calls` event.

```python
healer = StreamHealer()

for delta in sse_content_deltas:
    for event in healer.feed(delta):
        match event["type"]:
            case "content":
                pass  # safe to forward to the client as-is
            case "tool_calls":
                ...   # event["stop"] is True — one frame per turn;
                      # contains ALL calls seen (file, webfetch, …)

for event in healer.finish():   # flush at stream end
    ...
```

Event shapes: `{"type": "content", "text": str}` and
`{"type": "tool_calls", "tool_calls": [...], "stop": True}`.

### `normalize_chat_request(payload, backend="gguf") -> (dict, list[str])`

Sanitizes an outbound chat-completion request without mutating the input
(deep-copies). `backend="mlx"` additionally strips `tool_choice`
(unsupported by `mlx_lm.server`). Returns the sanitized payload plus
operator notes describing each fix. History fixes: leaked tool-call text
turns merged into the following `tool_calls` turn, adjacent assistant turns
merged or deduped — the shapes llama-server templates reject with `400`s.

```python
clean, notes = normalize_chat_request(payload, backend="gguf")
if notes:
    log.info("sanitized request", extra={"notes": notes})
```

## Embedding

Concretely, other Python consumers that can embed this today:

- **Open WebUI Pipelines** (Python) — run `extract_tool_calls` on the model
  response before it reaches the client.
- **LiteLLM custom callbacks** (Python) — hook extraction into the response
  path.
- **FastAPI / FastMCP / custom servers** fronting a local model — call
  `normalize_chat_request` before forwarding upstream.
- **Plain OpenAI-SDK scripts** — wrap every `chat.completions.create` with
  `normalize_chat_request` on send and `extract_tool_calls` on receive.

Non-Python tools (Ollama, llama-server's C++ internals, llama-swap, etc.)
cannot import a Python library — for those, the recommended integration is
**proxytool** in front of the backend over HTTP, which applies this same
repair logic.

## Relationship to the app

One-way dependency:

- `apps/proxytool` (proxytool) imports this library in-process.
- This library never imports the app, never opens sockets, and its test
  suite is fully offline (`packages/toolpatch/tests`) — it does not need
  the proxy or a model to run.

Real-world misformattings captured from live models are kept as regression
fixture pairs — see `tests/regression/fixtures/README.md`.

## Logging

The library is **silent by default** — it logs through the stdlib `logging`
system (logger names `toolpatch.extractor`, `toolpatch.stream`, …) but never
configures handlers or levels itself; a library shouldn't. In a bare script
nothing is emitted. To turn repair diagnostics on:

```python
from toolpatch import configure_logger

configure_logger("toolpatch", "debug")   # or "info"/"warning"/"error"
```

(Host apps like proxytool own this: proxytool's `--log-level debug` also
raises the library's logger — one knob for everything.)

## Status

`v1.0.0`. Depends only on `pydantic`
(a candidate for stdlib-only before publishing — see root `PLAN.md`).
