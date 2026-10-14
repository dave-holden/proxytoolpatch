"""``proxytool`` CLI — a thin launcher around the FastAPI app.

It does NOT replace Uvicorn: it builds a uvicorn ``Config``/``Server`` in-process,
optionally starts managed child processes (llama-server, mlx_lm.server, ...)
first, health-checks them, and routes requests to the selected backend. Every
setting can come from either a CLI flag or an env var (``LTP_*``, formerly
``LMW_*`` — both are read, ``LTP_`` wins).

Usage::

    # managed gguf backend (starts llama-server for you)
    proxytool serve --upstream gguf \
        --managed 'gguf=http://127.0.0.1:8181::llama-server --port 8181 --model $MODEL'

    # external backend already running (no child processes)
    proxytool serve --upstream-url http://127.0.0.1:8181

    # two backends routed by model id
    proxytool serve \
        --upstream gguf=http://127.0.0.1:8181 --upstream-models gguf=qwen-gguf \
        --upstream mlx=http://127.0.0.1:8281 --upstream-models mlx=qwen-mlx \
        --default-backend mlx
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import tempfile
import time
from typing import Any

import uvicorn

from proxytool.config import apply_config, config_help_line, describe_config
from proxytool.routing import Backend, backends_from_args, backends_from_env
from proxytool.supervisor import ManagedProcess, Supervisor, default_specs_for, parse_managed_specs


def _apply_toml_config(args: Any, *, serve: bool, routing: bool = False
                       ) -> tuple[pathlib.Path | None, dict[str, Any], dict[str, Any]]:
    """Bridge the TOML config file into env; exit(2) with a clear message on error.

    Precedence everywhere: CLI flag > env var > config file > default —
    the bridge only fills vars neither a flag nor the env expressed.
    """
    try:
        return apply_config(args, serve=serve, routing=routing)
    except ValueError as e:
        print(f"proxytool: config error: {e}", file=sys.stderr)
        raise SystemExit(2) from e


def _flag_or_env(args: Any, env_keys: list[str], attr: str) -> str:
    """First truthy CLI attr wins; else first truthy env var; else default."""
    if getattr(args, attr, None):
        return getattr(args, attr)
    for key in env_keys:
        if os.environ.get(key):
            return os.environ[key]
    return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proxytool",
        description="Launch the proxytool middleware (FastAPI) — optionally "
                    "managing the local LLM backend(s) it fronts.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the middleware (+ managed backends)")
    s.add_argument("--host", default=None, help="bind host (LTP_HOST, default 127.0.0.1)")
    s.add_argument("--port", type=int, default=None, help="bind port (LTP_PORT, default 8180)")

    # Routing: which backend(s) to front.
    s.add_argument(
        "--upstream", action="append", default=[], metavar="NAME=URL",
        help="register a backend, repeatable (LTP_UPSTREAM_<NAME>=URL)",
    )
    s.add_argument(
        "--upstream-models", action="append", default=[], metavar="NAME=m1,m2",
        help="model ids that route to a backend, repeatable "
             "(LTP_UPSTREAM_<NAME>_MODELS=...)",
    )
    s.add_argument(
        "--default-backend", default=None,
        help="backend used when no model list matches (LTP_DEFAULT_BACKEND)",
    )
    s.add_argument(
        "--upstream-url", default=None,
        help="standalone single-backend URL (LMW_UPSTREAM_URL/LTP_UPSTREAM_URL)",
    )

    # Managed children.
    s.add_argument(
        "--managed", action="append", default=[], metavar="NAME=URL::CMD",
        help="child process spec, repeatable, e.g. "
             "--managed 'gguf=http://127.0.0.1:8181::llama-server --port 8181 ...'",
    )
    s.add_argument(
        "--backend", choices=("gguf", "mlx", "omlx", "fake"), default=None,
        help="default managed-process template + request sanitizer mode "
             "(LMW_BACKEND/LTP_BACKEND)",
    )
    s.add_argument(
        "--model", default=None,
        help="model artifact for the default managed template (gguf/mlx/omlx)",
    )
    s.add_argument(
        "--no-managed", action="store_true",
        help="do not start any child processes even if --managed/--backend imply some",
    )

    # Request-side knobs forwarded to Settings.
    s.add_argument("--model-map", default=None, help="inline JSON map (LMW_MODEL_MAP)")
    s.add_argument("--model-map-file", default=None,
                   help="JSON file map (LMW_MODEL_MAP_FILE)")
    s.add_argument("--upstream-api-key", default=None, help="bearer token upstream")
    s.add_argument("--upstream-timeout", type=float, default=None,
                   help="seconds (LMW_UPSTREAM_TIMEOUT)")
    s.add_argument("--trace", action="store_true", help="LMW_TRACE=1 / LTP_TRACE=1")
    s.add_argument(
        "--loop-nudge", type=int, default=None, help="LMW_LOOP_NUDGE (0 = off)"
    )
    s.add_argument(
        "--loop-abort", type=int, default=None, help="LMW_LOOP_ABORT (0 = off)"
    )
    s.add_argument(
        "--log-level", default=None,
        choices=("debug", "info", "warning", "error"),
        help="app log level (LTP_LOG_LEVEL / LMW_LOG_LEVEL)"
    )
    s.add_argument("--quiet", action="store_true",
                   help="only log errors; equivalent to --log-level error")
    s.add_argument("--config", default=None, help=config_help_line())

    # Lifecycle subcommands operate on an already-running (or not) server.
    for name, help_text in (
        ("status", "show the running/not-running state of the middleware"),
        ("stop", "stop a proxytool started with `start` (graceful SIGTERM)"),
        ("health", "probe /health on the middleware and each registered backend"),
        ("models", "list the model ids each upstream reports (/v1/models)"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--host", default=None, help="middleware host (LTP_HOST)")
        sp.add_argument("--port", type=int, default=None, help="middleware port (LTP_PORT)")
        sp.add_argument("--config", default=None, help=config_help_line())

    start = sub.add_parser(
        "start",
        help="daemonize: launch `serve` in the background, return immediately",
    )
    # `start` forwards every serve flag; build it by mirroring serve's args.
    _mirror_serve_args(start)
    start.add_argument("--host", default=None, help="bind host (LTP_HOST)")
    start.add_argument("--port", type=int, default=None, help="bind port (LTP_PORT)")
    start.add_argument("--log-level", default=None,
                       choices=("debug", "info", "warning", "error"),
                       help="app log level for the daemonized server")
    start.add_argument("--quiet", action="store_true",
                       help="only log errors in the daemonized server")
    start.add_argument("--config", default=None, help=config_help_line())

    c = sub.add_parser(
        "config",
        help="diagnose the TOML config: discovery, validation, resolved settings",
        description="Show which config file discovery finds, validate it "
                    "strictly (unknown keys, bad types), and print every "
                    "setting `serve`/`start` would run with — value + "
                    "source. Never starts a server.",
    )
    c.add_argument(
        "action", nargs="?", choices=("print", "check"), default="print",
        help="print = discovery + validation + full settings table "
             "(default); check = discovery + validation only",
    )
    c.add_argument("--json", action="store_true",
                   help="machine-readable output instead of the table")
    c.add_argument("--config", default=None, help=config_help_line())

    sub.add_parser("version", help="print the proxytool version")
    return p


def _merge_env_into_process_env(args: Any,
                                backends: dict[str, Backend]) -> None:
    """Ensure settings_* env vars are visible to a reloaded ProxySettings."""
    if args.backend and not os.environ.get("LMW_BACKEND"):
        os.environ["LMW_BACKEND"] = args.backend
    if args.upstream_api_key and not os.environ.get("LMW_UPSTREAM_API_KEY"):
        os.environ["LMW_UPSTREAM_API_KEY"] = args.upstream_api_key
    if args.upstream_timeout and not os.environ.get("LMW_UPSTREAM_TIMEOUT"):
        os.environ["LMW_UPSTREAM_TIMEOUT"] = str(args.upstream_timeout)
    if args.model_map and not os.environ.get("LMW_MODEL_MAP"):
        os.environ["LMW_MODEL_MAP"] = args.model_map
    if args.model_map_file and not os.environ.get("LMW_MODEL_MAP_FILE"):
        os.environ["LMW_MODEL_MAP_FILE"] = args.model_map_file
    if args.trace and not os.environ.get("LMW_TRACE"):
        os.environ["LMW_TRACE"] = "1"
    if args.loop_nudge is not None and not os.environ.get("LMW_LOOP_NUDGE"):
        os.environ["LMW_LOOP_NUDGE"] = str(args.loop_nudge)
    if args.loop_abort is not None and not os.environ.get("LMW_LOOP_ABORT"):
        os.environ["LMW_LOOP_ABORT"] = str(args.loop_abort)
    if backends and not os.environ.get("LTP_DEFAULT_BACKEND"):
        d = next((n for n, b in backends.items() if b.default), None)
        if d:
            os.environ["LTP_DEFAULT_BACKEND"] = d


def _pick_single_backend_url(backends: dict[str, Backend], args: Any,
                             env_upstream: str) -> str:
    """When the app-level Settings only supports one upstream, pick it here."""
    explicit = _flag_or_env(args, ["LTP_UPSTREAM_URL", "LMW_UPSTREAM_URL"], "upstream_url")
    if explicit:
        return explicit
    if backends:
        d = next((b for b in backends.values() if b.default), None)
        return (d or next(iter(backends.values()))).url
    return env_upstream or "http://127.0.0.1:8181"


def cmd_serve(args: Any) -> int:
    # 0. TOML config bridge (precedence: flag > env > file > default).
    cfg_path, _cfg, cfg_info = _apply_toml_config(args, serve=True, routing=True)
    if cfg_path:
        print(f"[proxytool] config: using {cfg_path} "
              f"({len(cfg_info['applied'])} setting(s) bridged)", file=sys.stderr,
              flush=True)

    # 1. Resolve backends: explicit flags, then env, then single --upstream-url.
    backends = backends_from_args(
        args.upstream, dict(args.upstream_models or {}), args.default_backend
    )
    if not backends:
        backends = backends_from_env()
    if args.upstream_url:
        backends.setdefault("default", Backend(
            name="default", url=args.upstream_url.rstrip("/"),
            default=len(backends) == 0,
        ))

    # 2. Managed children (from --managed specs, config [managed.*], else
    #    backend templates). `--backend` is optional when `--model` is given:
    #    the backend kind is inferred from the artifact (see _infer_backend).
    specs = list(args.managed or []) or list(cfg_info.get("managed_specs") or [])
    eff_backend = args.backend or cfg_info.get("backend")
    eff_model = args.model or cfg_info.get("model")
    if eff_model and not eff_backend:
        eff_backend = _infer_backend(str(eff_model))
        if eff_backend is None:
            print(f"proxytool: cannot infer the backend from --model {eff_model!r} "
                  "— pass --backend gguf|mlx|omlx|fake explicitly", file=sys.stderr)
            return 2
    if not specs and eff_backend and not args.no_managed:
        specs = default_specs_for(eff_backend, eff_model)
    processes: list[ManagedProcess] = parse_managed_specs(specs, eff_backend or "gguf")

    # 3. Settings env bridge (flags → env → Settings defaults).
    _merge_env_into_process_env(args, backends)
    # App-level logging is configured once here (CLI flags / env). It is
    # intentionally separate from Uvicorn's log_level below, which only
    # controls the server/framework log output.
    from proxytool import configure_logging as _cfg
    _cfg(level=args.log_level, quiet=args.quiet)
    # proxytool.main builds its own Settings from env at import time (inside
    # uvicorn.run), so resolved values must be exported as env, not mutated
    # on a local instance uvicorn never sees.
    resolved_url = _pick_single_backend_url(
        backends, args, os.environ.get("LMW_UPSTREAM_URL", "")
    )
    os.environ["LMW_UPSTREAM_URL"] = resolved_url
    os.environ.setdefault("LTP_UPSTREAM_URL", resolved_url)
    # Multi-backend table: only export when the user declared >1 backend (or
    # declared one by name) — a single default backend would otherwise make
    # proxytool.main build a router where single-conduit logic already suffices.
    named = [b for b in backends.values() if b.name != "default"]
    if len(named) >= 2 or (len(named) == 1 and not args.upstream_url):
        for b in named:
            os.environ[f"LTP_UPSTREAM_{b.name.upper()}"] = b.url
            if b.models:
                os.environ[f"LTP_UPSTREAM_{b.name.upper()}_MODELS"] = ",".join(b.models)
        d = next((b for b in backends.values() if b.default and b.name != "default"), None)
        if d:
            os.environ["LTP_DEFAULT_BACKEND"] = d.name
    if args.trace:
        os.environ["LMW_TRACE"] = "1"
    print(f"[proxytool] middleware on {args.host or '127.0.0.1'}:{args.port or 8180}"
          f" → upstream {resolved_url}", file=sys.stderr, flush=True)

    # 4. Run children + uvicorn in the same event loop / signal context.
    supervisor = Supervisor(processes)
    with supervisor.run():
        # Thin launcher: we still hand off to Uvicorn, never replace it.
        uvicorn.run(
            "proxytool.main:app",
            host=args.host or os.environ.get("LTP_HOST", "127.0.0.1"),
            port=args.port if args.port is not None else int(os.environ.get("LTP_PORT", "8180")),
            log_level=os.environ.get("LTP_LOG_LEVEL", "info"),
        )  # blocking; SIGINT/SIGTERM propagate into the with-block exit
    return 0


def cmd_config(args: Any) -> int:
    """Diagnose the TOML config without touching the environment."""
    info = describe_config(args)
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return 0 if info["valid"] else 2
    if not info["valid"]:
        print("proxytool: config error:", file=sys.stderr)
        for line in info["errors"]:
            print(f"  {line}", file=sys.stderr)
        return 2
    print(f"config file: {info['file'] or '(none found)'}")
    for cand in info["searched"]:
        mark = "   <- found" if info["file"] and cand == info["file"] else ""
        print(f"  searched {cand}{mark}")
    if getattr(args, "action", "print") == "check":
        applied = len(info["applied"])
        print(f"validation: ok ({applied} setting(s) would be bridged)")
        return 0
    print("validation: ok")
    print(f"precedence: {info['precedence']}")
    if not info["settings"]:
        print("effective settings: (none — everything stays at defaults)")
        return 0
    width = max(len(r["key"]) for r in info["settings"])
    print("\neffective settings (what serve/start would run with):")
    for r in info["settings"]:
        src = r["source"] + (f" ({r['detail']})" if r["detail"] else "")
        print(f"  {r['key']:<{width}}  {r['value']}   <- {src}")
    return 0


def _infer_backend(model: str) -> str | None:
    """Deduce the backend kind from a model artifact, or None when unclear.

    Confident cases only: GGUF snapshots, omlx/mlx paths, mlx-community HF
    repo ids. Anything else (plain HF ids, bare names) stays ambiguous —
    mlX vs omlx servers differ there — and the CLI asks for --backend.
    """
    m = str(model).lower()
    if m.endswith(".gguf"):
        return "gguf"
    if "omlx" in m:  # before the mlx check — "omlx" contains "mlx"
        return "omlx"
    if "mlx" in m:  # covers mlx-community/…, -mlx names, mlx directories
        return "mlx"
    return None


def cmd_version(_args: Any) -> int:
    from importlib.metadata import PackageNotFoundError, version

    try:
        print(f"proxytool {version('proxytool')}")
    except PackageNotFoundError:
        print("proxytool (unversioned dev checkout)")
    return 0


# Flags `start` shares with `serve`: (dest, cli-flag, type, metavar/help).
# Kept explicit instead of introspecting serve's parser — the "recursive
# mirror" trick ran build_parser() unconditionally, which recursed forever.
_SERVE_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("backend", "--backend", "backend kind (gguf|mlx|omlx|fake)"),
    ("model", "--model", "model artifact for the managed template"),
    ("upstream_url", "--upstream-url", "standalone single-backend URL"),
    ("model_map", "--model-map", "inline JSON alias map"),
    ("model_map_file", "--model-map-file", "JSON alias-map file"),
    ("upstream_api_key", "--upstream-api-key", "bearer token upstream"),
    ("upstream_timeout", "--upstream-timeout", "upstream read timeout (seconds)"),
    ("default_backend", "--default-backend", "fallback backend name"),
)


def _mirror_serve_args(parser: argparse.ArgumentParser) -> None:
    """Give `start` the same flags as `serve` (it wraps `serve`)."""
    for dest, flag, help_text in _SERVE_FLAGS:
        parser.add_argument(flag, dest=dest, default=None, help=help_text)
    parser.add_argument("--upstream", action="append", default=[],
                        metavar="NAME=URL", help="register a backend, repeatable")
    parser.add_argument("--upstream-models", action="append", default=[],
                        metavar="NAME=m1,m2", help="model ids routed to a backend")
    parser.add_argument("--managed", action="append", default=[],
                        metavar="NAME=URL::CMD", help="child process spec, repeatable")
    parser.add_argument("--no-managed", action="store_true",
                        help="do not start any child processes")
    parser.add_argument("--trace", action="store_true", help="payload trace on")
    parser.add_argument("--loop-nudge", type=int, default=None, help="loop nudge threshold")
    parser.add_argument("--loop-abort", type=int, default=None, help="loop abort threshold")


def _state_path() -> pathlib.Path:
    if p := os.environ.get("LTP_STATE_PATH"):
        return pathlib.Path(p)
    return pathlib.Path(tempfile.gettempdir()) / "proxytool-serve-state.json"


def _read_state() -> dict[str, Any] | None:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(state: dict[str, Any] | None) -> None:
    path = _state_path()
    if state is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence probe only
        return True
    except (OSError, ProcessLookupError):
        return False


def _host_port(args: Any) -> tuple[str, int]:
    host = args.host or os.environ.get("LTP_HOST", "127.0.0.1")
    port = args.port if args.port is not None else int(os.environ.get("LTP_PORT", "8180"))
    return str(host), port


def cmd_status(_args: Any) -> int:
    """Check whether the server (started via ``start``) is alive and healthy."""
    _apply_toml_config(_args, serve=False)  # config host/port for the probe
    state = _read_state()
    host, port = (_host_port(_args) if hasattr(_args, "host") else ("127.0.0.1", 8180))
    if state and _pid_alive(int(state.get("pid", 0))):
        print(f"proxytool status: RUNNING (pid {state['pid']}, http://{host}:{port})")
        return 0
    if state:
        print(f"proxytool status: STALE (pid {state.get('pid')} gone) — clearing state")
        _write_state(None)
        return 1
    print(f"proxytool status: STOPPED (no state file at {_state_path()})")
    return 3  # conventional "stopped but fine" exit


def cmd_stop(_args: Any) -> int:
    """Gracefully stop a daemonized server: SIGTERM, wait, then SIGKILL."""
    _apply_toml_config(_args, serve=False)  # no-op unless config sets state/port
    state = _read_state()
    if not state:
        print("proxytool stop: not running (no state file)", file=sys.stderr)
        return 3
    pid = int(state.get("pid", 0))
    if not _pid_alive(pid):
        print(f"proxytool stop: pid {pid} already gone — clearing stale state")
        _write_state(None)
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _write_state(None)
            print("proxytool stop: stopped")
            return 0
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    _write_state(None)
    print("proxytool stop: SIGKILL after grace window (force-stopped)")
    return 0


def cmd_start(args: Any) -> int:
    """Daemonize ``serve``: same flags, detached, PID recorded for stop/status."""
    import subprocess

    # Config host/port must shape the child argv; the child re-reads the
    # file itself (same cwd discovery or the forwarded --config below).
    _apply_toml_config(args, serve=False)
    host, port = _host_port(args)
    state = _read_state()
    if state and _pid_alive(int(state.get("pid", 0))):
        print(f"proxytool start: already running (pid {state['pid']}) — use stop first")
        return 1
    # Rebuild the exact serve invocation from the parsed flags.
    argv = [sys.executable, "-m", "proxytool", "serve", "--host", host, "--port", str(port)]
    for name in ("backend", "model", "upstream_url", "model_map", "model_map_file",
                 "upstream_api_key"):
        v = getattr(args, name, None)
        if v:
            argv += [f"--{name.replace('_', '-')}", str(v)]
    for spec in (args.upstream or []):
        argv += ["--upstream", spec]
    for spec in (args.upstream_models or []):
        argv += ["--upstream-models", spec]
    if args.default_backend:
        argv += ["--default-backend", args.default_backend]
    for spec in (args.managed or []):
        argv += ["--managed", spec]
    if args.no_managed:
        argv.append("--no-managed")
    if args.trace:
        argv.append("--trace")
    if args.loop_nudge is not None:
        argv += ["--loop-nudge", str(args.loop_nudge)]
    if args.loop_abort is not None:
        argv += ["--loop-abort", str(args.loop_abort)]
    if getattr(args, "log_level", None):
        argv += ["--log-level", args.log_level]
    if getattr(args, "quiet", False):
        argv.append("--quiet")
    if getattr(args, "config", None):
        argv += ["--config", args.config]

    log_path = _state_path().with_suffix(".log")
    with open(log_path, "ab") as log:
        child = subprocess.Popen(
            argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from our terminal's process group
        )
    # Wait for /health so `start` returns only when the server truly answers.
    import urllib.request
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as r:
                if r.status < 400:
                    break
        except Exception:
            if child.poll() is not None:
                print(f"proxytool start: process died during startup — see {log_path}",
                      file=sys.stderr)
                return 1
            time.sleep(0.3)
    else:
        child.terminate()
        print(f"proxytool start: /health never came up within 30s — see {log_path}",
              file=sys.stderr)
        return 1
    _write_state({"pid": child.pid, "host": host, "port": port,
                  "log": str(log_path), "argv": argv[2:]})
    print(f"proxytool start: running (pid {child.pid}, http://{host}:{port}, log {log_path})")
    return 0


def cmd_health(args: Any) -> int:
    """Probe the middleware and every registered backend; print a JSON report."""
    import urllib.request

    # Config can define the middleware host/port and the [routing.*] table so
    # `proxytool health` "just works" without repeating flags/env.
    _apply_toml_config(args, serve=False, routing=True)

    host, port = _host_port(args)
    report: dict[str, Any] = {"middleware": {"url": f"http://{host}:{port}", "ok": False}}
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
            report["middleware"]["ok"] = r.status < 400
    except Exception as e:
        report["middleware"]["error"] = str(e)

    # Backends registered in env (routed setups); probe each, plus /v1/models.
    from proxytool.routing import backends_from_env

    backends = backends_from_env()
    probe_list: list[dict[str, Any]] = []
    for b in backends.values():
        entry: dict[str, Any] = {"name": b.name, "url": b.url, "ok": False}
        try:
            with urllib.request.urlopen(b.url.rstrip("/") + "/health", timeout=3) as r:
                entry["ok"] = r.status < 400
        except Exception as e:
            try:
                with urllib.request.urlopen(b.url.rstrip("/") + "/v1/models", timeout=3) as r:
                    entry["ok"] = r.status < 400  # mlx_lm.server has no /health
                    entry["via"] = "/v1/models fallback"
            except Exception:
                entry["error"] = str(e)
        probe_list.append(entry)
    report["backends"] = probe_list
    print(json.dumps(report, indent=2))
    return 0 if report["middleware"]["ok"] else 1


def cmd_models(_args: Any) -> int:
    """List each upstream's /v1/models ids (what routing matches on)."""
    import urllib.request

    # Same as health: the config's [routing.*] table feeds the probes.
    _apply_toml_config(_args, serve=False, routing=True)

    from proxytool.routing import backends_from_env

    out: list[dict[str, Any]] = []
    exit_code = 0
    for b in backends_from_env().values():
        entry: dict[str, Any] = {"name": b.name, "url": b.url}
        try:
            with urllib.request.urlopen(b.url.rstrip("/") + "/v1/models", timeout=5) as r:
                data = json.load(r)
                ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
                entry["models"] = ids
        except Exception as e:
            entry["error"] = str(e)
            exit_code = 1
        out.append(entry)
    print(json.dumps(out, indent=2))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = {
        "serve": cmd_serve,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "health": cmd_health,
        "models": cmd_models,
        "config": cmd_config,
        "version": cmd_version,
    }.get(args.command)
    if handler is None:
        parser.print_usage(sys.stderr)
        return 2
    return handler(args)


def _configure_logging_cli(*, level: str | None, quiet: bool) -> None:
    """Configure app-level logging from the CLI (serve / start).

    This is the CLI-facing entry point for app logging. It deliberately does
    not import ``proxytool.__init__``'s ``configure_logging`` so that
    ``proxytool.cli`` can be parsed/validated without triggering the
    proxytool package import path that expects a built distribution.
    """
    from proxytool import configure_logging as _configure

    _configure(level=level, quiet=quiet)


if __name__ == "__main__":
    sys.exit(main())
