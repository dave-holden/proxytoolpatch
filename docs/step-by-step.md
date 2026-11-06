# Local LLM stack — step by step (mlx-serve · llama-swap · opencode · pi · proxytool)

Everything marked **✅** was verified live on Apple Silicon (macOS, 2026).
Untested paths are marked **⚠️ untested-expected** with the reason rather than
pretended to work. Memory figures are indicative: they depend on the model
size and on how much RAM the machine has free at load time.

**Three different tools, three different jobs — never confuse them:**

| Tool | What it is | Serves | Tool-calling |
|---|---|---|---|
| `mlx-serve` | Homebrew binary, one process, many models | MLX dirs **and** GGUF files from one `--model-dir` (on-demand load, ≥1 resident) | Native ✅ (`tool_calls`, `finish_reason: tool_calls`) |
| `mlx-lm` (`mlx_lm.server`) | Python server, **one** model per process | a single local dir or HF repo id | ❌ none — emits tool JSON as **plain text**; rejected `tool_choice`; re-resolves the client's `model` id against huggingface.co |
| `llama-swap` | Go meta-router, spawns per-id backends (`llama-server`, `mlx_lm.server`) on demand | whatever its config declares | ✅ for GGUF (via llama.cpp) — same raw-dialect repair needed |

Clients: **opencode** and **pi** are agent harnesses; they add tool schemas
(read/bash/edit) to every prompt, so the *server's* tool-call dialect matters.
**proxytool** sits between them and normalizes everything (see step 5).

---

## Step 0 — prerequisites (one-time)

**Model policy: exactly one download path.** Models live in
`~/.cache/huggingface/hub` (via `hf download`) and are manually symlinked into
`~/.mlx-serve/models`. No app-private stores, no `mlx-serve pull`.

```sh
# 1) download the same model in both formats — comparisons are only fair
#    when the weights match
hf download Qwen/Qwen2.5-Coder-14B-Instruct-GGUF
hf download mlx-community/Qwen2.5-Coder-14B-Instruct-4bit

# 2) symlink snapshot dirs into the serve dir — symlink NAME = model id
ln -s "$(ls -d ~/.cache/huggingface/hub/models--Qwen--Qwen2.5-Coder-14B-Instruct-GGUF/snapshots/*)" \
      ~/.mlx-serve/models/qwen2.5-coder-14b-gguf
ln -s "$(ls -d ~/.cache/huggingface/hub/models--mlx-community--Qwen2.5-Coder-14B-Instruct-4bit/snapshots/*)" \
      ~/.mlx-serve/models/qwen2.5-coder-14b-4bit
```

**Run servers from your own terminal**, never from inside an agent/sandbox
session — those get reaped when the session is cleaned up (verified repeatedly).

Port map (stable):

| Port | Service |
|---|---|
| 8081 | mlx-serve (primary) |
| 8080 | llama-swap (fallback) |
| 8180 | proxytool (in front of 8081 by default) |

---

## Step 1 — one server, one client

### 1.1 Start mlx-serve ✅

```sh
mlx-serve serve --model-dir ~/.mlx-serve/models --host 127.0.0.1 --port 8081
```

Headless — nothing loads until the first request. Watch the boot line
`[mem] MLX buffer-pool cap …`; a small number means free RAM was low at boot
(see Troubleshooting).

### 1.2 Smoke test (any ten seconds) ✅

```sh
curl -s http://127.0.0.1:8081/v1/models | python3 -m json.tool | head -20
```

### 1.3 Tool-calling probe — keep during every step ✅

```sh
curl -s http://127.0.0.1:8081/v1/chat/completions -H 'content-type: application/json' -d '{
  "model":"qwen2.5-coder-14b-gguf","max_tokens":128,
  "messages":[{"role":"user","content":"What is the weather in Paris? Use the get_weather tool."}],
  "tools":[{"type":"function","function":{"name":"get_weather",
    "description":"Get current weather for a city",
    "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'
```

✅ Expect `"finish_reason":"tool_calls"` and
`{"name":"get_weather","arguments":"{\"city\":\"Paris\"}"}`.

### 1.4 First client — opencode (one-off, config files come in step 4) ✅

Assuming `~/.config/opencode/opencode.jsonc` defines a provider (call it
`local-mlx`) pointing at the stack — the full config file comes in step 4 —
a one-off run looks like this:

