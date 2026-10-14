"""TOML config file support for the ``proxytool`` CLI.

Precedence for every setting:

    CLI flag  >  environment (LTP_* / LMW_*)  >  config file  >  default

Config discovery: ``--config PATH``, then ``LTP_CONFIG``, then
``./proxytool.toml``, ``./.proxytool.toml``, and finally
``$XDG_CONFIG_HOME/proxytool/config.toml`` (default ``~/.config/...``).

The config file is a *bridge*, not a parallel system: recognized values are
exported as the same ``LTP_*`` env vars the rest of the stack already reads
(``settings``, routing, the daemonized child), but only when neither a CLI
flag nor the environment already expressed them. That makes the CLI stay tiny
(``proxytool start`` finds the file and goes) while flags/env keep winning.

Schema (all keys optional)::

    host = "127.0.0.1"
    port = 8180
    log_level = "info"            # debug | info | warning | error
    default_backend = "mlx"

    [upstream]                    # single-backend setup (llama-swap etc.)
    url = "http://127.0.0.1:8080"
    backend = "gguf"              # gguf | mlx | omlx | fake (sanitizer + template)
    model = "/models/qwen.gguf"   # artifact when proxytool launches the backend
    timeout = 600
    api_key = "sk-..."

    [routing.gguf]                # multi-backend table
    url = "http://127.0.0.1:8181"
    models = ["qwen-gguf", "qwen"]
    default = true

    [managed.gguf]                # child process to supervise
    command = "llama-server --port 8181 --model /models/qwen.gguf --jinja"
    base_url = "http://127.0.0.1:8181"   # or port = 8181
    health_path = "/health"
    startup_timeout = 120
    shutdown_timeout = 30

    [model_map]
    "qwen-gguf" = "qwen2.5-coder-14b-instruct"

    [loopguard]
    nudge = 3
    abort = 6

    [trace]
    enabled = false
    file = "/tmp/proxytool-trace.log"
"""

from __future__ import annotations

import json
import os
import re
import sys
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError, field_validator

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10 — tomli is the stdlib tomllib backport
    import tomli as tomllib

CWD_NAMES = ("proxytool.toml", ".proxytool.toml")


def find_config(explicit: str | None = None) -> Path | None:
    """Resolve the active config file path, or None when none exists."""
    if explicit:
        p = Path(explicit). expanduser()
        if not p.is_file():
            raise ValueError(f"config file not found: {explicit}")
        return p
    env = os.environ.get("LTP_CONFIG")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise ValueError(f"LTP_CONFIG points to a missing file: {env}")
        return p
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    cwd = Path.cwd()
    for cand in (cwd / CWD_NAMES[0], cwd / CWD_NAMES[1],
                 Path(xdg) / "proxytool" / "config.toml"):
        if cand.is_file():
            return cand
    return None


def load_config(path: Path | None) -> dict[str, Any]:
    """Parse the TOML config into a dict; {} when path is None."""
    if path is None:
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise ValueError(f"cannot read config {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"invalid TOML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"config {path}: top level must be a table")
    validate_config(data, label=str(path))
    return data


# ── strict schema validation ─────────────────────────────────────────────────
# The bridge below is permissive by design (config only FILLS unset env vars),
# but the file itself is validated strictly here: unknown keys/sections and
# wrong types fail fast at startup with a pointed message instead of surfacing
# later as an indirect runtime symptom (a typo'd port, a bad URL …).


class _CfgModel(BaseModel):
    """Base: forbid unknown keys so typos become config errors, not no-ops."""

    model_config = ConfigDict(extra="forbid")


class _Upstream(_CfgModel):
    url: AnyHttpUrl | None = None
    backend: Literal["gguf", "mlx", "omlx", "fake"] | None = None
    model: str | None = None
    timeout: float | None = Field(default=None, gt=0)
    api_key: str | None = None
    model_map_file: str | None = None


class _Routing(_CfgModel):
    # url presence is enforced with a friendlier message in validate_config.
    url: AnyHttpUrl | None = None
    models: list[str] | str | None = None
    default: bool = False


def _int_not_bool(v: Any) -> Any:
    """Lax int coercion would happily turn TOML ``port = true`` into 1."""
    if isinstance(v, bool):
        raise ValueError("expected an integer, not a boolean")
    return v


