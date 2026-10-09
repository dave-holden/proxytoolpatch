# llama-middleware

Qwen **tool-calling template-mismatch** middleware: a FastAPI proxy that sits
between your agent client (opencode, curl, any OpenAI-compatible tool) and a
Qwen inference backend — `llama-server` (GGUF) or `mlx_lm.server` / `omlx` (MLX)
on the same `POST /v1/chat/completions` interface.

When Qwen's chat template is missing or mismatched, the model emits its tool
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

## The rig

```
┌────────────┐ 8080 proxy ports→ ┌───────────────────┐ 8181 upstream → ┌────────────────┐
│ opencode / │  8180 (this MW)   │ FastAPI proxy     │  llama-server    │ Qwen (GGUF/MLX)│
│ curl       │──────────────────►│ sanitize + repair │─────────────────►│ 14B            │
└────────────┘◄──────────────────┤ (this repo)       │◄─────────────────└────────────────┘
```

## Quickstart

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

PyCharm Run/Debug (watch payloads transit, breakpoints, fake-upstream config):
see **PLAN.md §6** in the repository root one level up.