```sh
opencode run "List the files in this directory" --model local-mlx/qwen2.5-coder-14b-gguf
```

### 1.5 First client — pi (one-off) ⚠️ not re-run after the 14B swap

```sh
pi --provider llamacpp --model "qwen2.5-coder-14b-gguf" -p "List the files in this directory"
```

`pi --provider <name>` talks to whatever `baseUrl` that provider declares in
`~/.config/pi/agent/models.json` — in the finished setup that is proxytool on
`:8180`, so for a direct-to-server run point the provider (or a second
provider entry) at `http://127.0.0.1:8081/v1` first.

Both clients add their own tools (read/bash/edit) to the prompt; with this
model the same server handles them.

---

## Step 2 — many models, one server (mlx-serve `--model-dir`)

Same single command as 1.1 — mlx-serve **auto-discovers** every symlink under
`~/.mlx-serve/models` and serves them all (✅ verified: every model in a
mixed GGUF/MLX store listed in `/v1/models`, loaded on demand):

- model ids = symlink names
- on-demand load per id; `max_resident_models=3` (LRU evicts… but see troubleshooting: no manual-unload endpoint)
- GGUF and MLX live side by side; engine is picked per model

Memory discipline (verified): **one 14B-class model at a time unless ≥ ~19 GB
is free** — a fresh-server MLX-14B load needs ~9.7 GB and passed with 13.7 GB
available; with the GGUF twin already resident it 503'd with only 2.5 GB. If
you must co-load, raise what the server thinks it can use:
`MLX_SERVE_GPU_CEILING_MB=16384` (with care).

---

## Step 3 — multiple servers: llama-swap as a second stack

`llama-swap` exists for when you want **llama.cpp-specific tuning** or a
process-per-model manager. Its config is `~/.config/llama-swap/config.yaml`
(`--config` flag; `${PORT}` is injected per spawned backend):

```sh
llama-swap --config ~/.config/llama-swap/config.yaml --listen 127.0.0.1:8080
```

What the config needs to say:

