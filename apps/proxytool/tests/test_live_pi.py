"""E2E test through **pi** (pi.dev) — the second real agent client.

Opt-in via ``LMW_LIVE=1`` (also auto-skips when the ``pi`` CLI is not on PATH
or no live middleware is reachable). Requires a running rig:

    ./scripts/dev-rig.sh mlx mlx-community/Qwen2.5-Coder-14B-Instruct-4bit

    LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_pi.py -q

pi is configured via ``PI_MODELS_FILE`` (an isolated temp models.json) so the
test never touches ``~/.pi/agent/models.json``. It registers an
``openai-completions`` provider at the middleware and runs ``pi -p <prompt>``
(print mode, one-shot), then asserts:

- the run completed without the client choking on the repaired payload
- a structured tool call survived pi's stream parser (pi ran a tool)
- no raw hermes tags / leaked JSON in pi's rendered output
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

OPEN_TAG = bytes.fromhex("3c746f6f6c5f63616c6c3e").decode()

MIDDLEWARE = os.environ.get("LMW_MIDDLEWARE_URL", "http://127.0.0.1:8180")
PI_BIN = os.environ.get("LMW_PI_BIN", "pi")
MODEL = os.environ.get("LMW_LIVE_MODEL_PI", "qwen-mlx")

PROMPT = (
    "Use the list_dir tool to show me the contents of the current directory. "
    "Do not read any files; just list."
)


def _pi_available() -> bool:
    return shutil.which(PI_BIN) is not None


def _middleware_live() -> bool:
    try:
        return httpx.get(f"{MIDDLEWARE}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("LMW_LIVE") != "1"
    or not _pi_available()
    or not _middleware_live(),
    reason="LMW_LIVE=1 not set, pi CLI missing, or middleware not running",
)


def _make_pi_env(workdir: Path) -> tuple[dict[str, str], Path]:
    """Isolated pi config: custom provider -> middleware, nothing else."""
    cfg = {
        "providers": {
            "proxytool-mw-test": {
                "baseUrl": f"{MIDDLEWARE}/v1",
                "api": "openai-completions",
                "apiKey": "not-needed",
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [
                    {
                        "id": MODEL,
                        "name": "Qwen via llama-middleware (test)",
                        "input": ["text"],
                        "contextWindow": 16384,
                        "maxTokens": 8192,
                    }
                ],
            }
        }
    }
    cfg_path = workdir / "pi-models-test.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env["PI_MODELS_FILE"] = str(cfg_path)
    return env, cfg_path


def _run_pi(workdir: Path, env: dict[str, str], prompt: str) -> str:
    """Run `pi -p <prompt>` in an isolated dir; return its stdout."""
    projdir = workdir / "proj"
    projdir.mkdir(exist_ok=True)
    (projdir / "README.md").write_text("empty sandbox project\n", encoding="utf-8")

    proc = subprocess.run(
        [PI_BIN, "--provider", "proxytool-mw-test", "--model", MODEL,
         "-p", prompt],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(projdir),
    )
    if proc.returncode != 0:
        pytest.fail(
            f"pi run failed ({proc.returncode}):\nstdout:\n{proc.stdout[-2000:]}"
            f"\nstderr:\n{proc.stderr[-2000:]}"
        )
    return proc.stdout


def test_pi_agent_runs_a_tool():
    npc = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix=f"pi-mw-{npc}-") as tmp:
        workdir = Path(tmp)
        env, _ = _make_pi_env(workdir)
        out = _run_pi(workdir, env, PROMPT)

        blob = json.dumps(out)

        # 1. No leaked tool-call markup in client-visible output.
        assert OPEN_TAG not in blob, "hermes open tag leaked into pi output"
        assert '{"name"' not in blob, "raw leaked JSON next to tool output"

        # 2. pi actually did something beyond a plain text answer: its
        #    rendered output should reference the tool it invoked OR the
        #    visible listing. We accept either trace of tool use because pi's
        #    print-mode rendering differs by version.
        assert "list_dir" in blob or "README.md" in blob or "proj" in blob, (
            f"no evidence of tool use in pi output: {out[-600:]!r}"
        )


def test_pi_no_leak_plain_question():
    """A plain turn must render clean text without markup."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        env, _ = _make_pi_env(workdir)
        out = _run_pi(workdir, env, "Say exactly: RIGOK")
        assert OPEN_TAG not in out
        assert '{"name"' not in out
        assert "RIGOK" in out, out[-1000:]
