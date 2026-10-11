# llama-middleware

Qwen **tool-calling template-mismatch** middleware: a FastAPI proxy that sits
between your agent client (opencode, curl, any OpenAI-compatible tool) and a
Qwen inference backend — `llama-server` (GGUF) or `mlx_lm.server` / `omlx` (MLX)
on the same `POST /v1/chat/completions` interface.

when Qwen's chat template is missing or mismatched, the model emits its tool
call as **text inside** `choices[*].message.content` instead of the structured
`tool_calls` field — so clients print raw JSON in the TUI instead of executing
the tool. This middleware repairs it in-flight:

- Request side: validates/sanitizes `tools` + `tool_choice` (MLX drops
  `tool_choice`), without mutating unknown fields.
- Response side: extracts leaked tool calls (Hermes `<tool_call>…</tool_call>`,
  fenced ```json blocks, bare `{"name":...}` JSON in prose), heals broken JSON
  (trailing commas, raw newlines, smart quotes, fences) and rebuilds a clean
  OpenAI `tool_calls` payload — including during streaming, with bounded
  buffering that preserves time-to-first-token.
- Passthrough-first: clean requests/responses come out byte-equivalent.
- History sanitizer: merges/dedupes assistant-message shapes Qwen's chat template rejects (leaked-JSON twins of repaired tool calls, adjacent assistant turns), so multi-turn agent sessions don't hit llama-server `400`s.
- Stream termination guarantee: the repaired SSE stream always emits a `finish_reason` chunk before `[DONE]`, so agent clients close the turn instead of re-prompting.

**Status:** v0.2.0 — GGUF (llama-server) round-trip verified end-to-end in a live opencode session, including multi-step tool use. MLX is scaffolded (`tool_choice` strip, `config/opencode-mlx.jsonc`) and pending live verification.

## The rig

```
┌────────────┐ 8180 → ┌───────────────────┐ 8181 upstream → ┌────────────────┐
│ opencode / │        │ FastAPI proxy     │                 │ Qwen (GGUF/MLX)│
│ curl       │───────►│ sanitize + repair │────────────────►│ or fake        │
└────────────┘◄───────┤ (this repo)       │◄────────────────└────────────────┘
```

## One-command rig

Starts the upstream **and** the middleware together, waits for both health
checks, prints a ready smoke-test line, and runs until Ctrl-C:

```bash
./scripts/dev-rig.sh              # fake leaking upstream (no model download)
./scripts/dev-rig.sh gguf $MPATH/qwen2.5-coder-14b-instruct-q4_k_m.gguf
./scripts/dev-rig.sh mlx  mlx-community/Qwen2.5-Coder-14B-Instruct-4bit
./scripts/dev-rig.sh omlx mlx-community/Qwen2.5-Coder-14B-Instruct-4bit   # omlx serve

MW_PORT=8180 UP_PORT=8181       # port overrides
LMW_BACKEND=mlx ./scripts/dev-rig.sh mlx ...   # env still respected
```

## Quickstart (manual)

```bash
uv sync --all-packages          # one shared root .venv (workspace members editable)

# Terminal 1: fake upstream (or your real llama-server / mlx server on 8181)
uv run uvicorn scripts.fake_upstream:app --host 127.0.0.1 --port 8181

# Terminal 2: the middleware
LMW_TRACE=1 uv run uvicorn proxy.main:app --host 127.0.0.1 --port 8180

# Terminal 3: a client
curl -s http://127.0.0.1:8180/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen","stream":false,
       "messages":[{"role":"user","content":"list the files"}],
       "tools":[{"type":"function","function":{"name":"list_dir","parameters":{"type":"object","properties":{"path":{}}}}}]}' \
  | jq '.choices[0].message'
# → tool_calls populated; content clean (no leaked JSON)
```

## Real backends (port 8181)

```bash
# GGUF
llama-server --host localhost --port 8181 \
  --model $MPATH/qwen2.5-coder-14b-instruct-q4_k_m.gguf \
  --ctx-size 16384 --jinja