class _Managed(_CfgModel):
    # command/base_url/port presence is enforced with friendlier messages in
    # validate_config (the bridge has the same guards as a second line).
    command: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    base_url: AnyHttpUrl | None = None
    health_path: str | None = None
    startup_timeout: float | None = Field(default=None, gt=0)
    shutdown_timeout: float | None = Field(default=None, gt=0)

    @field_validator("port", mode="before")
    @classmethod
    def _mport_not_bool(cls, v: Any) -> Any:
        return _int_not_bool(v)


class _Loopguard(_CfgModel):
    nudge: int | None = Field(default=None, ge=0)
    abort: int | None = Field(default=None, ge=0)


class _Trace(_CfgModel):
    enabled: bool = False
    file: str | None = None


class _Config(_CfgModel):
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error"] | None = None
    default_backend: str | None = None
    upstream: _Upstream | None = None
    routing: dict[str, _Routing] | None = None
    managed: dict[str, _Managed] | None = None
    model_map: dict[str, str] | None = None
    loopguard: _Loopguard | None = None
    trace: _Trace | None = None

    @field_validator("port", mode="before")
    @classmethod
    def _port_not_bool(cls, v: Any) -> Any:
        return _int_not_bool(v)


_TOP_KEYS = tuple(_Config.model_fields)
_SECTION_FIELDS = {
    "upstream": tuple(_Upstream.model_fields),
    "routing": tuple(_Routing.model_fields),
    "managed": tuple(_Managed.model_fields),
    "loopguard": tuple(_Loopguard.model_fields),
    "trace": tuple(_Trace.model_fields),
}


def _did_you_mean(key: str, candidates: tuple[str, ...] | None) -> str:
    if not candidates:
        return ""
    m = get_close_matches(key, list(candidates), n=1, cutoff=0.6)
    return f" — did you mean '{m[0]}'?" if m else ""


def _pydantic_error_lines(exc: ValidationError) -> list[str]:
    """Turn pydantic errors into short, pointed config-error lines."""
    lines: list[str] = []
    for err in exc.errors(include_url=False):
        loc = tuple(str(p) for p in err["loc"])
        if err.get("type") == "extra_forbidden" and loc:
            key = loc[-1]
            if len(loc) == 1:
                lines.append("unknown key '" + key + "' (top level)"
                             + _did_you_mean(key, _TOP_KEYS))
            else:
                section = loc[0]
                lines.append("unknown key '" + key + "' in [" + section + "]"
                             + _did_you_mean(key, _SECTION_FIELDS.get(section)))
            continue
        msg = str(err.get("msg", "invalid value"))
        prefix = "[" + ".".join(loc or ()) + "] " if loc else ""
        lines.append(prefix + msg)
    return lines


def validate_config(data: dict[str, Any], label: str = "config") -> None:
    """Raise ValueError with every problem found, or return silently."""
    errors: list[str] = []

    routing = data.get("routing")
    if routing is not None:
        if not isinstance(routing, dict):
            errors.append("[routing] must be a table of [routing.<name>] entries")
        else:
            for name, t in routing.items():
                if not isinstance(t, dict):
                    errors.append(f"routing.{name} must be a table")
                    continue
                if not t.get("url"):
                    errors.append(f"routing.{name} needs at least a url")
            defaults = [str(n) for n, t in routing.items()
                        if isinstance(t, dict) and t.get("default")]
            if len(defaults) > 1:
                errors.append(
                    "default = true on more than one routing entry: "
                    + ", ".join(defaults))

    managed = data.get("managed")
    if managed is not None:
        if not isinstance(managed, dict):
            errors.append("[managed] must be a table of [managed.<name>] entries")
        else:
            for name, t in managed.items():
                if not isinstance(t, dict):
                    errors.append(f"managed.{name} must be a table")
                elif not t.get("command"):
                    errors.append(f"managed.{name} needs at least a command")
                elif not (t.get("base_url") or t.get("port")):
                    errors.append(
                        f"managed.{name}: set base_url or port for the health check")

    try:
        _Config.model_validate(data)
    except ValidationError as exc:
        errors.extend(_pydantic_error_lines(exc))

    if errors:
        raise ValueError(label + ":\n  - " + "\n  - ".join(errors))


def _env_set(*names: str) -> bool:
    return any(os.environ.get(n) for n in names)


def _set_if_unset(env_key: str, value: str) -> bool:
    """Set ``env_key`` unless it (or its legacy LMW_ twin) is already set."""
    legacy = "LMW_" + env_key[len("LTP_"):]
    if _env_set(env_key, legacy) or not value:
        return False
    os.environ[env_key] = value
    return True


