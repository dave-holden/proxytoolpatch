<p align="center">
  <img src="public/assets/images/proxytool.webp" alt="proxytool" width="200">
</p>

# proxytool (the app)

[![test & checks](https://github.com/dave-holden/proxytoolpatch/actions/workflows/test.yml/badge.svg)](https://github.com/dave-holden/proxytoolpatch/actions/workflows/test.yml)
[![Python 3.10–3.14](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue)](https://www.python.org)
[![code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**In plain terms:** you run one thing — `proxytool serve` — and it takes care
of the rest: it can start your local model server(s) for you (llama-server,
mlx_lm.server), watches them, routes each request to the right model, and fixes
garbled tool calls before your agent client ever sees them. You point
`opencode`, `pi`, or any OpenAI-compatible client at `http://127.0.0.1:8180/v1`
and stop thinking about it.

Technically: **proxytool** (formerly `llama-middleware` / `proxy` / `toolproxy`) — a FastAPI proxy that
sits between an agent client (opencode, pi, curl, any OpenAI-compatible tool)
and local inference backends — `llama-server` (GGUF) or `mlx_lm.server` /
`omlx` (MLX), or any OpenAI-compatible server on the same
`POST /v1/chat/completions` interface — and repairs the tool calls local
models leak as raw text.

When a model's chat template is missing or mismatched, it emits its tool call
as **text inside** `choices[*].message.content` instead of the structured
`tool_calls` field — so clients print raw JSON in the TUI instead of
executing the tool. proxytool repairs it in-flight:

- Request side: validates/sanitizes `tools` + `tool_choice` (MLX drops
  `tool_choice`), without mutating unknown fields.

- Response side: extracts leaked tool calls (Hermes-style XML function tags,
  fenced ```json blocks, bare `{"name":...}` JSON in prose), heals broken
  JSON (trailing commas, raw newlines, smart quotes, fences) and rebuilds a
  clean OpenAI `tool_calls` payload — including during streaming, with
  bounded buffering that preserves time-to-first-token.

- Passthrough-first: clean requests/responses come out byte-equivalent.

- History sanitizer: merges/dedupes assistant-message shapes llama-server's
  chat template rejects (leaked-JSON twins of repaired tool calls, adjacent
  assistant turns), so multi-turn agent sessions don't hit upstream `400`s.

- Stream termination guarantee: the repaired SSE stream always emits a
  `finish_reason` chunk before `[DONE]`, so agent clients close the turn
  instead of re-prompting.

- Frame-safe SSE ingestion: upstream HTTP chunks are reassembled into whole
  SSE lines before decoding (`_SSEParser`), so a `data:` payload or a
  multibyte UTF-8 character split across two chunks is neither dropped nor
  corrupted into U+FFFD — covered by split-frame regression tests.

- LoopGuard: detects consecutive byte-identical requests (same sanitized
  messages + tools), nudges the model, and aborts after a bounded number of
  repeats.

**Status:** v1.0.0 — GGUF (llama-server) and MLX (mlx_lm.server) both
round-trip verified end-to-end in live agent sessions. Ships a `proxytool`
CLI with managed-process supervision and multi-backend routing.

> This app imports all repair logic from the sibling library
> [`packages/toolpatch`](../../packages/toolpatch/README.md). Workspace
> overview and install: root [`README.md`](../../README.md).

## The rig

```
┌────────────┐ 8180 → ┌───────────────────┐ 8181 upstream → ┌────────────────┐
│ opencode / │        │ FastAPI proxy     │                 │ Qwen (GGUF/MLX)│
│ curl       │───────►│ sanitize + repair │────────────────►│ or fake        │
└────────────┘◄───────┤ (this app)        │◄────────────────└────────────────┘
```

## The `proxytool` CLI

A thin launcher around the FastAPI app (`proxytool/cli.py`; entry point
registered in `apps/proxytool/pyproject.toml` as a `project.scripts` console
script, so installing the package puts `proxytool` on your PATH). It does
*not* replace Uvicorn: it builds a uvicorn server in-process after
optionally starting and health-checking the backend processes it fronts.

### Install & run

```bash
uv sync --all-packages     # workspace install; provides `proxytool`
uv run proxytool serve     # default: 127.0.0.1:8180 → http://127.0.0.1:8181
uv run proxytool version
```

Once published: `pip install proxytool` and just `proxytool serve`.

**How the command gets on your PATH.** Both install paths register a shell
command via the packaging metadata — no manual copying required:

```toml
# apps/proxytool/pyproject.toml
[project.scripts]
proxytool = "proxytool.cli:main"
```

- `uv sync --all-packages` creates a `proxytool` executable inside the
  workspace `.venv` (`uv run proxytool …` runs it without activating the
  venv).
- `pip install proxytool` writes a `proxytool` entry-point script into the
  Python environment's `bin/` (or `Scripts/` on Windows) — `~/.venv/bin` on
  a venv, or `uv tool install proxytool` to get it on the user PATH without
  a project.

Eight subcommands:

| Subcommand | Purpose |
|---|---|
| `serve` | run the middleware in the foreground (+ managed backends) |
| `start` | daemonize `serve` (same flags; detached; waits for /health) |
| `stop` | gracefully stop a `start`ed server (SIGTERM → grace → SIGKILL) |
| `status` | running / stopped / stale, from the state file |
| `health` | JSON probe: middleware `/health` + each registered backend |
| `models` | JSON: each upstream's `/v1/models` ids (what routing matches) |
| `config` | diagnose the config file: `config print` (discovery + validation + resolved settings vs. env), `config check` (validate only) |
| `version` | print the version |

`proxytool serve --help` lists all flags; `proxytool --help` never starts a
server. Lifecycle state lives in `$LTP_STATE_PATH` or
`/tmp/proxytool-serve-state.json` — see `docs/DEVELOPMENT.md`.

### Flags (every flag has an env-var equivalent — flags win)

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--host` | `LTP_HOST` | `127.0.0.1` | bind host |
| `--port` | `LTP_PORT` | `8180` | bind port |
| `--upstream NAME=URL` (repeatable) | `LTP_UPSTREAM_<NAME>` | — | register a routed backend |
| `--upstream-models NAME=m1,m2` (repeatable) | `LTP_UPSTREAM_<NAME>_MODELS` | — | model ids routed to that backend |
| `--default-backend NAME` | `LTP_DEFAULT_BACKEND` | first registered | backend used when no model list matches |
| `--upstream-url URL` | `LTP_UPSTREAM_URL` / `LMW_UPSTREAM_URL` | `http://127.0.0.1:8181` | standalone single-backend URL |
| `--managed 'NAME=http://host:port::CMD'` (repeatable) | `LTP_PROCESS_<NAME>_HEALTH_PATH` / `_STARTUP_TIMEOUT` / `_SHUTDOWN_TIMEOUT` | — | child process to supervise |
| `--backend gguf\|mlx\|omlx\|fake` | `LTP_BACKEND` / `LMW_BACKEND` | inferred | request-sanitizer mode + managed-process template. Optional when `--model` pins it down: `*.gguf` → gguf, `*omlx*`/`*mlx*` (incl. `mlx-community/…`) → mlx/omlx; anything else needs it explicitly |
| `--model PATH_OR_HF_ID` | — | — | model artifact for the managed template |
| `--no-managed` | — | false | never spawn children (external upstream only) |
| `--model-map JSON` / `--model-map-file PATH` | `LMW_MODEL_MAP` / `LMW_MODEL_MAP_FILE` | — | client alias → real model id |
| `--upstream-api-key TOK` | `LMW_UPSTREAM_API_KEY` | — | static bearer token upstream |
| `--upstream-timeout S` | `LMW_UPSTREAM_TIMEOUT` | `300` | upstream read timeout (covers cold starts) |
| `--loop-nudge N` / `--loop-abort N` | `LMW_LOOP_NUDGE` / `LMW_LOOP_ABORT` | `3` / `6` | LoopGuard thresholds (0 = off) |
| `--trace` | `LMW_TRACE` / `LTP_TRACE` | off | dump payload transit points to stderr |
| `--log-level debug\|info\|warning\|error` | `LTP_LOG_LEVEL` / `LMW_LOG_LEVEL` | `info` | app log tier (proxytool + toolpatch debug lines; Uvicorn's own access logs follow the same value via `serve`) |
| `--quiet` | — | off | only log errors (≡ `--log-level error`) |
| `--config PATH` | `LTP_CONFIG` | discovery | TOML config file — see [Config file](#config-file) below |

`serve` and `start` share every flag above; `start` additionally daemonizes
(daemon log lands next to the state file, `/tmp/proxytool-serve-state.log` by
default). `status`/`stop`/`health`/`models` take `--host`/`--port` and
`--config` (config `host`/`port` and `[routing.*]` feed their probes too).
Legacy `LMW_*` env spellings keep working everywhere;
`LTP_*` wins when both are set.

### Upstream selection — three ways

```bash
# A. Standalone single backend (most common):
proxytool serve --upstream-url http://127.0.0.1:8181

# B. Managed child: proxytool starts llama-server, health-checks it, and
#    shuts it down cleanly when you Ctrl-C. shell-quoted model paths OK.
#    `--backend` is inferred from the artifact (*.gguf → gguf, mlx → mlx):
proxytool serve --model "$MPATH/qwen2.5-coder-14b-instruct-q4_k_m.gguf"
proxytool serve --backend mlx --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit   # explicit is always allowed

# C. Explicit managed spec (full control; repeatable):
proxytool serve --managed 'gguf=http://127.0.0.1:8181::llama-server --host 127.0.0.1 --port 8181 --ctx-size 16384 --jinja --model "$MPATH/qwen.gguf"'
```

### Multi-backend routing

Register more than one backend and route incoming requests by the client's
`model` id, with fallback to a default:

```bash
proxytool serve \
  --upstream gguf=http://127.0.0.1:8181  --upstream-models gguf=qwen-gguf,qwen \
  --upstream mlx=http://127.0.0.1:8281   --upstream-models mlx=qwen-mlx \
  --default-backend mlx
```

Exactly one registered backend → it receives everything. Several → chat
requests are routed **per request** by the client's `model` id: model-list
match first, then `--default-backend` (route decisions are logged as
`MW.ROUTE` when tracing). `/health`, `/v1/models`, and catch-all passthrough
still hit the primary URL (`--upstream-url` / first backend). The same wiring
via env:

```bash
LTP_UPSTREAM_GGUF=http://127.0.0.1:8181 \
LTP_UPSTREAM_GGUF_MODELS=qwen-gguf,qwen \
LTP_UPSTREAM_MLX=http://127.0.0.1:8281 \
LTP_DEFAULT_BACKEND=mlx \
proxytool serve
```

All legacy `LMW_*` env vars keep working; `LTP_*` takes precedence when both
are set.

### Managed-process details

Each `--managed` child is supervised: started before Uvicorn binds,
poll-probed at `<base-url>/health` (override with
`LTP_PROCESS_<NAME>_HEALTH_PATH`, e.g. `/v1/models` for mlx_lm.server),
bound to a startup timeout (`LTP_PROCESS_<NAME>_STARTUP_TIMEOUT`, default
120 s), and SIGTERM'd (then SIGKILL'd after a grace window) when the CLI
exits. If the port is already occupied, proxytool assumes an existing
instance and reuses it instead of failing.

### Test routing yourself

```bash
# two mock upstreams + per-model routing, assert which one answered:
uv run pytest apps/proxytool/tests/test_routing_integration.py -q
```

## Quickstart (manual)

```bash
uv sync --all-packages          # one shared root .venv (workspace members editable)

# Terminal 1: fake upstream (or your real llama-server / mlx server on 8181)
uv run uvicorn fake_upstream:app --host 127.0.0.1 --port 8181

# Terminal 2: the middleware
LMW_TRACE=1 uv run uvicorn proxytool.main:app --host 127.0.0.1 --port 8180

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

`--jinja` matters — it makes llama-server use the chat template with tool
support, which keeps most Hermes-tag leaks local to the server. What it does
*not* cover (fenced/bare-JSON leaks from other models, malformed JSON,
history-shape `400`s) is exactly what this proxy adds — see
[`packages/toolpatch/README.md`](../../packages/toolpatch/README.md).

## End-to-end: the real stack (llama / mlx_lm + llama-swap + opencode or pi)

A complete worked setup — two models behind llama-swap, proxytool in front,
`opencode`/`pi` as the agent.

**1. Declare the models in llama-swap's config** (it starts each backend on
demand for the first request, unloads it after `ttl` seconds idle):

```yaml
# ~/.llama-swap/config.yml
health-check: true
models:
  qwen-gguf:
    cmd: llama-server --host 127.0.0.1 --port ${PORT} --ctx-size 16384 --jinja --model /models/qwen2.5-coder-14b.gguf
    ttl: 300
  qwen-mlx:
    cmd: python -m mlx_lm.server --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit --port ${PORT}
    ttl: 300
```

**2. Start the gateway, then the middleware:**

```bash
llama-swap --config ~/.llama-swap/config.yml      # listens on :8080
proxytool serve --upstream-url http://127.0.0.1:8080 \
  --upstream-timeout 600                          # covers cold model loads
# or daemonized instead of a foreground terminal:
# proxytool start --upstream-url http://127.0.0.1:8080 --upstream-timeout 600
```

**3. Point the clients at the middleware's `/v1` — never at llama-swap
directly:**

```bash
export OPENCODE_CONFIG=./examples/opencode-gguf.jsonc   # baseURL http://127.0.0.1:8180/v1
opencode                                              # interactive agent
pi --provider proxytool --model qwen-mlx -p "list the files in ."   # one-shot turn
proxytool health && proxytool models                  # sanity check
```

**4. Without llama-swap**, steps 1–2 collapse into one managed command —
proxytool starts, health-checks and supervises the backend itself (`--jinja
and ctx-size already included in the built-in gguf template):

```bash
proxytool serve --model "$MPATH/qwen2.5-coder-14b-instruct-q4_k_m.gguf"   # backend inferred (.gguf)
proxytool serve --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit      # backend inferred (mlx)
```

Steps 3 onwards are identical in both variants.

## opencode configs

Two ready-made client configs (point opencode at the middleware's `/v1` so
the repair path is engaged — see the `/v1` suffix note inside each file):

| Config | Model id | Backend |
|---|---|---|
| `examples/opencode-gguf.jsonc` | `proxytool/qwen-gguf` | llama-server (GGUF) |
| `examples/opencode-mlx.jsonc` | `proxytool/qwen-mlx` | mlx_lm.server (MLX) |

```bash
export OPENCODE_CONFIG=./examples/opencode-gguf.jsonc   # or opencode-mlx.jsonc
opencode
```

## pi (pi.dev) as a client

[pi](https://pi.dev/) is a second supported agent client (alongside
opencode). Both point at the same middleware `/v1` endpoint — same repair
path, same LoopGuard.

1. Install: `curl -fsSL https://pi.dev/install.sh | sh` (Node 22.19+)
2. Copy `examples/pi/models.json` into `~/.pi/agent/models.json`
3. Run:

```bash
pi --provider proxytool --model qwen-mlx       # interactive
pi --provider proxytool --model qwen-mlx -p "..."   # one-shot print mode
pi --list-models proxytool                      # verify the provider is visible
```

Flags worth knowing: `/model` inside a session switches models without a
restart (models.json is re-read every time). The `compat` flags in
`examples/pi/models.json` (`supportsDeveloperRole`, `supportsReasoningEffort`)
keep pi's request dialect compatible with local inference servers.

## llama-swap / gateway upstreams

The middleware fronts llama-swap (or any OpenAI-compatible gateway) with:

```bash
proxytool serve --upstream-url http://127.0.0.1:8080
```

(model ids/aliases: llama-swap routes by the `model` field of each request;
see the routing bullets below for alias-to-id mapping.)

- **Model routing** — llama-swap spins the backend from its own YAML config
  based on the `model` field our proxy sends. Client aliases (`qwen-gguf`,
  `qwen-mlx`) are resolved first via `LMW_MODEL_MAP`, then by probing
  upstream `/v1/models`; when several models are loaded, alias token overlap
  picks the closest match, preferring the artifact the server was launched
  with (a local snapshot path beats a same-name HF repo id).
- **Timeouts / no buffering** — `LMW_UPSTREAM_TIMEOUT` (default `300`s)
  covers llama-swap's on-demand model loads; SSE responses stream through
  unbuffered (the healer emits as chunks arrive, preserving TTFT) — the
  only accumulation is frame reassembly of chunk-boundary-split SSE lines
  and content buffers while a leak might be forming.
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
| `LTP_LOG_LEVEL` / `LMW_LOG_LEVEL` | `info` | app log tier: `debug` shows repair/routing decisions from the app **and** the toolpatch library; `--quiet` ≡ `error` |
| `LTP_CONFIG` | — | explicit TOML config-file path (skips cwd/XDG discovery) |

(`LTP_*` equivalents exist for every var — see the flags table above; they
take precedence when both are set.)

## Config file

Drop a TOML file and run bare commands — the llama-swap experience:

```bash
# one-time
cp examples/proxytool/proxytool.toml ~/.config/proxytool/config.toml   # or ./proxytool.toml

# day to day — no flags, no env:
proxytool start          # or: proxytool serve
proxytool health && proxytool models
proxytool status && proxytool stop
```

Discovery (first hit wins): `--config PATH` → `$LTP_CONFIG` →
`./proxytool.toml` → `./.proxytool.toml` →
`$XDG_CONFIG_HOME/proxytool/config.toml` → `~/.config/proxytool/config.toml`.
Sections: `host`/`port`/`log_level`/`default_backend` (top-level), `[upstream]`
(single backend URL), `[routing.<name>]` (multi-backend table; `default = true`
on exactly one entry, or top-level `default_backend`), `[managed.<name>]`
(child process to supervise), `[model_map]`, `[loopguard]`, `[trace]`.
See the annotated `examples/proxytool/proxytool.toml` for the full schema.

Precedence per key — **CLI flag > env var > config file > built-in default**.
The bridge only fills env vars (`LTP_*`) that neither a flag nor the
environment expressed, so flags/env stay authoritative for overrides and
scripting; a `[managed.*]` section is ignored when `--managed`/`--backend` is
passed. Parse errors fail fast: `proxytool: config error: <file>: <detail>`.

Run it from another directory with an explicit file:

```bash
proxytool start --config /etc/proxytool/config.toml
```

#### Strict validation

The file is validated at load time — unknown keys/sections and wrong types
fail fast with pointed messages instead of becoming runtime symptoms:

```
proxytool: config error: proxytool.toml:
  - unknown key 'prot' (top level) — did you mean 'port'?
  - unknown key 'timeot' in [upstream] — did you mean 'timeout'?
  - [upstream.port] Input should be a valid integer
  - default = true on more than one routing entry: gguf, mlx
```

Parsed errors (invalid TOML syntax) fail the same way. `routing.<name>`
entries need a `url`; `[managed.*]` entries need a `command` plus a
`base_url` or `port`; `[upstream].url` must be a real `http(s)://` URL;
`backend` must be one of `gguf | mlx | omlx | fake`.

#### Diagnose without starting anything

When a setting doesn't land the way you expected, ask the CLI what it would
actually resolve:

```bash
proxytool config print    # discovery + validation + every effective setting
proxytool config check    # discovery + validation only (CI/script-friendly)
proxytool config print --json

# example print output
$ proxytool config print
config file: ./proxytool.toml
  searched ./proxytool.toml   <- found
validation: ok
precedence: CLI flag > env var > config file > built-in default

effective settings (what serve/start would run with):
  LTP_PORT           8180    <- config file (port)
  LTP_BACKEND        mlx     <- env (pre-existing)
  ...
```

A row marked `env (pre-existing)` means the environment already set that key,
so the config file did not (and will not) override it; `config file (key)`
shows which TOML key fed it. CLI flags still beat both — this subcommand
takes no flags, so it shows the env-vs-file layer.

LoopGuard: consecutive byte-identical requests (same sanitized messages +
tools) are detected; after N repeats a nudge is appended telling the model to
stop, after M the middleware stops forwarding and returns a clean completion
explaining the cut-off ("LoopGuard: request aborted after N identical
attempts"). A different request resets the count.

## Tests & checks

```bash
uv run pytest          # unit + regression + proxy integration (fixtures in packages/toolpatch/tests/regression)
uv run ruff check .
uv run pyright
uv run nox -s coverage # branch coverage over both modules (report printed)
```

New real-world misformattings captured from your model should be added as
regression fixture pairs — see
`packages/toolpatch/tests/regression/fixtures/README.md`.

### Live model test (opt-in)

Runs real requests through the middleware against a **live** Qwen backend,
asserts nothing leaks (no tag artifacts, no raw JSON next to repaired
tool_calls, stream and non-stream), and prints a capture block ready to paste
into the regression-fixture dir if the model produces a novel misformatting:

```bash
# with the middleware serving a real model (see the quickstart above):
LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_qwen.py -q
```

Env: `LMW_LIVE=1` to enable, `LMW_MIDDLEWARE_URL` (default
`http://127.0.0.1:8180`) and `LMW_LIVE_MODEL` (override the `model` string
sent in requests). Skipped (2 tests) unless it is set and the upstream health
check passes, so `pytest` stays green without it.

### Real client test (opencode, opt-in)

Beyond curl: runs the **actual `opencode` CLI** against the middleware — an
isolated temp config (via `OPENCODE_CONFIG`, so your `~/.config/opencode` is
never touched) defines a custom `@ai-sdk/openai-compatible` provider pointed
at `http://127.0.0.1:8180/v1`, then `opencode run --format json` executes in
a throwaway sandbox dir and we assert that a real **structured tool call**
survived opencode's stream parser (`tool_use` events completed) with zero
leakage in the rendered output:

```bash
# rig on 8181 + middleware on 8180, then:
LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_opencode.py -q
```

Also auto-skips when `opencode` is not on PATH. Extra env:
`LMW_OPENCODE_BIN` (defaults to `opencode`).

### Real client test (pi, opt-in)

Same idea as the opencode client test but for **pi**: an isolated temp
`PI_MODELS_FILE` (never touching `~/.pi/agent/models.json`) registers an
`openai-completions` provider pointed at `http://127.0.0.1:8180/v1`, then
`pi --provider proxytool --model qwen-mlx -p "..."` runs one-shot in a
throwaway sandbox dir. We assert the run completes, a structured tool call
survived pi's stream parser, and zero tag-artifact or raw-JSON leakage in the
rendered output:

```bash
# rig on 8181 + middleware on 8180, then:
LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_pi.py -q
```

Also auto-skips when `pi` is not on PATH. Extra env: `LMW_PI_BIN` (defaults
to `pi`), `LMW_LIVE_MODEL_PI` (defaults to `qwen-mlx`).

```bash
# End-to-end one-shots with a real model behind the middleware on 8180:
LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_qwen.py -q         # qwen suite
LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_opencode.py -q   # + opencode
LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_pi.py -q         # + pi
```

PyCharm Run/Debug (watch payloads transit, breakpoints, fake-upstream
config): see **PLAN.md §6** in the repository root.