# MLX
python -m mlx_lm.server --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit --port 8181
```

## opencode configs

Two ready-made client configs (point opencode at the middleware's `/v1` so the
repair path is engaged — see the `/v1` suffix note inside each file):

| Config | Model id | Backend |
|---|---|---|
| `config/opencode-gguf.jsonc` | `llamamw/qwen-gguf` | llama-server (GGUF) |
| `config/opencode-mlx.jsonc` | `llamamw/qwen-mlx` | mlx_lm.server (MLX) |

```bash
export OPENCODE_CONFIG=./config/opencode-gguf.jsonc   # or opencode-mlx.jsonc
opencode
```

## pi (pi.dev) as a client

[pi](https://pi.dev/) is a second supported agent client (alongside opencode).
Both point at the same middleware `/v1` endpoint — same repair path, same
LoopGuard.

1. Install: `curl -fsSL https://pi.dev/install.sh | sh` (Node 22.19+)
2. Copy `config/pi-models.json` into `~/.pi/agent/models.json`
3. Run:

```bash
pi --provider llamamw --model qwen-mlx       # interactive
pi --provider llamamw --model qwen-mlx -p "..."   # one-shot print mode
pi --list-models llamamw                      # verify the provider is visible
pi --provider llamamw --model qwen-mlx -p "..."
pi --provider llamamw --model qwen-mlx -p "..." --format json      # non-interactive
pi --provider llamamw --model qwen-mlx -p "..." --format text      # plain text
```

Flags worth knowing: `/model` inside a session switches models without a
restart (models.json is re-read every time). The `compat` flags in
`config/pi-models.json` (`supportsDeveloperRole`, `supportsReasoningEffort`)
keep pi's request dialect compatible with local inference servers.

Quick smoke test (middleware health, raw tool round-trip, one-shot pi turn):

```bash
./scripts/pi-smoke.sh
MW=http://127.0.0.1:8180 ./scripts/pi-smoke.sh   # explicit middleware URL
```

## llama-swap / gateway upstreams

The middleware fronts llama-swap (or any OpenAI-compatible gateway) with:

```bash
LMW_UPSTREAM_URL=http://127.0.0.1:8080 ./scripts/dev-rig.sh gguf <model>  # or run uvicorn directly
```

- **Model routing** — llama-swap spins the backend from its own YAML config
  based on the `model` field our proxy sends. Client aliases (`qwen-gguf`,
  `qwen-mlx`) are resolved first via `LMW_MODEL_MAP`, then by probing
  upstream `/v1/models`; when several models are loaded, alias token overlap
  picks the closest match, preferring the artifact the server was launched
  with (a local snapshot path beats a same-name HF repo id).
- **Timeouts / no buffering** — `LMW_UPSTREAM_TIMEOUT` (default `300`s)
  covers llama-swap's on-demand model loads; SSE responses stream through
  unbuffered (the healer emits as chunks arrive, preserving TTFT).
- **Header preservation** — client `Authorization` and routing headers are
  forwarded per-request; additionally, `LMW_UPSTREAM_API_KEY` sends a static
  bearer token upstream (for `llama-server --api-key` or gated gateways).
- **Cold starts** — the first request after an idle period may take a while
  while llama-swap loads the model; that's what the timeout covers. If you
  set `LMW_LOOP_NUDGE`/`LMW_LOOP_ABORT` too low, a cold-start retry storm
  could look like a loop — keep the abort threshold above 3–4.

## Environment

| Var | Default | Purpose |
|---|---|---|
| `LMW_UPSTREAM_URL` | `http://127.0.0.1:8181` | backend the proxy forwards to |
| `LMW_BACKEND` | `gguf` | `mlx` also strips `tool_choice` |
| `LMW_UPSTREAM_TIMEOUT` | `300` | seconds to wait on an upstream request (SSE + cold starts) |
| `LMW_UPSTREAM_API_KEY` | *(empty)* | static bearer token sent as `Authorization` upstream |
| `LMW_MODEL_MAP` | *(empty)* | inline JSON `{"alias": "real-id"}` map |
| `LMW_MODEL_MAP_FILE` | *(empty)* | path to the same map as a JSON file |
| `LMW_LOOP_NUDGE` | `3` | identical requests before a nudge is appended (`0` = off) |
| `LMW_LOOP_ABORT` | `6` | identical requests before the request is aborted (`0` = off) |
| `LMW_TRACE` | off | `1` = dump every payload transit point to stderr |
| `LMW_TRACE_FILE` | *(stderr)* | path to append trace output to a file instead |

