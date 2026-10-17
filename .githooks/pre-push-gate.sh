#!/usr/bin/env bash
#
# pre-push-gate.sh — quality gate run before a commit or push.
# Wired into git via `core.hooksPath .githooks` (see .githooks/install.sh).
#
# Runs: ruff lint → quick default-python pytest. Tools that can't run
# (missing interpreter / no network for uv) degrade to a warning, never
# silently pass — the script reports what it could NOT run.
set -uo pipefail
cd "$(dirname "$0")/.."

STAGE="$1" # pre-push | pre-commit
fails=0
skipped=()

step() { # step <label> <cmd...>
  local label="$1"; shift
  echo "▶ $label"
  if "$@"; then
    echo "  ✅ $label"
  else
    local rc=$?
    echo "  ❌ $label (exit $rc)" >&2
    fails=$((fails+1))
  fi
}

echo "═══════════════════════════════════════════════"
echo " pre-push gate ($(date '+%H:%M:%S')) — stage: $STAGE"
echo "═══════════════════════════════════════════════"

if command -v uv >/dev/null 2>&1; then
  step "ruff lint"              uv run ruff check .
  step "pytest (default python)" uv run pytest -q
  if [[ "$STAGE" == "pre-push" && "${FULL_MATRIX:-0}" == "1" ]]; then
    if [[ -x scripts/test-matrix.sh ]]; then # local-only (untracked) full matrix
      step "full python matrix" ./scripts/test-matrix.sh
    else
      echo "  ⚠ FULL_MATRIX=1 but scripts/test-matrix.sh not found locally — skipped" >&2
    fi
  fi
else
  skipped+=("uv not found — lint/tests NOT run")
fi

if ((${#skipped[@]})); then
  echo "⚠  SKIPPED (not run): ${skipped[*]}" >&2
fi

if ((fails > 0)); then
  echo "═══════════════════════════════════════════════"
  echo " GATE FAILED — $fails check(s) red. $STAGE blocked." >&2
  echo "═══════════════════════════════════════════════"
  exit 1
fi
echo "═══════════════════════════════════════════════"
echo " gate passed — proceeding with $STAGE"
echo "═══════════════════════════════════════════════"