def _flag(attr: str, args: Any) -> Any:
    return getattr(args, attr, None) if args is not None else None


def _flag_unset(value: Any) -> bool:
    return value is None or value is False


def _env_backends_present() -> bool:
    """True when the env already declares a *named* routed backend.

    ``LTP_UPSTREAM_URL`` (the standalone single-backend URL) and the DEFAULT/
    _MODELS helpers are not named backends.
    """
    for var in os.environ:
        if not var.startswith("LTP_UPSTREAM_"):
            continue
        rest = var[len("LTP_UPSTREAM_"):]
        if rest.endswith("_MODELS") or rest in ("URL", "DEFAULT"):
            continue
        return True
    return False


def _bridge_routing(cfg: dict[str, Any], args: Any, applied: list[str]) -> None:
    """[routing.<name>] + default_backend → LTP_UPSTREAM_* env."""
    flag_backends = list(getattr(args, "upstream", None) or [])
    if flag_backends or _env_backends_present():
        return
    for name, table in (cfg.get("routing") or {}).items():
        if not isinstance(table, dict) or not table.get("url"):
            raise ValueError(f"routing.{name} needs at least a url")
        upper = str(name).upper().replace("-", "_")
        if _set_if_unset(f"LTP_UPSTREAM_{upper}", str(table["url"]).rstrip("/")):
            applied.append(f"LTP_UPSTREAM_{upper}")
        models = table.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        if models:
            joined = ",".join(str(m) for m in models)
            if _set_if_unset(f"LTP_UPSTREAM_{upper}_MODELS", joined):
                applied.append(f"LTP_UPSTREAM_{upper}_MODELS")
    flag_default = _flag("default_backend", args)
    if flag_default is None and not _env_set("LTP_DEFAULT_BACKEND"):
        cfg_default = cfg.get("default_backend")
        if cfg_default:
            os.environ["LTP_DEFAULT_BACKEND"] = str(cfg_default)
            applied.append("LTP_DEFAULT_BACKEND")
        else:
            # top-level default_backend unset → accept `default = true` on
            # exactly one [routing.*] entry (the llama-swap-style spelling).
            defaults = [str(n) for n, t in (cfg.get("routing") or {}).items()
                        if isinstance(t, dict) and t.get("default")]
            if len(defaults) > 1:
                raise ValueError(
                    "default = true on more than one routing entry: "
                    + ", ".join(defaults))
            if defaults:
                os.environ["LTP_DEFAULT_BACKEND"] = defaults[0]
                applied.append("LTP_DEFAULT_BACKEND")


def _bridge_settings(cfg: dict[str, Any], args: Any, applied: list[str]) -> None:
    """Top-level + [upstream] + [model_map] + [loopguard] + [trace] settings."""
    if _flag_unset(_flag("host", args)) and "host" in cfg and _set_if_unset(
            "LTP_HOST", str(cfg["host"])):
        applied.append("LTP_HOST")
    if _flag("port", args) is None and "port" in cfg and _set_if_unset(
            "LTP_PORT", str(int(cfg["port"]))):
        applied.append("LTP_PORT")
    if _flag("log_level", args) is None and not _flag("quiet", args):
        raw = str(cfg.get("log_level", "") or "")
        if raw and _set_if_unset("LTP_LOG_LEVEL", raw):
            applied.append("LTP_LOG_LEVEL")

    up = cfg.get("upstream") or {}
    if _flag_unset(_flag("upstream_url", args)) and up.get("url") and _set_if_unset(
            "LTP_UPSTREAM_URL", str(up["url"]).rstrip("/")):
        applied.append("LTP_UPSTREAM_URL")
    for attr, key, envk in (
        ("backend", "backend", "LTP_BACKEND"),
        ("upstream_api_key", "api_key", "LTP_UPSTREAM_API_KEY"),
        ("upstream_timeout", "timeout", "LTP_UPSTREAM_TIMEOUT"),
    ):
        if _flag_unset(_flag(attr, args)) and up.get(key) and _set_if_unset(
                envk, str(up[key])):
            applied.append(envk)

    mm = None
    if _flag_unset(_flag("model_map", args)) and _flag_unset(
            _flag("model_map_file", args)):
        table = cfg.get("model_map")
        if isinstance(table, dict) and table:
            mm = json.dumps({str(k): str(v) for k, v in table.items()})
        elif up.get("model_map_file"):
            mm = "@file:" + str(up["model_map_file"])
    if mm and _set_if_unset("LTP_MODEL_MAP", mm):
        applied.append("LTP_MODEL_MAP")

    lg = cfg.get("loopguard") or {}
    for attr, key, envk in (
        ("loop_nudge", "nudge", "LTP_LOOP_NUDGE"),
        ("loop_abort", "abort", "LTP_LOOP_ABORT"),
    ):
        if _flag(attr, args) is None and lg.get(key) is not None and _set_if_unset(
                envk, str(int(lg[key]))):
            applied.append(envk)

    tr = cfg.get("trace") or {}
    if not _flag("trace", args) and tr.get("enabled") and _set_if_unset(
            "LTP_TRACE", "1"):
        applied.append("LTP_TRACE")
    if tr.get("file") and not _env_set("LMW_TRACE_FILE"):
        os.environ["LMW_TRACE_FILE"] = str(tr["file"])
        applied.append("LMW_TRACE_FILE")


