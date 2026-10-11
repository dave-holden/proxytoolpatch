#!/usr/bin/env bash
# pi-smoke.sh — smoke-test the middleware with the `pi` coding agent.
#
# Usage:
#   ./scripts/pi-smoke.sh                  # uses LMW_MIDDLEWARE_URL or 127.0.0.1:8180
#   MW=http://127.0.0.1:8180 ./scripts/pi-smoke.sh
#
# Assumes:
#   - the rig is running (./scripts/dev-rig.sh mlx|gguf|omlx <model>)
#   -/pi is installed (curl -fsSL https://pi.dev/install.sh | sh)
#   - ~/.pi/agent/models.json contains the llamamw provider
#     (copy config/pi-models.json, see README §pi).

set -euo pipefail
cd "$(dirname "$0")/.."

MW="${LMW_MIDDLEWARE_URL:-http://127.0.0.1:8180}"
PI_BIN="${LMW_PI_BIN:-pi}"

echo "[smoke] middleware: $MW"
echo "[smoke] pi bin:     $PI_BIN"

# 1. middleware health
curl -sf "$MW/health" > /dev/null || { echo "[smoke] FAIL: middleware not reachable at $MW" >&2; exit 1; }
echo "[smoke] middleware health: OK"

# 2. middleware sees the upstream (models listing engages the resolution path)
MODELS=$(curl -sf "$MW/v1/models" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("data",[])))')
echo "[smoke] /v1/models lists $MODELS upstream model(s)"

# 3. raw OpenAI round-trip (no client): tool call must come back structured
echo '[smoke] raw curl tool round-trip...'
curl -sf "$MW/v1/chat/completions" -H 'content-type: application/json' -d '{
  "model": "qwen-mlx",
  "stream": true,
  "messages": [{"role":"user","content":"List the files in the current directory using the list_dir tool."}],
  "tools": [{"type":"function","function":{
    "name":"list_dir","description":"List the files in a directory.",
    "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
  }}]
}' | python3 -c '
import sys, json
calls, content = [], ""
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("data: ") or line == "data: [DONE]":
        continue
    try: evt = json.loads(line[6:])
    except json.JSONDecodeError: continue
    delta = (evt.get("choices") or [{}])[0].get("delta") or {}
    content += delta.get("content") or ""
    calls += delta.get("tool_calls") or []
assert calls, f"no structured tool_calls in stream; content={content!r}"
name = calls[0]["function"]["name"]
assert not any(m in content for m in ("tool_call", "{\"name\":")), f"leak in content: {content!r}"
print(f"[smoke] structured tool call OK (function name: {name!r}, content clean)")
'

# 4. pi one-shot agent turn (isolated project dir so we never touch real files)
if command -v "$PI_BIN" > /dev/null; then
  TMPDIR=$(mktemp -d)
  trap 'rm -rf "$TMPDIR"' EXIT
  mkdir -p "$TMPDIR/proj"
  echo "empty sandbox project" > "$TMPDIR/proj/README.md"
  echo "[smoke] pi one-shot turn in $TMPDIR/proj ..."
  (cd "$TMPDIR/proj" && "$PI_BIN" \
      --provider llamamw --model qwen-mlx \
      -p 'Use the list_dir tool to show me the contents of the current directory. Do not read any files; just list.' \
      > "$TMPDIR/pi-out.txt" 2>&1) || {
    echo "[smoke] FAIL: pi exited nonzero" >&2; cat "$TMPDIR/pi-out.txt" >&2; exit 1;
  }
  if grep -qE 'tool_call|["\042]name["\042]:' "$TMPDIR/pi-out.txt"; then
    echo "[smoke] FAIL: pi output contains raw JSON leak" >&2
    cat "$TMPDIR/pi-out.txt" >&2; exit 1
  fi
  echo "[smoke] pi one-shot turn OK (no leaked markup)"
  tail -n 6 "$TMPDIR/pi-out.txt" || true
else
  echo "[smoke] SKIP: '$PI_BIN' not on PATH — install with: curl -fsSL https://pi.dev/install.sh | sh"
fi

echo "[smoke] ALL CHECKS PASSED"
