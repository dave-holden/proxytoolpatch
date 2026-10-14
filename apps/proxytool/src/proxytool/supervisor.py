"""Managed-process supervisor: launch backends the middleware fronts.

Configured per-process with command, port, health-check URL/path, startup
timeout and graceful-shutdown timeout (all overridable per process and via
``LTP_PROCESS_<NAME>_*`` env vars). Requests to the middleware are then routed
to the selected backend's base URL — see ``EndpointMap``.
"""

from __future__ import annotations

import logging as _std_logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from toolpatch.logging import get_logger

_log = get_logger("proxytool.supervisor")


def _is_real_logger(obj: Any) -> bool:
    """Best-effort check: a stdlib Logger configured by app startup."""
    if obj is None:
        return False
    if not hasattr(obj, "handle"):
        return False
    return isinstance(obj, _std_logging.Logger)


def _can_emit(obj: Any) -> bool:
    return _is_real_logger(obj)


@dataclass(frozen=True)
class ManagedProcess:
    """One child process the CLI should keep alive for the middleware's life."""

    name: str
    command: str  # full shell command line; the cwd's venv PATH applies
    port: int
    base_url: str | None = None  # routing id when >1 backend; else None
    health_path: str = "/health"
    startup_timeout: float = 120.0
    shutdown_timeout: float = 30.0

    def url_for(self, path: str) -> str:
        base = self.base_url or f"http://127.0.0.1:{self.port}"
        if not path.startswith("/"):
            path = "/" + path
        return f"{base.rstrip('/')}{path}"


def _probe(url: str, timeout: float) -> bool:
    try:
        with urlopen(url, timeout=min(timeout, 5.0)) as resp:
            return resp.status < 400
    except Exception:
        # urllib raises on >=4xx as HTTPError; anything raised = "not ready yet"
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _env_overrides(name: str) -> dict[str, str]:
    """LTP_PROCESS_<NAME>_COMMAND / _PORT / _HEALTH_PATH / _BASE_URL / _STARTUP_TIMEOUT."""
    prefix = f"LTP_PROCESS_{name.upper().replace('-', '_')}_"
    return {k[len(prefix):]: v for k, v in os.environ.items()
            if k.startswith(prefix)}


class Supervisor:
    """Starts/stops managed child processes and health-checks them."""

    def __init__(self, processes: list[ManagedProcess]) -> None:
        self._procs = processes
        self._children: dict[str, subprocess.Popen] = {}

    # -- lifecycle ----------------------------------------------------------
    @contextmanager
    def run(self) -> Iterator[Supervisor]:
        try:
            for p in self._procs:
                self.start(p)
            yield self
        finally:
            self.shutdown()

    def start(self, proc: ManagedProcess) -> None:
        if proc.name in self._children:
            return
        if not _port_free(proc.port):
            if _can_emit(_log):
                _log.info(
                    "%s: port %d already in use — assuming an existing %s; "
                    "not starting a second one.",
                    proc.name, proc.port, proc.name,
                )
            self._children[proc.name] = subprocess.Popen(["true"])  # placeholder
            return
        if _can_emit(_log):
            _log.info("starting %s: %s", proc.name, proc.command)

        child = subprocess.Popen(
            proc.command, shell=True,
            stdout=sys.stderr, stderr=sys.stderr,
        )
        try:
            self._wait_until_ready(proc)
        except TimeoutError as e:
            child.terminate()
            raise RuntimeError(
                f"{proc.name} failed to become healthy on "
                f"{proc.url_for(proc.health_path)} within "
                f"{proc.startup_timeout}s"
            ) from e
        self._children[proc.name] = child

    def _wait_until_ready(self, proc: ManagedProcess) -> None:
        deadline = time.monotonic() + proc.startup_timeout
        health_url = proc.url_for(proc.health_path)
        while time.monotonic() < deadline:
            if _probe(health_url, proc.startup_timeout):
                if _can_emit(_log):
                    _log.info("%s healthy at %s", proc.name, health_url)
                return
            if (
                proc.name in self._children
                and self._children[proc.name].poll() is not None
            ):
                raise RuntimeError(
                    f"{proc.name} exited (rc={self._children[proc.name].returncode}) "
                    "before becoming healthy"
                )
            time.sleep(0.5)
        raise TimeoutError(f"{proc.name} health check never passed: {health_url}")

    def shutdown(self) -> None:
        """SIGTERM children, wait for a graceful window, then SIGKILL leftovers."""
        for name, child in self._children.items():
            try:
                child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                continue
            if _can_emit(_log):
                _log.info("stopped %s", name)
        deadline = time.monotonic() + 10.0
        for name, child in self._children.items():
            try:
                child.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                if _can_emit(_log):
                    _log.warning("SIGKILL after graceful window: %s", name)
                child.kill()
                child.wait(timeout=5)



def _parse_command(text: str) -> tuple[str, int, str | None, str]:
    """Parse one ``LTP_COMPILE``-style spec: ``NAME=BASE_URL::command`` (shlex-aware)."""
    if "::" not in text:
        raise ValueError(
            "managed process spec must look like 'NAME=http://host:port::command'"
        )
    head, command = text.split("::", 1)
    name, _, url = head.partition("=")
    if not name or not url:
        raise ValueError(f"managed process spec missing NAME or URL: {text!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.port:
        raise ValueError(f"managed process URL must include scheme+port: {url!r}")
    # Unquote any quoted parts of the command so `shlex` round-trips cleanly.
    return (
        name.strip(),
        parsed.port or 0,
        f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        command,
    )


def parse_managed_specs(specs: list[str] | None,
                        backend_kind: str) -> list[ManagedProcess]:
    """Build ManagedProcess list; defaults come from the --upstream backend kind."""
    # NOTE: `advisor` regexes / shlex join — one entry per process.
    specs = specs or []
    out: list[ManagedProcess] = []
    for spec in specs:
        name, port, base, command = _parse_command(spec)
        overrides = _env_overrides(name)
        timeout = float(overrides.get("STARTUP_TIMEOUT", "120"))
        health = overrides.get("HEALTH_PATH", "/health")
        out.append(ManagedProcess(
            name=name,
            command=command.strip(),
            port=port,
            base_url=base,
            health_path=health,
            startup_timeout=timeout,
            shutdown_timeout=float(overrides.get("SHUTDOWN_TIMEOUT", "30")),
        ))
    # No managed children were asked for: probe default ports the backend
    # templates imply (8181 gguf/mlx; 8080 llama-swap) but do not start anything.
    _ = backend_kind  # legacy settings still carry it for defaults downstream
    return out


def default_specs_for(backend_kind: str, model: str | None) -> list[str]:
    """Shell command templates applied when the CLI is given no explicit specs."""
    if not model:
        return []
    q = shlex.quote(model)
    if backend_kind == "gguf":
        return [
            "gguf=http://127.0.0.1:8181::llama-server --host 127.0.0.1 --port 8181"
            f" --ctx-size 16384 --jinja --model {q}"
        ]
    if backend_kind == "mlx":
        return [
            "mlx=http://127.0.0.1:8181::python -m mlx_lm.server"
            f" --model {q} --port 8181"
        ]
    if backend_kind == "omlx":
        return [f"omlx=http://127.0.0.1:8181::omlx serve {q} --port 8181"]
    return []


def env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}
