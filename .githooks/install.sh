#!/usr/bin/env bash
#
# install.sh — wire this repo's git hooks to .githooks (tracked, shared).
# Uses core.hooksPath so the hooks live in-repo; no hook content is
# generated here, git just needs to be pointed at the directory once per
# clone. Re-run any time (idempotent).
set -euo pipefail
cd "$(dirname "$0")/.."

chmod +x .githooks/pre-push .githooks/pre-commit .githooks/pre-push-gate.sh 2>/dev/null || true
git config core.hooksPath .githooks

echo "hooks installed: core.hooksPath = .githooks"
echo "  pre-push   → .githooks/pre-push-gate.sh pre-push"
echo "  pre-commit → .githooks/pre-push-gate.sh pre-commit (reduced gate)"
echo ""
echo "full matrix per push: FULL_MATRIX=1 git push ..."
