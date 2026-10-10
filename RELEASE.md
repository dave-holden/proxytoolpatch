# RELEASE.md

## v0.2.0 — 0098a69 (2026-10-10)
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

## v0.1.0 — ebbdbb7 (2026-10-09)
- uv workspace: virtual root with shared ruff/pyright/pytest configs; packages/llama-qwen (pure logic) + apps/proxy (FastAPI).
- Core engine: OpenAI-compatible schemas, JSON heal engine (trailing commas, raw newlines, smart quotes, code fences).
- Tool-call extractor: Hermes tags, fenced ```json, bare JSON-in-prose, with tool-name filtering.
- StreamHealer: bounded-buffer SSE repair emitting OpenAI-shaped tool-call deltas.
- Request normalizer: backend-aware (`gguf`/`mlx`) tool_choice handling; input never mutated.
- Proxy: repair-in-transit for non-streaming and streaming; passthrough byte regression guards.
- Regression harness: golden fixture pairs (capture real Qwen leaks → add files, never re-baseline).
- 29 llama-qwen unit/regression tests + 8 proxy integration tests; ruff + pyright clean.