LoopGuard: consecutive byte-identical requests (same sanitized messages +
tools) are detected; after N repeats a nudge is appended telling the model to
stop, after M the middleware stops forwarding and returns a clean completion
explaining the cut-off ("LoopGuard: request aborted after N identical attempts"). A different request resets the count.

## Tests & checks

```bash
uv run pytest          # unit + regression + proxy integration (fixtures in packages/llama-qwen/tests/regression)
uv run ruff check .
uv run pyright
```

New real-world misformattings captured from your model should be added as
regression fixture pairs — see
`packages/llama-qwen/tests/regression/fixtures/README.md`.

### Live model test (opt-in)

Runs real requests through the middleware against a **live** Qwen backend,
asserts nothing leaks (no hermes tags, no raw JSON next to repaired
tool_calls, stream and non-stream), and prints a capture block ready to paste
into the regression-fixture dir if the model produces a novel misformatting:

```bash
# with the rig running a real model (see One-command rig above):  
LMW_LIVE=1 uv run pytest apps/proxy/tests/test_live_qwen.py -q

# or one-shot: rig up, test, tear down:
./scripts/dev-rig.sh gguf $MPATH/qwen2.5-...gguf uv-test
```

Env: `LMW_LIVE=1` to enable, `LMW_MIDDLEWARE_URL` (default `http://127.0.0.1:8180`)
and `LMW_LIVE_MODEL` (override the `model` string sent in requests).
Skipped (2 tests) unless it is set and the upstream health check passes, so
`pytest` stays green without it.

### Real client test (opencode, opt-in)

Beyond curl: runs the **actual `opencode` CLI** against the middleware — an
isolated temp config (via `OPENCODE_CONFIG`, so your
`~/.config/opencode` is never touched) defines a custom
`@ai-sdk/openai-compatible` provider pointed at `http://127.0.0.1:8180/v1`,
then `opencode run --format json` executes in a throwaway sandbox dir and we
assert that a real **structured tool call** survived opencode's stream parser
(`tool_use` events completed) with zero hermes-tag leakage:

```bash
# rig on 8181 + middleware on 8180, then:
LMW_LIVE=1 uv run pytest apps/proxy/tests/test_live_opencode.py -q
```

Also auto-skips when `opencode` is not on PATH. Extra env:
`LMW_OPENCODE_BIN` (defaults to `opencode`).

### Real client test (pi, opt-in)

Same idea as the opencode client test but for **pi**: an isolated temp
`PI_MODELS_FILE` (never touching `~/.pi/agent/models.json`) registers an
`openai-completions` provider pointed at `http://127.0.0.1:8180/v1`, then
`pi --provider llamamw --model qwen-mlx -p "..."` runs one-shot in a
throwaway sandbox dir. We assert the run completes, a structured tool call
survived pi's stream parser, and zero hermes-tag or raw-JSON leakage in the
rendered output:

```bash
# rig on 8181 + middleware on 8180, then:
LMW_LIVE=1 uv run pytest apps/proxy/tests/test_live_pi.py -q
```

Also auto-skips when `pi` is not on PATH. Extra env: `LMW_PI_BIN`
(defaults to `pi`), `LMW_LIVE_MODEL_PI` (defaults to `qwen-mlx`).

```bash
# End-to-end one-shots: rig with real model + middleware + all live suites:
./scripts/dev-rig.sh gguf $MPATH/qwen2.5-...gguf uv-test              # qwen suite
LMW_LIVE=1 uv run pytest apps/proxy/tests/test_live_opencode.py -q   # + opencode
LMW_LIVE=1 uv run pytest apps/proxy/tests/test_live_pi.py -q         # + pi
./scripts/pi-smoke.sh                                                 # quick pi smoke
```

PyCharm Run/Debug (watch payloads transit, breakpoints, fake-upstream config):
see **PLAN.md §6** in the repository root one level up.
