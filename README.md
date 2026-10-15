<p align="center">
  <img src="public/assets/images/proxytoolpatch.webp" alt="proxytoolpatch" width="220">
</p>

# proxytoolpatch workspace

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
│  Agent clients:  opencode · pi · curl · any OpenAI-compatible      │
└──────────────────────────┬───────────────────────────────────────┘
                           │ HTTP (OpenAI dialect)
┌──────────────────────────▼───────────────────────────────────────┐
│  proxytool  (apps/proxytool) — the runnable app                    │
│  FastAPI + `proxytool` CLI: bind, supervise, route, sanitize       │
│  requests, repair leaked tool calls (streaming + non-streaming),   │
│  guard against repetition loops — all repair logic via the         │
│  library below                                                     │
└──────────────────────────┬───────────────────────────────────────┘
                           │ in-process import
┌──────────────────────────▼───────────────────────────────────────┐
│  toolpatch  (packages/toolpatch) — the repair library              │
│  Pure, zero-I/O Python: leak detection (Hermes / fenced / bare     │
│  JSON), JSON healing, SSE stream healing, history normalization    │
└──────────────────────────┬───────────────────────────────────────┘
                           │ HTTP (OpenAI dialect)
┌──────────────────────────▼───────────────────────────────────────┐
│  Backend servers: llama-server (GGUF) · mlx_lm.server · omlx ·     │
│  llama-swap · any OpenAI-compatible local inference server         │
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

```bash
uv run proxytool serve     # default: 127.0.0.1:8180 → http://127.0.0.1:8181
```

The `proxytool` command is a console script registered in the app package
(`[project.scripts]` in `apps/proxytool/pyproject.toml`), so any install of the
app — workspace `uv sync`, `pip install proxytool`, or
`uv tool install proxytool` — puts it on your PATH. Full CLI reference:
[`apps/proxytool/README.md`](apps/proxytool/README.md).

## Run it for real (llama / mlx_lm / llama-swap + opencode or pi)

The exact stack many people run: a GGUF or MLX model (or several, behind
llama-swap) served locally, and `opencode` or `pi` as the coding agent.
Three choices, oldest-first complexity:

**1. One model, one command** — proxytool starts *and* supervises the backend
for you, then shuts it down cleanly when you Ctrl-C:

```bash
proxytool serve --backend gguf --model "$MPATH/qwen2.5-coder-14b-instruct-q4_k_m.gguf"
proxytool serve --backend mlx --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit
# clients point at http://127.0.0.1:8180/v1
```

**2. llama-swap picks the model on demand** — llama-swap already spins
backends up/down from its own YAML; put proxytool in front of it:

```bash
proxytool serve --upstream-url http://127.0.0.1:8080 \
  --model-map '{"qwen-gguf":"<real llama-swap model id>","qwen-mlx":"<real id>"}'
# opencode config / pi models.json still point at http://127.0.0.1:8180/v1
```

**3. Run it in the background** (daemon) instead of a foreground terminal:

```bash
proxytool start --backend gguf --model "$MPATH/qwen…gguf"  # returns once /health answers
proxytool status && proxytool health && proxytool models
proxytool stop                                             # logs: /tmp/proxytool-serve-state.log
```

Prefer zero flags on the day-to-day? A config file makes `proxytool start`
bare: `cp examples/proxytool/proxytool.toml ~/.config/proxytool/config.toml`,
then `proxytool start` / `proxytool stop` just work. Precedence is
**flag > env > config file > default** — full schema and discovery order in
[`apps/proxytool/README.md`](apps/proxytool/README.md#config-file). The file
is strictly validated at load (`proxytool config print` shows exactly what
resolved, and where every value came from).

Client wiring (opencode `examples/opencode-*.jsonc`, pi `examples/pi/models.json`) and the
detailed recipes, env-var table and flags: [`apps/proxytool/README.md`](apps/proxytool/README.md).
What the two pieces actually repair when a model leaks: [`packages/toolpatch/README.md`](packages/toolpatch/README.md).

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
  the same nox sessions CI runs; a local `scripts/test-matrix.sh` equivalent
  exists in untracked local tooling
- Git hooks: optional, see `docs/DEVELOPMENT.md` — pre-push runs lint + tests
- CI mirrors all of it on every PR (`.github/workflows/test.yml`); publishing
  is tag-driven and test-gated (`.github/workflows/publish.yml`)
- PR/branch-protection steps + daemon CLI (start/stop/status/health/models):
  [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)

## Design history

Full design rationale and history lives in `PLAN.md`; release notes in
`RELEASE.md`.
