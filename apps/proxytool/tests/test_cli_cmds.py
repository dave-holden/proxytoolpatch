"""Tests for the lifecycle/diagnostic CLI commands and their helpers.

Everything runs in-process against fakes: no daemon, no managed children.
State files are pointed at tmp_path via LTP_STATE_PATH; env-heavy paths use
monkeypatch so nothing leaks between tests.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import types
import urllib.request
from http.server import HTTPServer
from threading import Thread

import pytest

import proxytool.cli as cli


# ---------------------------------------------------------------- helpers
def _parse(argv: list[str]):
    return cli.build_parser().parse_args(argv)


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    """Point the CLI's state file + serve log at the test's tmp dir."""
    monkeypatch.setenv("LTP_STATE_PATH", str(tmp_path / "state.json"))
    return tmp_path


@pytest.fixture(autouse=True)
def clean_upstream_env(monkeypatch):
    """Upstream-routing env vars leak across tests; start each one clean."""
    for var in ("LTP_UPSTREAM_URL", "LMW_UPSTREAM_URL", "LTP_DEFAULT_BACKEND",
                "LMW_BACKEND", "LMW_UPSTREAM_API_KEY", "LMW_TRACE",
                "LMW_UPSTREAM_TIMEOUT", "LMW_MODEL_MAP", "LMW_MODEL_MAP_FILE"):
        monkeypatch.delenv(var, raising=False)


def _write_state(path, **fields):
    path.write_text(json.dumps(fields), encoding="utf-8")


class _FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class _FailingResp:
    """Context manager that raises like urllib does on refused connections."""

    def __enter__(self):
        raise ConnectionError("connection refused")

    def __exit__(self, *exc):
        return False


class _FakeChild:
    """Just enough of subprocess.Popen for cmd_start."""

    def __init__(self, exit_code=None):
        self.pid = 424242
        self._poll_result = None if exit_code is None else exit_code

    def poll(self):
        return self._poll_result

    def terminate(self):
        pass


# ---------------------------------------------------------------- config
def test_cmd_config_print_with_explicit_file(tmp_path, capsys):
    cfg = tmp_path / "proxytool.toml"
    cfg.write_text('host = "127.0.0.1"\nport = 8181\n', encoding="utf-8")
    args = _parse(["config", "print", "--config", str(cfg)])
    assert cli.cmd_config(args) == 0
    out = capsys.readouterr().out
    assert "config file:" in out
    assert "effective settings" in out
    assert "port" in out


def test_cmd_config_check_and_json(tmp_path, capsys):
    cfg = tmp_path / "proxytool.toml"
    cfg.write_text('host = "127.0.0.1"\n', encoding="utf-8")
    args = _parse(["config", "check", "--config", str(cfg)])
    assert cli.cmd_config(args) == 0
    assert "validation: ok" in capsys.readouterr().out

    args = _parse(["config", "--json", "--config", str(cfg)])
    assert cli.cmd_config(args) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["valid"] is True
    assert "file" in info


def test_cmd_config_invalid_file(tmp_path, capsys):
    cfg = tmp_path / "bad.toml"
    cfg.write_text("port = 'not-an-int'\n", encoding="utf-8")
    args = _parse(["config", "print", "--config", str(cfg)])
    assert cli.cmd_config(args) == 2
    assert "config error" in capsys.readouterr().err


# ---------------------------------------------------------------- version
def test_cmd_version(capsys):
    assert cli.cmd_version(types.SimpleNamespace()) == 0
    assert "proxytool" in capsys.readouterr().out


# ---------------------------------------------------------------- status
def test_cmd_status_stopped(state_env, capsys):
    assert cli.cmd_status(_parse(["status"])) == 3
    assert "STOPPED" in capsys.readouterr().out


def test_cmd_status_stale_clears_state(state_env, capsys):
    _write_state(state_env / "state.json", pid=999_999_999)
    assert cli.cmd_status(_parse(["status"])) == 1
    assert "STALE" in capsys.readouterr().out
    assert not (state_env / "state.json").exists()


def test_cmd_status_running(state_env, capsys, monkeypatch):
    _write_state(state_env / "state.json", pid=os.getpid())  # self = alive pid
    assert cli.cmd_status(_parse(["status"])) == 0
    assert "RUNNING" in capsys.readouterr().out


# ---------------------------------------------------------------- stop
def test_cmd_stop_not_running(state_env, capsys):
    assert cli.cmd_stop(_parse(["stop"])) == 3
    assert "not running" in capsys.readouterr().err


def test_cmd_stop_stale_pid(state_env, capsys):
    _write_state(state_env / "state.json", pid=999_999_999)
    assert cli.cmd_stop(_parse(["stop"])) == 0
    assert "already gone" in capsys.readouterr().out
    assert not (state_env / "state.json").exists()


def test_cmd_stop_graceful_real_child(state_env, capsys):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        _write_state(state_env / "state.json", pid=child.pid)
        assert cli.cmd_stop(_parse(["stop"])) == 0
        assert "stopped" in capsys.readouterr().out
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# ---------------------------------------------------------------- start
def test_cmd_start_already_running(state_env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    _write_state(state_env / "state.json", pid=1234)
    assert cli.cmd_start(_parse(["start"])) == 1
    assert "already running" in capsys.readouterr().out


def test_cmd_start_success(state_env, monkeypatch, capsys):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _FakeChild())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(200))
    assert cli.cmd_start(_parse(["start"])) == 0
    state = json.loads((state_env / "state.json").read_text())
    assert state["pid"] == 424242
    assert "running" in capsys.readouterr().out


def test_cmd_start_health_never_up(state_env, monkeypatch, capsys):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _FakeChild())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FailingResp())
    # Fake clock 0.6s/call + no-op sleep → the 30s deadline shapes out in
    # ~50 quick iterations instead of 30 realtime seconds.
    tick = iter(float(i) * 0.6 for i in range(1_000))
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(
        monotonic=lambda: next(tick, 1e9), sleep=lambda s: None))
    assert cli.cmd_start(_parse(["start", "--port", "8999"])) == 1
    assert "never came up" in capsys.readouterr().err


def test_cmd_start_died_during_startup(state_env, monkeypatch, capsys):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _FakeChild(exit_code=1))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FailingResp())
    assert cli.cmd_start(_parse(["start"])) == 1
    assert "died during startup" in capsys.readouterr().err


# ---------------------------------------------------------------- health
def test_cmd_health_ok_and_backend_fallback(state_env, monkeypatch, capsys):
    def fake_urlopen(url, timeout=3):
        if "/health" in url and "8191" in url:
            return _FailingResp()  # backend 2: no /health endpoint
        return _FakeResp(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LTP_UPSTREAM_B2", "http://127.0.0.1:8191")
    assert cli.cmd_health(_parse(["health"])) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["middleware"]["ok"] is True
    entry = report["backends"][0]
    assert entry["ok"] is True
    assert entry.get("via") == "/v1/models fallback"


def test_cmd_health_down(state_env, monkeypatch, capsys):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FailingResp())
    assert cli.cmd_health(_parse(["health"])) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["middleware"]["ok"] is False
    assert "error" in report["middleware"]


# ---------------------------------------------------------------- models
def test_cmd_models_lists_and_flags_error(state_env, monkeypatch):
    payload = json.dumps({"data": [{"id": "qwen-1"}, {"id": "qwen-2"}]}).encode()

    def fake_urlopen(url, timeout=5):
        if "8192" in url:
            return _FakeResp(200, payload)
        return _FailingResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LTP_UPSTREAM_GOOD", "http://127.0.0.1:8192/v1")
    monkeypatch.setenv("LTP_UPSTREAM_BAD", "http://127.0.0.1:8193/v1")
    assert cli.cmd_models(_parse(["models"])) == 1


def test_cmd_models_list_content(state_env, monkeypatch, capsys):
    payload = json.dumps({"data": [{"id": "qwen-1"}, {"id": "qwen-2"}]}).encode()

    def fake_urlopen(url, timeout=5):
        if "8192" in url:
            return _FakeResp(200, payload)
        return _FailingResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LTP_UPSTREAM_GOOD", "http://127.0.0.1:8192/v1")
    monkeypatch.setenv("LTP_UPSTREAM_BAD", "http://127.0.0.1:8193/v1")
    assert cli.cmd_models(_parse(["models"])) == 1
    by_name = {e["name"]: e for e in json.loads(capsys.readouterr().out)}
    assert by_name["good"]["models"] == ["qwen-1", "qwen-2"]
    assert "error" in by_name["bad"]


# ---------------------------------------------------------------- serve
def test_cmd_serve_runs_uvicorn_once(state_env, monkeypatch):
    # cmd_serve writes upstream-url env vars DIRECTLY (os.environ), which
    # monkeypatch cannot track — pre-set them so teardown restores.
    monkeypatch.setenv("LMW_UPSTREAM_URL", "")
    monkeypatch.setenv("LTP_UPSTREAM_URL", "")
    calls: list[tuple[tuple, dict]] = []

    def fake_uvicorn_run(*a, **kw):
        calls.append((a, kw))

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    args = _parse(["serve", "--port", "8971",
                   "--upstream-url", "http://127.0.0.1:8288"])
    assert cli.cmd_serve(args) == 0
    assert len(calls) == 1
    a, kw = calls[0]
    assert a[0] == "proxytool.main:app"
    assert kw["port"] == 8971
    assert os.environ["LMW_UPSTREAM_URL"] == "http://127.0.0.1:8288"


def test_flag_or_env(monkeypatch):
    args = types.SimpleNamespace(value=None)
    assert cli._flag_or_env(args, ["A", "B"], "value") == ""
    monkeypatch.setenv("A", "first")
    assert cli._flag_or_env(args, ["A", "B"], "value") == "first"
    args2 = types.SimpleNamespace(value="flag")
    assert cli._flag_or_env(args2, ["A", "B"], "value") == "flag"


def test_merge_env_into_process_env(monkeypatch):
    import proxytool.routing as routing

    args = types.SimpleNamespace(
        upstream_api_key="sk", upstream_timeout=7.5,
        model_map=None, model_map_file=None, trace=True,
        loop_nudge=3, loop_abort=9, upstream_url=None,
    )
    # Pre-set every key the helper writes directly: teardown restores them.
    for var in ("LMW_UPSTREAM_API_KEY", "LMW_UPSTREAM_TIMEOUT",
                "LMW_MODEL_MAP", "LMW_MODEL_MAP_FILE", "LMW_TRACE",
                "LMW_LOOP_NUDGE", "LMW_LOOP_ABORT", "LTP_DEFAULT_BACKEND"):
        monkeypatch.setenv(var, "")
    backends = {"b": routing.Backend(name="b", url="http://127.0.0.1:1",
                                     models=("m",), default=True)}
    monkeypatch.delenv("LMW_LOOP_NUDGE", raising=False)
    monkeypatch.delenv("LMW_LOOP_ABORT", raising=False)
    cli._merge_env_into_process_env(args, backends)
    assert os.environ["LMW_UPSTREAM_API_KEY"] == "sk"
    assert os.environ["LMW_TRACE"] == "1"
    assert os.environ["LMW_LOOP_NUDGE"] == "3"
    assert os.environ["LMW_LOOP_ABORT"] == "9"
    assert os.environ["LTP_DEFAULT_BACKEND"] == "b"


def test_pick_single_backend_url(monkeypatch):
    import proxytool.routing as routing

    monkeypatch.delenv("LTP_UPSTREAM_URL", raising=False)
    monkeypatch.delenv("LMW_UPSTREAM_URL", raising=False)
    args = types.SimpleNamespace(upstream_url=None)
    backends = {"b": routing.Backend(name="b", url="http://b:1", default=True)}
    assert cli._pick_single_backend_url(backends, args, "") == "http://b:1"
    assert cli._pick_single_backend_url({}, args, "http://env:9") == "http://env:9"
    args2 = types.SimpleNamespace(upstream_url="http://flag:2")
    assert cli._pick_single_backend_url({}, args2, "") == "http://flag:2"


# ---------------------------------------------------------------- __main__
def test_python_dash_m_proxytool_help_exits_zero():
    res = subprocess.run([sys.executable, "-m", "proxytool", "--help"],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0
    assert "serve" in res.stdout


def test_every_subcommand_help_has_a_description_paragraph():
    """Each subcommand's --help opens with a description paragraph.

    Argparse renders the subparser description between the usage block and
    the "options:" section; assert it is present and substantial for every
    subcommand, and that the top-level help matches the v2 self-only scope
    (no claim that proxytool manages backend processes).
    """
    parser = cli.build_parser()
    sub = parser._subparsers
    assert sub is not None  # required=True subparsers exist
    first_group = sub._group_actions[0]
    assert isinstance(first_group.choices, dict) and first_group.choices
    choices = first_group.choices
    res = subprocess.run([sys.executable, "-m", "proxytool", "--help"],
                         capture_output=True, text=True, timeout=30)
    top = res.stdout
    assert res.returncode == 0
    assert "self-only" in top
    assert "optionally managing the local LLM backend" not in top

    for name in choices:
        out = subprocess.run(
            [sys.executable, "-m", "proxytool", name, "--help"],
            capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, name
        lines = out.stdout.splitlines()
        # Description = nonblank lines between the usage ladder's first
        # blank line and the options/positional section header.
        first_blank = next(i for i, line in enumerate(lines) if not line.strip())
        desc: list[str] = []
        for line in lines[first_blank:]:
            if line.strip() in ("options:", "positional arguments:", "usage:"):
                break
            if line.strip():
                desc.append(line)
        assert len(desc) >= 2, f"{name}: no description paragraph:\n{out.stdout!r}"
        assert not desc[0].lstrip().startswith("["), (
            f"{name}: usage ladder leaked into description: {desc[0]!r}")


# ---------------------------------------------------------------- real-server smoke
class _OKHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format=None, *args):  # stdlib signature
        pass


def test_cmd_health_against_real_localhost_server(state_env):
    served = HTTPServer(("127.0.0.1", 0), _OKHandler)
    port = served.server_address[1]
    th = Thread(target=served.serve_forever, daemon=True)
    th.start()
    try:
        assert cli.cmd_health(_parse(["health", "--port", str(port)])) == 0
    finally:
        served.shutdown()
        served.server_close()
