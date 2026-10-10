#!/usr/bin/env bash
# dev-rig.sh — one-command rig: backend on 8181 + middleware on 8180.
# Runs until Ctrl-C; middlewares/upstreams started here keep running while it does.
#
# Usage:
#   ./scripts/dev-rig.sh                    # fake upstream (no model download)
#   ./scripts/dev-rig.sh gguf  [model.gguf] # llama-server backend
#   ./scripts/dev-rig.sh mlx  [model]       # mlx_lm.server backend
#   ./scripts/dev-rig.sh omlx [model]       # omlx serve backend (Apple Silicon)
#
# With 'uv-test' as the LAST arg, also runs the live E2E test against the rig
# once both servers are up (LMW_LIVE=1); requires a real model.
#
# Env overrides:
#   MW_PORT=8180 UP_PORT=8181 LMW_TRACE=1 ./scripts/dev-rig.sh mlx

set -euo pipefail
cd "$(dirname "$0")/.."   # workspace root (llama-middleware/)

MW_PORT="${MW_PORT:-8180}"
UP_PORT="${UP_PORT:-8181}"

if [[ "${1:-}" == "uv-test" ]]; then
  RUN_UV_TEST=1; shift
else
  RUN_UV_TEST=0
fi
BACKEND="${1:-fake}"
shift || true
MODEL_ARG="${1:-}"

LMW_TRACE="${LMW_TRACE:-1}"

BACKEND="${LMW_BACKEND:-$BACKEND}"   # LMW_BACKEND=mlx implies mlx

PIDS=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "[rig] shutting down..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

start_upstream() {
  case "$BACKEND" in
    fake)
      echo "[rig] upstream: fake leaking upstream on :$UP_PORT"
      uv run uvicorn scripts.fake_upstream:app --host 127.0.0.1 --port "$UP_PORT" &
      ;;
    gguf)
      : "${MODEL_ARG:?usage: dev-rig.sh gguf <model.gguf>}$"
      echo "[rig] upstream: llama-server (GGUF) on :$UP_PORT"
      llama serve --host localhost --port "$UP_PORT" --ctx-size 16384 --jinja --model "$MODEL_ARG" &
      ;;
    mlx)
      : "${MODEL_ARG:?usage: dev-rig.sh mlx <model>}$"
      echo "[rig] upstream: mlx_lm.server on :$UP_PORT"
      python -m mlx_lm.server --model "$MODEL_ARG" --port "$UP_PORT" &
      ;;
    omlx)
      : "${MODEL_ARG:?usage: dev-rig.sh omlx <model>}$"
      echo "[rig] upstream: omlx serve on :$UP_PORT"
      omlx serve "$MODEL_ARG" --port "$UP_PORT" &
      ;;
    *)
      echo "[rig] unknown backend: $BACKEND (fake|gguf|mlx|omlx)" >&2
      exit 2
      ;;
  esac
  PIDS+=("$!")
}

wait_for() {
  local url="$1" name="$2" tries=60
  for _ in $(seq 1 "$tries"); do
    if curl -sf -o /dev/null "$url"; then
      echo "[rig] $name is up ($url)"
      return 0
    fi
    sleep 0.5
  done
  echo "[rig] $name failed to come up at $url" >&2
  return 1
}

start_upstream
wait_for "http://127.0.0.1:$UP_PORT/health" "upstream"

# LMW_BACKEND must match the real backend (mlx strips tool_choice on request side)
export LMW_BACKEND="$BACKEND"
export LMW_TRACE
echo "[rig] middleware: proxy.main:app on :$MW_PORT (LMW_TRACE=$LMW_TRACE) — Ctrl-C stops all"
uv run uvicorn proxy.main:app --host 127.0.0.1 --port "$MW_PORT" &
PIDS+=("$!")
wait_for "http://127.0.0.1:$MW_PORT/health" "middleware"

echo "[rig] ready: http://127.0.0.1:$MW_PORT/v1  (point opencode baseURL here)"
echo "[rig] smoke:  curl -s http://127.0.0.1:$MW_PORT/v1/chat/completions -H 'Content-Type: application/json' -d '{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"list the files\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"list_dir\",\"parameters\":{\"type\":\"object\",\"properties\":{\"path\":{}}}}}]}'"

if [[ "$RUN_UV_TEST" == "1" ]]; then
  echo "[rig] running live E2E test (LMW_LIVE=1)..."
  LMW_LIVE=1 uv run pytest apps/proxy/tests/test_live_qwen.py -q || {
    echo "[rig] live test FAILED" >&2
    exit 1
  }
  echo "[rig] live test PASSED"
fi

wait
