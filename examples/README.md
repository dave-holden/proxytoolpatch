# examples

Runnable, copy-paste example configs for every moving part of the setup.
Nothing here is loaded automatically — each file documents at its top where
it belongs and what to edit.

| File | What | Where it goes |
|---|---|---|
| [`proxytool/proxytool.toml`](proxytool/proxytool.toml) | The middleware's own config: server bind, upstream, managed backends, model map, LoopGuard, tracing | `~/.config/proxytool/config.toml` or `./proxytool.toml` next to where you run it |
| [`opencode-gguf.jsonc`](opencode-gguf.jsonc) | opencode client wired to `proxytool/qwen-gguf` (llama-server upstream) | `OPENCODE_CONFIG=./examples/opencode-gguf.jsonc` |
| [`opencode-mlx.jsonc`](opencode-mlx.jsonc) | opencode client wired to `proxytool/qwen-mlx` (mlx_lm.server upstream) | `OPENCODE_CONFIG=./examples/opencode-mlx.jsonc` |
| [`pi/models.json`](pi/models.json) | pi client provider + two model aliases through the proxy | `~/.pi/agent/models.json` (or `PI_MODELS_FILE`) |
| [`llama-swap/config.yml`](llama-swap/config.yml) | llama-swap gateway config: one llama-server per model, spawned on demand | `llama-swap --config examples/llama-swap/config.yml` |

## Stack recipes

**llama-swap as upstream** (several GGUF models, llama-swap spawns/stops
them): start llama-swap with the example above, then proxytool in front:

```bash
proxytool serve --upstream-url http://127.0.0.1:8080 \
  --model-map '{"qwen-gguf":"qwen2.5-coder-14b-instruct-q4_k_m"}'
```

**A single llama-server, no gateway**: no config file for the backend is
needed — llama-server takes flags. Either point proxytool at one already
running:

```bash
proxytool serve --upstream-url http://127.0.0.1:8181
```

or let proxytool own the process (spawn + health-check + clean shutdown, see
the `[managed.gguf]` block in [`proxytool/proxytool.toml`](proxytool/proxytool.toml)):

```bash
proxytool serve --backend gguf \
  --model /models/qwen2.5-coder-14b-instruct-q4_k_m.gguf
```

**mlx_lm.server**: same two options as above (`--upstream-url` or
`--model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit` — the `mlx` backend
kind is inferred from the id).

**Client aliases** (`qwen-gguf`, `qwen-mlx`) are meaningless to the upstream —
they must map to real model ids via `--model-map` or the `[model_map]` section
of the TOML example. Full flag/env/config reference:
[`apps/proxytool/README.md`](../apps/proxytool/README.md).
