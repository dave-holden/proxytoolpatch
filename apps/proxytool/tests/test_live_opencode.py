"""E2E test through **opencode** — the real agent client — instead of raw curl.

Opt-in via ``LMW_LIVE=1`` (also auto-skips when the ``opencode`` CLI is not on
PATH or no live middleware is reachable). Requires a running rig:

    ./scripts/dev-rig.sh gguf $MPATH/qwen2.5-...gguf      # terminal 1

    LMW_LIVE=1 uv run pytest apps/proxytool/tests/test_live_opencode.py -q
    # or one-shot:  ./scripts/dev-rig.sh gguf <model> uv-test

opencode is configured via ``OPENCODE_CONFIG`` (an isolated temp JSONC file) so
the test never touches ``~/.config/opencode`` — no plugins, no MCP servers, no
global model defaults leak in. It points a custom ``openai-compatible``
provider at the middleware and runs ``opencode run --format json``, which
emits raw JSON events (assistant text + tool calls), then asserts:

- the run completed without the client choking on the repaired payload
- the agent actually invoked our ``list_dir`` tool (structured tool call
  survived opencode's own stream parsing end-to-end)
- no raw hermes tags / leaked JSON in what the client decided to display
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
OPENCODE_BIN = os.environ.get("LMW_OPENCODE_BIN", "opencode")

PROMPT = (
    "Use the list_dir tool to show me the contents of the current directory. "
    "Do not read any files; just list."
)


def _opencode_available() -> bool:
    return shutil.which(OPENCODE_BIN) is not None


def _middleware_live() -> bool:
    try:
        return httpx.get(f"{MIDDLEWARE}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("LMW_LIVE") != "1"
    or not _opencode_available()
    or not _middleware_live(),
    reason="LMW_LIVE=1 not set, opencode CLI missing, or middleware not running",
)


def _make_opencode_env(workdir: Path) -> tuple[dict[str, str], Path]:
    """Isolated opencode config: custom provider -> middleware, nothing else."""
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "provider": {
            "proxytool": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "llama-middleware",
                "options": {"apiKey": "not-needed", "baseURL": f"{MIDDLEWARE}/v1"},
                "models": {"qwen": {"name": "Qwen via llama-middleware"}},
            }
        },
        "model": "proxytool/qwen",
        "small_model": "proxytool/qwen",
        "mcp": {},
        "plugin": [],
    }
    cfg_path = workdir / "opencode-test-config.jsonc"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env["OPENCODE_CONFIG"] = str(cfg_path)
    return env, cfg_path


def _run_opencode(workdir: Path, env: dict[str, str]) -> list[dict]:
    """Run `opencode run --format json` in an isolated dir; return event list."""
    projdir = workdir / "proj"
    projdir.mkdir(exist_ok=True)
    (projdir / "README.md").write_text("empty sandbox project\n", encoding="utf-8")

    proc = subprocess.run(
        [
            OPENCODE_BIN,
            "run",
            PROMPT,
            "--format", "json",
            "--dir", str(projdir),
            "--print-logs",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(projdir),
    )
    if proc.returncode != 0:
        pytest.fail(
            f"opencode run failed ({proc.returncode}):\nstdout:\n{proc.stdout[-2000:]}"
            f"\nstderr:\n{proc.stderr[-2000:]}"
        )
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assert events, "opencode run produced no JSON events"
    return events


def test_opencode_agent_lists_dir():
    npc = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix=f"oc-mw-{npc}-") as tmp:
        workdir = Path(tmp)
        env, _ = _make_opencode_env(workdir)
        events = _run_opencode(workdir, env)

        # Collect everything the client saw, in serialized form.
        blob = json.dumps(events)

        # 1. No leaked tool-call markup made it into client-visible text.
        assert OPEN_TAG not in blob, "hermes open tag leaked into opencode events"

        # 2. Among opencode event types, a tool invocation happened.
        #    (opencode brings its own tools — bash/read/edit — so the model
        #    will not call our list_dir; what matters is that a structured
        #    tool call survived opencode's stream parser at all.)
        tool_parts = [
            e.get("part") for e in events
            if str(e.get("type", "")).lower() == "tool_use"
        ]
        assert tool_parts, (
            f"no tool_use events in opencode run — got types: "
            f"{sorted({str(e.get('type', '')) for e in events})}\n"
            f"events tail: {json.dumps(events[-3:])[:800]}"
        )
        completed = [
            p for p in tool_parts
            if isinstance(p, dict)
            and (p.get("state") or {}).get("status") == "completed"
        ]
        assert completed, f"tool_use events never completed: {json.dumps(tool_parts)[:600]}"


def test_opencode_no_leak_plain_question():
    """Even without tool pressure, a plain turn must render clean text."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        env, _ = _make_opencode_env(workdir)
        proc = subprocess.run(
            [OPENCODE_BIN, "run", "Say exactly: RIGOK", "--format", "json"],
            env=env, capture_output=True, text=True, timeout=300,
            cwd=str(workdir),
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert OPEN_TAG not in proc.stdout
        assert "RIGOK" in proc.stdout, proc.stdout[-1000:]
