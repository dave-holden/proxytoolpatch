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

## Environment

| Var | Default | Purpose |
|---|---|---|
| `LMW_UPSTREAM_URL` | `http://127.0.0.1:8181` | backend the proxy forwards to |
| `LMW_TRACE` | off | `1` = dump every payload transit point to stderr |
| `LMW_BACKEND` | `gguf` | `mlx` also strips `tool_choice` |

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

```bash
# End-to-end one-shot: rig with real model + middleware + both live suites:
./scripts/dev-rig.sh gguf $MPATH/qwen2.5-...gguf uv-test   # qwen suite
LMW_LIVE=1 uv run pytest apps/proxy/tests/test_live_opencode.py -q   # + opencode
```

PyCharm Run/Debug (watch payloads transit, breakpoints, fake-upstream config):
see **PLAN.md §6** in the repository root one level up.
