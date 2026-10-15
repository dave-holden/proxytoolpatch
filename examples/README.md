# examples

Runnable, copy-paste example configs for every moving part of the setup.
Nothing here is loaded automatically — each file documents at its top where
it belongs and what to edit.

| File | What | Where it goes |
|---|---|---|
| [`proxytool/proxytool.toml`](proxytool/proxytool.toml) | The middleware's own config: server bind, upstream, routing, model map, LoopGuard, tracing | `~/.config/proxytool/config.toml` or `./proxytool.toml` next to where you run it |
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

or run the server yourself in this same terminal before proxytool (the
relevant llama-server flags live upstream, not in proxytool):

```bash
llama-server --host 127.0.0.1 --port 8181 --jinja \
  --model /models/qwen2.5-coder-14b-instruct-q4_k_m.gguf &
proxytool serve --upstream-url http://127.0.0.1:8181
```

**mlx_lm.server**: start it the same way (`python -m mlx_lm.server --model
mlx-community/Qwen2.5-Coder-14B-Instruct-4bit --port 8181`), then point
proxytool's `--upstream-url` at it.

**Client aliases** (`qwen-gguf`, `qwen-mlx`) are meaningless to the upstream —
they must map to real model ids via `--model-map` or the `[model_map]` section
of the TOML example. Full flag/env/config reference:
[`apps/proxytool/README.md`](../apps/proxytool/README.md).