def _bridge_managed(cfg: dict[str, Any], args: Any,
                    applied: list[str]) -> list[str]:
    """[managed.<name>] → spec strings + LTP_PROCESS_<NAME>_* env.

    Skipped when the CLI already names children via --managed/--backend.
    Returns dev-spec strings for ``parse_managed_specs``.
    """
    managed = cfg.get("managed") or {}
    if not managed or list(getattr(args, "managed", None) or []) or _flag(
            "backend", args):
        return []
    specs: list[str] = []
    for name, t in managed.items():
        if not isinstance(t, dict) or not t.get("command"):
            raise ValueError(f"managed.{name} needs at least a command")
        base = str(t.get("base_url") or "").rstrip("/") or (
            f"http://127.0.0.1:{int(t['port'])}" if t.get("port") else "")
        if not base:
            raise ValueError(
                f"managed.{name}: set base_url or port for the health check")
        specs.append(f"{name}={base}::{t['command']}")
        upper = str(name).upper().replace("-", "_")
        for _key, envk, val in (
            ("base_url", "BASE_URL", base),
            ("port", "PORT", str(int(t["port"])) if t.get("port") else None),
            ("health_path", "HEALTH_PATH",
             str(t["health_path"]) if t.get("health_path") else None),
            ("startup_timeout", "STARTUP_TIMEOUT",
             str(float(t["startup_timeout"])) if t.get("startup_timeout") else None),
            ("shutdown_timeout", "SHUTDOWN_TIMEOUT",
             str(float(t["shutdown_timeout"])) if t.get("shutdown_timeout") else None),
        ):
            if val and not _env_set(f"LTP_PROCESS_{upper}_{envk}"):
                os.environ[f"LTP_PROCESS_{upper}_{envk}"] = val
                applied.append(f"LTP_PROCESS_{upper}_{envk}")
        applied.append(f"managed.{name}")
    return specs


def apply_config(args: Any, *, serve: bool,
                 routing: bool = False) -> tuple[Path | None, dict[str, Any],
                                                 dict[str, Any]]:
    """Bridge the resolved config into env.

    Returns ``(path, cfg, info)`` where ``info`` carries ``applied`` (env keys
    set), ``managed_specs`` (serve only), and the "backend"/"model" values
    from ``[upstream]`` for the managed template.
    """
    path = find_config(getattr(args, "config", None) if args is not None else None)
    cfg = load_config(path)
    info: dict[str, Any] = {"applied": [], "managed_specs": []}
    if not cfg:
        return path, cfg, info
    applied: list[str] = info["applied"]
    _bridge_settings(cfg, args, applied)
    up = cfg.get("upstream") or {}
    info["backend"] = up.get("backend")
    info["model"] = up.get("model")
    if routing:
        _bridge_routing(cfg, args, applied)
    if serve and not getattr(args, "no_managed", False):
        info["managed_specs"] = _bridge_managed(cfg, args, applied)
    return path, cfg, info


def config_help_line() -> str:
    """Short discovery summary for --help text."""
    home = Path.home() / ".config" / "proxytool" / "config.toml"
    return (f"TOML config file (LTP_CONFIG; default search: ./proxytool.toml, "
            f"./.proxytool.toml, {home})")
# ── diagnostics (`proxytool config print|check`) ────────────────────────────

