# examples

Runnable, copy-paste example configs for every moving part of the setup.
Nothing here is loaded automatically — each file documents at its top where
it belongs and what to edit.

Ports, startup order and the per-server commands live in `dev/RIG.md`
(local-only, not in this repo).

| File | What | Where it goes |
|---|---|---|
| [`proxytool/proxytool.toml`](proxytool/proxytool.toml) | The proxytool's own config: server bind, upstream, routing, model map, LoopGuard, tracing | `~/.config/proxytool/config.toml` or `./proxytool.toml` next to where you run it |
| [`proxytool/proxytool-routing.toml`](proxytool/proxytool-routing.toml) | Same, for the gateway-less rig: four backends on fixed ports 8181/8182/8281/8381, routed by model id | `proxytool serve --config examples/proxytool/proxytool-routing.toml` |
| [`opencode-gguf.jsonc`](opencode-gguf.jsonc) | opencode client wired to `proxytool/qwen-gguf` (llama-server upstream) | `OPENCODE_CONFIG=./examples/opencode-gguf.jsonc` |
| [`opencode-mlx.jsonc`](opencode-mlx.jsonc) | opencode client wired to `proxytool/qwen-mlx` (mlx_lm.server upstream) | `OPENCODE_CONFIG=./examples/opencode-mlx.jsonc` |
| [`opencode-swap.jsonc`](opencode-swap.jsonc) | opencode client listing all four models (three swap-demo + the oMLX Phi) | `OPENCODE_CONFIG=./examples/opencode-swap.jsonc` |
| [`pi/models.json`](pi/models.json) | pi client provider + four model aliases (`qwen-gguf`, `qwen-mlx`, `phi-4-mini`, `phi-4-mini-mlx`) through the proxy | `~/.pi/agent/models.json` (or `$PI_CODING_AGENT_DIR/models.json`) |
| [`llama-swap/config.yml`](llama-swap/config.yml) | llama-swap gateway: three models in one swap group, spawned on demand | `llama-swap --config examples/llama-swap/config.yml` |

## Three-model swap demo

Qwen2.5-Coder-14B in GGUF **and** MLX, plus Phi-4-mini Q5_K_M — one model
resident at a time, swapped on demand by llama-swap.

**0. Once:** install the backends and prefetch the three models (~20 GB):

```bash
brew install llama.cpp                # `llama` → `llama serve`
uv tool install mlx-lm                # mlx_lm.server
hf download Qwen/Qwen2.5-Coder-14B-Instruct-GGUF \
  --include '*q4_0.gguf'              # qwen-gguf, 8.5 GB
hf download mlx-community/Qwen2.5-Coder-14B-Instruct-4bit   # qwen-mlx, 8.3 GB
hf download bartowski/microsoft_Phi-4-mini-instruct-GGUF \
  --include '*Q5_K_M.gguf'            # phi-4-mini, 2.8 GB

hf cache ls --no-truncate             # confirm all three are on disk
```

**1. Terminal 1 — llama-swap** (gateway on :8080):

```bash
llama-swap --config examples/llama-swap/config.yml --listen 127.0.0.1:8080
```

**2. Terminal 2 — proxytool** (repair layer on :8180):

```bash
cp examples/proxytool/proxytool.toml ./proxytool.toml   # timeout 600 for cold loads
proxytool start        # or: proxytool serve
proxytool health                                          # proxytool is up
curl -s http://127.0.0.1:8180/v1/models \
  | python3 -c "import json,sys; print([m['id'] for m in json.load(sys.stdin)['data']])"
```

The last command must list `qwen-gguf`, `qwen-mlx` and `phi-4-mini` (llama-swap
orders them itself) — that is what the clients receive, and the ids are
already what llama-swap expects.
(`proxytool models` prints only `[routing.*]` backends, so it stays empty in
single-upstream mode; it's for multi-backend setups.)

**3. Terminal 3 — pi** (no copy needed; point pi at a throwaway agent dir):

```bash
mkdir -p /tmp/pi-demo && cp examples/pi/models.json /tmp/pi-demo/models.json
export PI_CODING_AGENT_DIR=/tmp/pi-demo

pi --provider proxytool --model qwen-gguf  -p "List the files in this directory."
pi --provider proxytool --model qwen-mlx   -p "List the files in this directory."
pi --provider proxytool --model phi-4-mini -p "List the files in this directory."
```

This keeps your existing `~/.pi/agent/models.json` untouched; merge the
provider in if you'd rather have it always available.

Inside a session, `/model` switches without restarting — each switch just
changes the `model` field, so llama-swap evicts one backend and loads the
next while proxytool waits out the cold start.

**4. Or opencode:**

```bash
export OPENCODE_CONFIG=./examples/opencode-swap.jsonc
opencode run -m proxytool/qwen-gguf  "List the files in this directory."
opencode run -m proxytool/qwen-mlx   "List the files in this directory."
opencode run -m proxytool/phi-4-mini "List the files in this directory."
```

Interactively, `/models` switches model in-session.

**What you should see:** in the llama-swap terminal a swap per model change
(unload, then load — the group keeps only one resident); in each client a
normal `tool` call instead of leaked JSON. After the first swap `globalTTL: 120`
idles the backend out, so the next request pays a cold load again
(`timeout = 600` in the proxytool config covers that).

**If a swap fails:** run the model's `cmd` by hand from the llama-swap config
with `--port 9999` and fix that first — llama-swap only reports a failed
health check. `proxytool health` then tells you whether the gateway or the
proxy is the broken hop. If llama-swap can't find `llama` or
`mlx_lm.server`, use their absolute paths in `cmd` (GUI/launchd environments
often have a shorter `PATH` than your shell).

## Stack recipes

**llama-swap as upstream** (several models, llama-swap spawns/stops them):
start llama-swap with the example above, then proxytool in front — the
llama-swap keys are already the client ids, so no model map is needed:

```bash
proxytool serve --upstream-url http://127.0.0.1:8080 --upstream-timeout 600
```

**A single `llama serve`, no gateway**: no config file for the backend is
needed — it takes flags. Either point proxytool at one already running:

```bash
proxytool serve --upstream-url http://127.0.0.1:8181
```

or run the server yourself in this same terminal before proxytool (the
relevant llama.cpp flags live upstream, not in proxytool):

```bash
llama serve --host 127.0.0.1 --port 8181 --jinja \
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
