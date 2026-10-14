#!/usr/bin/env bash
# llama_serve.sh — start a real GGUF or MLX backend on 127.0.0.1:8181,
# plus the middleware on 127.0.0.1:8180.
#
# Usage:
#   ./scripts/llama_serve.sh gguf [$MODEL_PATH]
#   ./scripts/llama_serve.sh mlx  [$MODEL_PATH_OR_HF_ID]
#
# Requires `uv sync --all-packages` once (workspace .venv).
set -euo pipefail
cd "$(dirname "$0")/.."

KIND="${1:-gguf}"
PORT_UP="${UP_PORT:-8181}"
PORT_MW="${MW_PORT:-8180}"

case "$KIND" in
  gguf)
    if [[ $# -ge 2 ]]; then MODEL="$2"
    else
      SNAP="$(find "$HF_HUB_CACHE"/models--Qwen--Qwen2.5-Coder-14B-Instruct-GGUF/snapshots -mindepth 1 -maxdepth 1 -type d | head -n 1)"
      MODEL="$SNAP/qwen2.5-coder-14b-instruct-q4_k_m.gguf"
    fi
    CMD=(llama-server --host 127.0.0.1 --port "$PORT_UP"
         --model "$MODEL" --ctx-size 16384 --jinja)
    ;;
  mlx)
    if [[ $# -ge 2 ]]; then MODEL="$2"
    else MODEL="mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"; fi
    CMD=(python -m mlx_lm.server --model "$MODEL" --port "$PORT_UP")
    ;;
  *) echo "usage: $0 {gguf|mlx} [model-path]" >&2; exit 2 ;;
esac

echo "▶ backend ($KIND) on :$PORT_UP  — model: $MODEL"
"${CMD[@]}" &
UP_PID=$!
trap 'kill "$UP_PID" 2>/dev/null || true' EXIT

echo "▶ middleware on :$PORT_MW (trace on)"
LMW_TRACE=1 uv run uvicorn proxytool.main:app --host 127.0.0.1 --port "$PORT_MW"
