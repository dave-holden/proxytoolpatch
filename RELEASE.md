# RELEASE.md

## v0.1.0 — ebbdbb7 (2026-10-09)
- uv workspace: virtual root with shared ruff/pyright/pytest configs; packages/llama-qwen (pure logic) + apps/proxy (FastAPI).
- Core engine: OpenAI-compatible schemas, JSON heal engine (trailing commas, raw newlines, smart quotes, code fences).
- Tool-call extractor: Hermes tags, fenced ```json, bare JSON-in-prose, with tool-name filtering.
- StreamHealer: bounded-buffer SSE repair emitting OpenAI-shaped tool-call deltas.
- Request normalizer: backend-aware (`gguf`/`mlx`) tool_choice handling; input never mutated.
- Proxy: repair-in-transit for non-streaming and streaming; passthrough byte regression guards.
- Regression harness: golden fixture pairs (capture real Qwen leaks → add files, never re-baseline).
- 29 llama-qwen unit/regression tests + 8 proxy integration tests; ruff + pyright clean.
