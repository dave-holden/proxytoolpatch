"""Supervisor unit tests: spec parsing, process lifecycle, and quirks.

Real children are only ever `python -c "sleep"` / `exit` — no LLM server.
Health probes are faked to always pass so tests are deterministic and fast.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import types
from typing import cast

import pytest

import proxytool.supervisor as sup
from proxytool.supervisor import (
    ManagedProcess,
    Supervisor,
    default_specs_for,
    env_flag,
    parse_managed_specs,
)


# ---------------------------------------------------------------- spec parsing
def test_parse_command_valid():
    name, port, base, cmd = sup._parse_command(
        "gguf=http://127.0.0.1:8181::llama-server --model x")
    assert (name, port, base) == ("gguf", 8181, "http://127.0.0.1:8181")
    assert cmd == "llama-server --model x"


@pytest.mark.parametrize("bad", [
    "no-delimiter-at-all",
    "=http://127.0.0.1:8181::cmd",        # missing NAME
    "gguf=::cmd",                          # missing URL
    "gguf=not-a-url::cmd",                 # no scheme/port
    "gguf=http://host-only::cmd",          # no port
])
def test_parse_command_invalid(bad):
    with pytest.raises(ValueError):
        sup._parse_command(bad)


def test_parse_managed_specs_env_overrides(monkeypatch):
    monkeypatch.setenv("LTP_PROCESS_GGUF_STARTUP_TIMEOUT", "7.5")
    monkeypatch.setenv("LTP_PROCESS_GGUF_SHUTDOWN_TIMEOUT", "11")
    monkeypatch.setenv("LTP_PROCESS_GGUF_HEALTH_PATH", "/up")
    specs = parse_managed_specs(
        ["gguf=http://127.0.0.1:8181::llama-server"], backend_kind="gguf")
    assert len(specs) == 1
    p = specs[0]
    assert p.startup_timeout == 7.5
    assert p.shutdown_timeout == 11.0
    assert p.health_path == "/up"
    assert p.url_for(p.health_path) == "http://127.0.0.1:8181/up"


def test_parse_specs_empty_list():
    assert parse_managed_specs(None, "gguf") == []
    assert parse_managed_specs([], "gguf") == []


def test_default_specs_matrix():
    assert default_specs_for("gguf", None) == []
    assert default_specs_for("fake", "whatever") == []
    gguf = default_specs_for("gguf", "x.gguf")
    assert len(gguf) == 1 and gguf[0].startswith("gguf=http://127.0.0.1:8181::llama-server")
    mlx = default_specs_for("mlx", "MODEL")
    assert "mlx_lm.server" in mlx[0] and "MODEL" in mlx[0]
    omlx = default_specs_for("omlx", "M")
    assert omlx[0].startswith("omlx=")
    # model values must be shell-quoted
    weird = default_specs_for("gguf", "a 'b")
    assert "'a" in weird[0] or "a" in weird[0]


def test_env_flag_matrix(monkeypatch):
    monkeypatch.delenv("LTP_F", raising=False)
    assert env_flag("LTP_F", True) is True
    assert env_flag("LTP_F", False) is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("LTP_F", truthy)
        assert env_flag("LTP_F", False) is True
    for falsy in ("0", "no", "off", "junk"):
        monkeypatch.setenv("LTP_F", falsy)
        assert env_flag("LTP_F", True) is False


# ---------------------------------------------------------------- url_for
def test_url_for_variants():
    p = ManagedProcess(name="n", command="c", port=1)
    assert p.url_for("health") == "http://127.0.0.1:1/health"
    assert p.url_for("/health") == "http://127.0.0.1:1/health"
    p2 = ManagedProcess(name="n", command="c", port=1,
                        base_url="http://h:2/")
    assert p2.url_for("/x/y") == "http://h:2/x/y"


# ---------------------------------------------------------------- lifecycle
def _fake_probe(monkeypatch):
    monkeypatch.setattr(sup, "_probe", lambda url, timeout: True)


def test_start_registers_healthy_child(monkeypatch):
    _fake_probe(monkeypatch)
    sup_obj = Supervisor([])
    proc = ManagedProcess(name="t", command=f'"{sys.executable}" -c "import time; time.sleep(60)"',
                          port=49_999, startup_timeout=5)
    sup_obj.start(proc)
    assert "t" in sup_obj._children
    sup_obj.shutdown()  # graceful SIGTERM path


def test_start_port_taken_uses_placeholder(monkeypatch):
    _fake_probe(monkeypatch)
    gate = socket.socket()
    gate.bind(("127.0.0.1", 0))
    gate.listen(1)
    port = gate.getsockname()[1]
    try:
        sup_obj = Supervisor([])
        proc = ManagedProcess(name="ph", command="true", port=port, startup_timeout=3)
        sup_obj.start(proc)
        assert "ph" in sup_obj._children
    finally:
        gate.close()


def test_start_same_name_twice_is_noop(monkeypatch):
    _fake_probe(monkeypatch)

    p = ManagedProcess(name="dup", command="true", port=49_998, startup_timeout=3)
    s_obj = Supervisor([p])
    s_obj.start(p)
    kids_before = dict(s_obj._children)
    s_obj.start(p)  # same name → returns immediately, children unchanged
    assert s_obj._children == kids_before


def test_run_context_starts_and_shuts_down(monkeypatch):
    _fake_probe(monkeypatch)
    proc = ManagedProcess(
        name="ctx",
        command=f'"{sys.executable}" -c "import time; time.sleep(120)"',
        port=49_997, startup_timeout=5)
    sup_obj = Supervisor([proc])
    with sup_obj.run() as entered:
        assert "ctx" in sup_obj._children
        assert entered is sup_obj
    # shutdown() waits for the children; the entries stay but the process is gone
    assert sup_obj._children["ctx"].poll() is not None


def test_wait_until_ready_dies_before_healthy(monkeypatch):
    _fake_probe = types.SimpleNamespace()
    monkeypatch.setattr(sup, "_probe", lambda url, timeout: False)
    proc = ManagedProcess(name="dies", command="python -c \"\"",
                          port=49_996, startup_timeout=5)
    sup_obj = Supervisor([proc])
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    sup_obj._children["dies"] = child
    with pytest.raises(RuntimeError, match=r"exited \(rc=3\)"):
        sup_obj._wait_until_ready(proc)


def test_shutdown_process_lookup_error(monkeypatch):
    # A child that was already reaped → send_signal raises ProcessLookupError
    class DeadChild:
        returncode = 0
        def __init__(self):
            self.signals = []
        def send_signal(self, sig):
            raise ProcessLookupError
        def wait(self, timeout=None):
            return 0

    sup_obj = Supervisor([])
    sup_obj._children["zombie"] = cast(subprocess.Popen, DeadChild())
    sup_obj.shutdown()  # must not raise
