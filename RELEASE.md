# RELEASE.md

> Rename note (2026-10-12): workspace renamed to `proxytoolpatch`; app
> `toolproxy` -> `proxytool` (dir `apps/proxytool`, module `proxytool`);
> library `llama-qwen` -> `toolpatch` (dir `packages/toolpatch`, module
> `toolpatch`). Entries below use the names current at the time.

## v1.0.0 (2026-10-14)
- **First major release.** Public API is stable as shipped: `toolpatch`'s five
  callables (`extract_tool_calls`, `heal_json`, `StreamHealer`,
  `normalize_chat_request`, schemas) and `proxytool`'s eight CLI subcommands
  (`serve`/`start`/`stop`/`status`/`health`/`models`/`config`/`version`).
- Rename completed: library `llama-qwen` → `toolpatch`, app `toolproxy` →
  `proxytool`, workspace root `proxytoolpatch`. Both member packages move to
  `1.0.0` together (rename + publication-ready stable API = the major bump).
- Supported Python widened to the full floor **3.10–3.14** (`requires-python
  =">=3.10"` everywhere; ruff target `py310`; pyright gated on the 3.10
  floor). Matrix proven green on all five versions via the isolated
  wheel-install harness (`scripts/test-matrix.sh` and its Python twin
  `scripts/test_matrix.py`).
- CLI lifecycle hardening: `start` daemonizes `serve`, waits for `/health`,
  records PID; `stop` SIGTERM→grace→SIGKILL; `status`/`health`/`models` round
  out the daemon story. `python -m proxytool` delegates to the CLI instead of
  unconditionally booting a server.
- Publish pipeline test-gated (`.github/workflows/publish.yml`: rc-tags →
  TestPyPI, clean tags → PyPI, post-upload clean-venv smoke import). Both
  `proxytool` (app) and `toolpatch` (library) wheels are publish targets now;
  the app's `[project.scripts]` entry-point puts `proxytool` on PATH at
  install time.
- TOML config file (`config/proxytool-example.toml` →
  `~/.config/proxytool/config.toml`): llama-swap-style `[upstream]` /
  `[routing.*]` / `[managed.*]` / `[model_map]` / `[loopguard]` / `[trace]`,
  XDG-aware discovery (`--config` > `$LTP_CONFIG` > `./proxytool.toml` >
  XDG), strict validation at load (unknown keys with "did you mean" hints,
  bad types/ranges, all errors aggregated, exit 2), and precedence
  **flag > env > file > default** — `proxytool start` runs with zero flags.
  New `proxytool config print|check [--json]` diagnoses discovery,
  validation and every effective setting's source without starting a
  server. `--backend` is now optional when `--model` pins it down
  (`*.gguf` → gguf, `*omlx*`/`*mlx*` → omlx/mlx).
- CI (`.github/workflows/test.yml`) runs the `noxfile.py` sessions directly —
  single source of truth for the 3.10–3.14 matrix.
- Checks at tag time: 157 passed / 6 skipped (opt-in live suites); ruff
  clean; pyright 0 errors on the 3.10 floor; full nox matrix green.

## v0.2.1 (2026-10-10)
- MLX parity tests: `backend="mlx"` produces an identical sanitized history shape to gguf (parametrized parity test); `tool_choice` strip has its own note assertion.
- `PUBLISHING.md`: concise publish guide (pre-publish checklist, bump rules, TestPyPI-first flow, name-availability check, install-and-smoke-test of the published wheel); `llama-qwen` confirmed **free** on PyPI; wheel build verified locally.
- RELEASE.md headings now carry version + date only (no commit hash).
- Package versions `0.2.0` → `0.2.1`. Checks: 50 passed / 4 skipped; ruff clean; pyright 0 errors.

## v0.2.0 (2026-10-10)
- History sanitizer (`_sanitize_history` in request normalizer): drops leaked-JSON text twins of repaired tool_calls turns (semantic argument match), merges adjacent assistant messages, dedupes duplicate adjacent answers — fixes llama-server `400 "Cannot have 2 or more assistant messages at the end of the list"` reported live by opencode.
- Proxy SSE path now always terminates the OpenAI stream: preserves upstream `id`/`created`/`model`, forwards `usage`, and emits a `finish_reason` chunk before `[DONE]`, using the upstream's own or a synthetic `stop` — fixes client re-prompt loop (opencode re-issued identical prompts forever when the terminating chunk was dropped during repair reshaping).
- Three new streaming tests: termination survives repair, meta/usage forwarded, unterminated upstream gets synthetic stop.
- Regression fixture `test_opencode_history_400.py` replays the exact 7-message history captured from the live 400 trace.
- 6 new `_sanitize_history` unit tests (twin-drop, merge variants, dedupe, clean-history passthrough).
- Dev rig scripts: `scripts/dev-rig.sh` (upstream + middleware + optional live pytest one-shot), `scripts/llama_serve.sh`; `proxy.__main__` entry.
- Live opt-in suites: `test_live_qwen.py` (real backend through middleware, asserts no leakage, stream + non-stream), `test_live_opencode.py` (real opencode CLI with isolated config, asserts structured tool_call survives opencode parse).
- Trace file support: `LMW_TRACE_FILE` writes payload transit log to file.
- Configs: `config/opencode-gguf.jsonc` (model id `qwen-gguf`) and `config/opencode-mlx.jsonc` (model id `qwen-mlx`).
- Versions: both member packages `0.1.0` → `0.2.0`. Checks: 47 passed / 4 skipped; ruff clean; pyright 0 errors.

## v0.1.0 (2026-10-09)
- uv workspace: virtual root with shared ruff/pyright/pytest configs; packages/llama-qwen (pure logic) + apps/proxy (FastAPI).
- Core engine: OpenAI-compatible schemas, JSON heal engine (trailing commas, raw newlines, smart quotes, code fences).
- Tool-call extractor: Hermes tags, fenced ```json, bare JSON-in-prose, with tool-name filtering.
- StreamHealer: bounded-buffer SSE repair emitting OpenAI-shaped tool-call deltas.
- Request normalizer: backend-aware (`gguf`/`mlx`) tool_choice handling; input never mutated.
- Proxy: repair-in-transit for non-streaming and streaming; passthrough byte regression guards.
- Regression harness: golden fixture pairs (capture real Qwen leaks → add files, never re-baseline).
- 29 llama-qwen unit/regression tests + 8 proxy integration tests; ruff + pyright clean.
