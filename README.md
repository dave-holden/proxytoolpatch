<p align="center">
  <img src="public/assets/images/proxytoolpatch.webp" alt="proxytoolpatch" width="220">
</p>

# proxytoolpatch workspace

[![test & checks](https://github.com/dave-holden/proxytoolpatch/actions/workflows/test.yml/badge.svg)](https://github.com/dave-holden/proxytoolpatch/actions/workflows/test.yml)

[![publish toolpatch + proxytool](https://github.com/dave-holden/proxytoolpatch/actions/workflows/publish.yml/badge.svg)](https://github.com/dave-holden/proxytoolpatch/actions/workflows/publish.yml)

[![Python 3.10–3.14](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue)](https://www.python.org)

[![code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[![PyPI proxytool](https://img.shields.io/badge/PyPI-toolpatch-blue)](https://pypi.org/project/proxytool/)
[![PyPI toolpatch](https://img.shields.io/badge/PyPI-toolpatch-blue)](https://pypi.org/project/toolpatch/)

**In plain terms:** local AI models sometimes *mumble* their tool commands —
your coding agent asks the model to list your files, and instead of a clean,
structured instruction the model splatters raw JSON into the chat. This
workspace fixes the mumble in-flight: you point your agent client at one
local address, and tool calls just work.

Technically: a workspace for **agent-ready local LLM serving**: run a small model with a
standard OpenAI-compatible server, put this in front of it, and agent clients
(opencode, pi, curl, anything speaking the OpenAI dialect) get clean
structured tool calls — even when the model leaks them as raw text.

```
┌──────────────────────────────────────────────────────────────────┐
│  Agent clients:  opencode · pi · curl · any OpenAI-compatible    │
└──────────────────────────┬───────────────────────────────────────┘
                           │ HTTP (OpenAI dialect)
┌──────────────────────────▼───────────────────────────────────────┐
│  proxytool  (apps/proxytool) — the runnable app                  │
│  FastAPI + `proxytool` CLI: bind, supervise, route, sanitize     │
│  requests, repair leaked tool calls (streaming + non-streaming), │
│  guard against repetition loops — all repair logic via the       │
│  library below                                                   │
└──────────────────────────┬───────────────────────────────────────┘
                           │ in-process import
┌──────────────────────────▼───────────────────────────────────────┐
│  toolpatch  (packages/toolpatch) — the repair library            │
│  Pure, zero-I/O Python: leak detection (Hermes / fenced / bare   │
│  JSON), JSON healing, SSE stream healing, history normalization  │
└──────────────────────────┬───────────────────────────────────────┘
                           │ HTTP (OpenAI dialect)
┌──────────────────────────▼───────────────────────────────────────┐
│  Backend servers: llama-server (GGUF) · mlx_lm.server · omlx ·   │
│  llama-swap · any OpenAI-compatible local inference server       │
└──────────────────────────────────────────────────────────────────┘
```

## Modules

| Module | Kind | What it provides | Docs |
|---|---|---|---|
| `apps/proxytool` | app | The proxy server + `proxytool` CLI: serve, supervise backends, route by model id, repair leaked tool calls in-flight | [`apps/proxytool/README.md`](apps/proxytool/README.md) |
| `packages/toolpatch` | library | Pure-logic repair engine: `extract_tool_calls`, `heal_json`, `StreamHealer`, `normalize_chat_request` — embeddable by any Python server | [`packages/toolpatch/README.md`](packages/toolpatch/README.md) |
| `examples/` | runnable examples | Copy-paste config files: proxytool TOML, opencode + pi client wiring, llama-swap upstream | [`examples/README.md`](examples/README.md) |
| `scripts/` | local tooling | Personal dev scripts (dev rig, hooks, test matrix) — intentionally untracked, not needed to run the modules | — |

The naming: `toolpatch` is the published library name; both components work for any small local model, not just
Qwen.

## Install

Workspace (development):

```bash
uv sync --all-packages     # one shared root .venv, editable across modules
```

Once published, `pip install proxytool` will be enough for the app — it
declares the library as a dependency, so installing the app pulls the
library too. The library alone is `pip install toolpatch` (published as `toolpatch`; previously
a rename candidate).

## Usage

The `proxytool` command is a console script registered in the app package
(`[project.scripts]` in `apps/proxytool/pyproject.toml`), so any install of
the app — workspace `uv sync`, `pip install proxytool`, or
`uv tool install proxytool` — puts it on your PATH. Full CLI reference:
[`apps/proxytool/README.md`](apps/proxytool/README.md).

**What it is, one line:** `proxytool` is a FastAPI middleware + CLI that sits
in front of standard OpenAI-compatible local model servers, rewrites handler
aliases, repairs garbled tool calls in-flight via the `toolpatch` library,
and aborts asking-loops via LoopGuard.

**Scope, stated clearly:** proxytool only looks after itself. It fronts
model servers that are *already running*; owning their process lifecycle is
deliberately out of scope — that's the job of llama-swap (multi-model start /
stop from YAML), or your compose/systemd unit.

### Startup order

For the common setups the pieces come up in *this* order:

1. **LLM server(s)** (llama.cpp, mlx_lm, oMLX) — must be listening first
2. **llama-swap** — if you want on-demand multi-model swaps, start it after
   its backends
3. **proxytool** — last server piece: it health-checks its upstream at
   startup and holds traffic until the backend answers
4. **Agent client** (opencode, pi, …) — only needs proxytool's endpoint URL

### 1 · Start the LLM server yourself

```bash
# llama.cpp with the qwen tool-call template
llama-server --host 127.0.0.1 --port 8181 --jinja \
  --model "$MPATH/qwen2.5-coder-14b-instruct-q4_k_m.gguf" &

# or mlx_lm.server
python -m mlx_lm.server --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit --port 8181

# optional: tell the repair layer which server dialect it is talking to
export LMW_BACKEND=gguf   # gguf | mlx | omlx | fake  (repair hint only)
```

### 2 · Start llama-swap (scenario B: llama-swap owns the backends)

llama-swap decides which model to spin up from its own YAML — proxytool just
stands in front of it and keeps the repair/aliasing per request:

```bash
llama-swap --config llama-swap/config.yml           # listens on :8080
proxytool serve --upstream-url http://127.0.0.1:8080 \
  --model-map '{"qwen-gguf":"<real llama-swap model id>"}'
```

Equivalent env: `LTP_UPSTREAM_URL=http://127.0.0.1:8080`,
`LTP_MODEL_MAP='{…}'`. See `examples/llama-swap/config.yml` for a working
upstream file.

Then start proxytool itself — foreground, or daemonized with its own
lifecycle commands (this self-management is what proxytool *does* own):

```bash
proxytool serve --upstream-url http://127.0.0.1:8080      # foreground

# or the daemon form: returns once its /health answers
proxytool start --upstream-url http://127.0.0.1:8080
proxytool status && proxytool health && proxytool models
uv run proxytool status && uv run proxytool health && uv run proxytool models
proxytool stop
```

### 3 · Start the agent client

```bash
# opencode — point it at proxytool via one env var
export OPENCODE_CONFIG=examples/opencode-gguf.jsonc   # or opencode-mlx.jsonc
opencode

# pi — copy the provider file once, then run
mkdir -p ~/.pi/agent && cp examples/pi/models.json ~/.pi/agent/models.json
pi --provider proxytool --model qwen-mlx -p "list the files in ."

# smoke-test without any agent
LMW_TRACE=1 uv run uvicorn proxytool.main:app --port 8180   # dev server
```

The configs point their baseURL at `http://127.0.0.1:8180/v1` (proxytool).
Aliases are rewritten server-side; clients stay oblivious.

### 4 · One middle layer, several backends (scenario C)

When not using llama-swap but more than one model server, let proxytool
route by model id and pick the fallback:

```bash
proxytool serve \
  --upstream gguf=http://127.0.0.1:8181 --upstream-models gguf=qwen-gguf \
  --upstream mlx=http://127.0.0.1:8281 --upstream-models mlx=qwen-mlx \
  --default-backend mlx
```

Full env-var table, precedence rules, config schema and diagnostics:
[`apps/proxytool/README.md`](apps/proxytool/README.md). What gets repaired
when a model leaks: [`packages/toolpatch/README.md`](packages/toolpatch/README.md).

## Tests & checks

```bash
uv run pytest          # unit + regression + proxy integration
uv run ruff check .
uv run pyright
uv run nox             # lint + full multi-Python matrix (3.10–3.14, both modules)
```

Nox (`noxfile.py` at the root) drives the same per-version test sessions CI
uses — every slice installs the workspace packages as wheels under the target
interpreter in a throwaway uv env — and CI runs those nox sessions directly,
so `noxfile.py` is the single source of truth for what “checked” means.
`uv run nox` locally is exactly what `test-matrix` in CI tests.

Live model / real-client tests are opt-in (`LMW_LIVE=1`) and auto-skip —
see [`apps/proxytool/README.md`](apps/proxytool/README.md#tests--checks).

Matrix / hooks / CI / PR setup:

- Multi-version matrix: `uv run nox -s test` (Python 3.10–3.14, both modules) —
  the same nox sessions CI runs (a local test-matrix helper may exist in your
  untracked tooling; the repo itself needs only nox)
- Git hooks: optional, see `docs/DEVELOPMENT.md` — pre-push runs lint + tests
- CI mirrors all of it on every PR (`.github/workflows/test.yml`); publishing
  is tag-driven and test-gated (`.github/workflows/publish.yml`)
- PR/branch-protection steps + daemon CLI (start/stop/status/health/models):
  [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)

## Design history

Design rationale and release notes are maintained as local-only files,
deliberately outside the repo, so this README stays authoritative for a
fresh clone. What the repo itself ships is the git tag history — pushing
a version tag publishes both wheels through
`.github/workflows/publish.yml` (test-gated).
