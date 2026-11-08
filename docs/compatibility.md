# Server & client compatibility matrix

How local LLM servers and coding agents work when **proxytool** sits between
them — verified by runnable scenario scripts, not by review. Two ladders feed
this page:

- **The proxy ladder** (server per rung, clients grow): 
  one server → a model alias → a real agent client → proxytool's own 
  routing → four backends → a gateway → hybrid gateway-or-direct → the full 
  stack with streaming.

- **The mlx-serve ladder** (proxy wiring per rung): 
  one pinned model → a mixed GGUF/MLX store → the store behind a llama-swap 
  peer → `mlx_lm.server` in a throwaway venv.

Every rung below was asserted in a live run: the proxy returns a structured
`tool_calls` where the raw backend leaked text, the trace names the backend
that handled each request, and — for the agent clients — a real tool-using
turn completes.

## The matrix

| Server setup | How proxytool attaches | Model ids you'll see | Agent clients verified | Notes / gotchas |
|---|---|---|---|---|
| `llama.cpp` (`llama serve` …, OpenAI endpoint) | single upstream: `--upstream-url`, `PTP_UPSTREAM_URL`, or `[upstream] url` | repo/quant id, e.g. `qwen-gguf` | curl, pi, opencode | GGUF runs sometimes wrap the call in `<tools>{…}</tools>` / `<function>{…}` markup — repaired on stream and off stream |
| `mlx_lm.server` | single upstream **plus `--model-map`** | the HF repo id, e.g. `mlx-community/...` | curl, pi | a client alias the server doesn't know fails as an HF repository error, not a clean 404 — the alias rewrite in `[model_map]` is load-bearing |
| oMLX | single upstream or one leg of proxytool routing | id = the directory name under its model dir | curl, pi | oMLX's default `balanced` memory guard can refuse prefill while a GGUF neighbour holds RAM (`--memory-guard aggressive` fixes cohabitation) |
| llama-swap as router, backends spawned by `cmd:` | proxytool's single upstream points at llama-swap | llama-swap's model keys (aliases optional per backend) | curl, pi, opencode | gateway owns model residency (spawn/evict); use `checkEndpoint: /v1/models` for servers without `/health` and `useModelName` when the client id ≠ the server's id |
| `mlx-serve` — one pinned model (`--model <path>`) | single upstream, any spelling | the snapshot **hash**, not a friendly name; requests still route to the single loaded model | curl, opencode, pi | fine for a lone model; for stable ids use a `--model-dir` store |
| `mlx-serve` — mixed GGUF + MLX store (`--model-dir`) | single upstream, config-file `[upstream]` spelling verified | id = the subdirectory name under the store | curl, opencode, pi | GGUF writes need an explicit `--ctx-size`: the auto-admission heuristic squeezed per-request budgets to under ~1k tokens, 400-ing real agent prompts (8192 verified on both legs; 16384 can over-commit the MLX side) |
| llama-swap + `mlx-serve` as a **peer** (`peers:` block, never spawned) | proxytool → llama-swap → mlx-serve (two hops) | `<peer-name>/<id>` (plain ids also route while unambiguous) | curl, opencode, pi | the peer manages its own LRU residency; llama-swap must not `cmd:`-spawn it |
| Hybrid: gateway **and** a direct upstream | proxytool routing table (`--upstream NAME=…`, `[routing.<name>]`) | per-leg ids as above | pi | both mechanisms in one proxy; the trace shows which leg answered per request |
| Full stack (gateway + direct + streaming) | routing table | all of the above | curl (SSE), pi, opencode | stream terminates with `[DONE]`, carries a `tool_calls` delta, and must not leak `<tools>`/`<function…>` markup into `content`; LoopGuard is observable end-to-end |


## Where the pieces live

**proxytool** is the process your agent talks to; it owns configuration
(flags / env vars / TOML — behaviourally equivalent, precedence flags > env >
file), routing, health, model listing, trace and LoopGuard.

**toolpatch** is proxytool's repair engine. Running servers and clients do
not import it: the inference servers in the matrix never load it, and
opencode/pi don't know it exists. It patches the HTTP response stream as it
passes through the proxy:

```
coding agent ──> proxytool (toolpatch: extractor + StreamHealer) ──> model server
```

It recognises supported textual tool-call formats, normalises them into
OpenAI-style `tool_calls`, strips leaked wrapper markup, and repairs the
streamed variants.

The package is also a plain importable library — published on PyPI as
`toolpatch` — and is used that way directly in the project's own test and
pilot harnesses (for example, the mlx-serve pilot replays live responses
through `toolpatch.extractor` and `toolpatch.stream`'s `StreamHealer` to
verify repair against real server output). Shipped integrations go
through proxytool; direct library use is supported API, exercised by these
harnesses.

## Reproducing these results

The scenario scripts are deliberately public-shaped (scratch configs, no
global state touched, per-rung teardown) but live in the local `dev/` tree,
not in the published repo. The durable, public artifacts are:

- [`examples/`](../examples/README.md) — checked-in configs for llama-swap,
  proxytool and the pi/opencode clients, matching the shapes above.
- [`apps/proxytool/README.md`](../apps/proxytool/README.md) — configuration
  reference (flags, env vars, TOML, routing, trace, LoopGuard).

Minimal requirements to run a single-upstream rung: an OpenAI-compatible
server (`llama serve`, `mlx_lm.server`, oMLX, `mlx-serve` …), one GGUF or
MLX model in the HF cache, and proxytool installed. Gateway rungs add
llama-swap. Ports used by the ladder: one per backend, **8180 for proxytool**
in every rung, so client base URLs never change as the ladder grows.

## Status

Verified on Apple Silicon (macOS) against current-at-the-time builds of
llama.cpp, `mlx-lm`, oMLX, llama-swap and mlx-serve. Compatibility is
behavioural — the assertions are about messages on the wire, not log lines —
but exact tool-call shapes still vary per model build, so treat the matrix
as "verified at least this well", runnable script as proof, not promise.