1. **GGUF backends** — spawn `llama-server`: ✅ verified (probe → 200, the
   model called the tool, but llama.cpp's `<tools>` raw-leak caveat → step 5)

   ```yaml
   models:
     qwen2.5-coder-14b-gguf:
       cmd: >
         llama-server --host 127.0.0.1 --port ${PORT}
         -m ${models}/qwen2.5-coder-14b-gguf/qwen2.5-coder-14b-instruct-q4_0.gguf
         -a qwen2.5-coder-14b-gguf -c 32768 -ngl 999 -fa on --jinja
   ```

2. **MLX backends** — spawn `mlx_lm.server`, **NOT** the `mlx_lm` CLI
   (verified: `mlx_lm --port …` exits prematurely → 500; `mlx_lm.server`
   boots ✅):

   ```yaml
   macros:
     mlx_backend: "mlx_lm.server --host 127.0.0.1 --port ${PORT} --model"
   ```

3. **MLX ids must be full HF repo ids** ✅ verified — mlx-lm re-resolves the
   client's `model` id against huggingface.co, so a friendly id
   (`qwen2.5-coder-14b-4bit`) 404s ("Repository Not Found"), and llama-swap
   itself only routes by the ids in its config. Register the entry under the
   full repo id:

   ```yaml
     mlx-community/Qwen2.5-Coder-14B-Instruct-4bit:
       checkEndpoint: /v1/models
       cmd: >
         ${mlx_backend} ${models}/qwen2.5-coder-14b-4bit
   ```

   ✅ verified: probe with `"model":"mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"`
   → 200. mlx-lm returned the tool call as plain text (no native contract) →
   only make this path workable behind proxytool (step 5) or accept raw text.

4. **TTL eviction**: `globalTTL: 120` unloads idle backends (✅ observed);
   requests that hit a model while *two* 14Bs are resident behave like step 2's
   memory rules.

---

## Step 4 — client config files

Point each client at the proxy and keep the model ids identical to the
`~/.mlx-serve/models` symlink names, so every layer can forward them verbatim:

- `~/.config/opencode/opencode.jsonc` — provider `local-mlx` →
  `http://127.0.0.1:8180/v1` (proxytool; bypass = swap baseURL to `:8081/v1`,
  single comment line). Default model + `small_model` + `agent.architect` +
  `agent.sysadmin` = `local-mlx/qwen2.5-coder-14b-gguf`, with `code-reviewer`
  and `test-writer` pointed at whatever other models the same store serves.
  MCP servers (memory, context, search) are client-side and don't need server
  support — a model server cannot host them.
- `~/.config/pi/settings.json` — `llamaSettings.servers`: `proxytool :8180/v1`
  (primary) and `mlx-serve :8081/v1` (bypass).
- `~/.config/pi/agent/models.json` — provider `llamacpp` → `:8180/v1` with the
  same model ids.

No other central config is required. Per-project overrides can go in each
project (`.opencode.json` / pi's local sessions) but weren't needed.

---

## Step 5 — proxytool in the middle

One config file, `~/.config/proxytool/config.toml` (a single upstream
`http://127.0.0.1:8081`, plus a 600 s timeout if your server loads models on
demand):

```sh
proxytool start;  proxytool status;  proxytool health;  proxytool models
```

Point the clients at `:8180` instead of the server. That is the whole wiring.
What it fixes (each **✅ verified**):

- **Raw-dialect repair**: llama.cpp GGUF with tool schemas leaks
  `<tools>…</tools>` / `<tool_call>` JSON as ordinary text with
  `finish_reason: stop` (✅ seen with both a direct llama-server and
  llama-swap). proxytool's `StreamHealer`/`extract_tool_calls` converts those
  back into structured `tool_calls` + `finish_reason: tool_calls` ✅.
- **mlx-lm dialect**: strips `tool_choice` (mlx-lm rejects it), and frees
  llama-swap+mlx-lm from the id war of step 3.3 by mapping ids —
  `PTP_MODEL_MAP='{"qwen2.5-coder-14b-4bit":"mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}'`
  (or `[model_map]` in the TOML) — ⚠️ the *forwarded* id must be one
  llama-swap has registered; with repo-id entries (step 3.3) it is.
- **LoopGuard**: nudges and optionally aborts identical-prompt repetition.
- **Friendly aliases** in both directions, so client configs never change
  when you change stacks.

**Multi-upstream** (one proxy fronts both stacks, both 14Bs share the
LLM-work but are *separate* servers):

```sh
proxytool serve \
  --upstream gguf=http://127.0.0.1:8080 --upstream-models gguf=qwen2.5-coder-14b-gguf \
  --upstream mlx=http://127.0.0.1:8081  --upstream-models mlx=qwen2.5-coder-14b-4bit \
  --default-backend gguf
```

The bare single-upstream form needs no config file at all:

```sh
proxytool serve --upstream-url http://127.0.0.1:8081   # bare single upstream
```

Bypass the proxy whenever you want client→server directly: pi (second entry in
`settings.json`), opencode (comment in `opencode.jsonc`).

---

## Step 6 — troubleshooting cheat sheet (all verified)

| Symptom | Cause | Fix |
|---|---|---|
| Server respawns nothing / dies when your agent session ends | process spawned inside a sandboxed session gets reaped | run in your own terminal; `launchd` for permanence |
| `[mem] MLX buffer-pool cap 2048 MB (was 31129 MB)` at boot | server clamps pool to free GPU memory at boot | free RAM, restart; or `MLX_SERVE_GPU_CEILING_MB=16384` (over-commit can crash) |
| 503 `Not enough free memory to load model: needs ~9.7 GB` | second 14B while one is resident (mlx-serve has **no unload endpoint**) | restart the server, or load one 14B at a time; `--skip-mem-preflight` only at own risk |
| `unsupported model_type '<arch>'` at boot | MLX architecture not implemented in the build (the quoted name is the family in the model's `config.json`) | use the model's GGUF on `llama.cpp`/`llama-server` instead |
| llama.cpp returns tool JSON as plain `<tools>` text | GGUF/chat-template dialect, no structured contract | proxytool in front ✅ |
| llama-swap MLX backend: 500 `upstream command exited prematurely` | macro called the `mlx_lm` CLI | use `mlx_lm.server` in the macro ✅ |
| `Repository Not Found … api/models/qwen2.5-coder-14b-4bit` | mlx-lm resolves the client id against HF | register the model under the full HF repo id ✅ (and/or proxytool `model_map`) |
| llama-swap: `no router for requested model` | request id isn't a config key | make request id match the (repo-id) key — via proxytool mapping |
| `mlx-serve list` shows fewer models than expected | dangling symlink after a hub-cache cleanup | guardrail loop in [compatibility.md](compatibility.md) §1 |