_SEARCH_DETAIL = {
    "LTP_HOST": "host",
    "LTP_PORT": "port",
    "LTP_LOG_LEVEL": "log_level",
    "LTP_UPSTREAM_URL": "upstream.url",
    "LTP_BACKEND": "upstream.backend",
    "LTP_UPSTREAM_API_KEY": "upstream.api_key",
    "LTP_UPSTREAM_TIMEOUT": "upstream.timeout",
    "LTP_MODEL_MAP": "model_map",
    "LTP_LOOP_NUDGE": "loopguard.nudge",
    "LTP_LOOP_ABORT": "loopguard.abort",
    "LTP_TRACE": "trace.enabled",
    "LMW_TRACE_FILE": "trace.file",
}
_PROCESS_KEYS = ("BASE_URL", "PORT", "HEALTH_PATH", "STARTUP_TIMEOUT",
                 "SHUTDOWN_TIMEOUT")


def _search_candidates(explicit: str | None) -> list[Path]:
    """The paths find_config consults, in order (for diagnostic display)."""
    if explicit:
        return [Path(explicit).expanduser()]
    env = os.environ.get("LTP_CONFIG")
    if env:
        return [Path(env).expanduser()]
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    cwd = Path.cwd()
    return [cwd / name for name in CWD_NAMES] + [Path(xdg) / "proxytool" / "config.toml"]


def _detail_for(key: str) -> str:
    """Which config-file key would supply ``env key`` (for sourced rows)."""
    if key in _SEARCH_DETAIL:
        return _SEARCH_DETAIL[key]
    m = re.fullmatch(r"LTP_UPSTREAM_([A-Z0-9_]+)_MODELS", key)
    if m:
        return f"routing.{m.group(1).lower()}.models"
    m = re.fullmatch(r"LTP_UPSTREAM_([A-Z0-9_]+)", key)
    if m:
        return f"routing.{m.group(1).lower()}.url"
    m = re.fullmatch(
        r"LTP_PROCESS_([A-Z0-9_]+)_(?:" + "|".join(_PROCESS_KEYS) + r")", key)
    if m:
        return f"managed.{m.group(1).lower()}.{m.group(2).lower()}"
    return ""


def _relevant_env_keys(keys: Any) -> list[str]:
    """Env vars the bridge could read or fill — diagnostic row candidates."""
    out = []
    for key in keys:
        if key == "LTP_CONFIG":
            continue  # it *selects* the file; shown in the discovery block
        if key.startswith(("LTP_", "LMW_")):
            out.append(key)
    return sorted(out)


def describe_config(args: Any | None, *, routing: bool = True,
                    serve: bool = True) -> dict[str, Any]:
    """Dry-run the config bridge: resolve, validate, attribute — no env edits.

    Returns a JSON-friendly dict with the discovered file, the candidates
    consulted, validation errors when any, and one row per relevant env var
    (LTP_/LMW_) with its value and where it would come from at serve/start
    time: ``config file`` (bridged now) or ``env (pre-existing)`` (already
    set, so the bridge would leave it). Flags are intentionally not shown —
    precedence is CLI flag > env > config file > default, and this
    subcommand takes no flags.
    """
    explicit = getattr(args, "config", None) if args is not None else None
    candidates = [str(p) for p in _search_candidates(explicit)]
    snapshot = dict(os.environ)
    out: dict[str, Any] = {
        "file": None, "searched": candidates, "valid": True, "errors": [],
        "applied": [], "settings": [],
        "precedence": "CLI flag > env var > config file > built-in default",
    }
    in_file = next((c for c in candidates if Path(c).is_file()), None)
    try:
        path = find_config(explicit)
        if path is not None:
            load_config(path)  # validates strictly (unknown keys, types, ...)
        _path, _cfg, _info = apply_config(args, serve=serve, routing=routing)
        changed = {k: v for k, v in os.environ.items() if snapshot.get(k) != v}
    except ValueError as exc:
        out["valid"] = False
        out["errors"] = str(exc).splitlines()
        out["file"] = in_file
        return out
    finally:
        os.environ.clear()
        os.environ.update(snapshot)

    rows: list[dict[str, str]] = []
    for key in sorted(set(changed) | set(_relevant_env_keys(snapshot.keys()))):
        if key in changed:
            src, value = "config file", changed[key]
        else:
            src, value = "env (pre-existing)", snapshot[key]
        detail = _detail_for(key) if src == "config file" else ""
        rows.append({"key": key, "value": value, "source": src, "detail": detail})
    out["file"] = str(path) if path is not None else None
    out["applied"] = sorted(changed)
    out["settings"] = rows
    return out
