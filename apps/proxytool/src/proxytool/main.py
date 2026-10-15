"""FastAPI middleware: sits between the agent client and the Qwen server.

- POST /v1/chat/completions : sanitize the outbound request, repair leaked
  tool calls in the inbound response (both streaming SSE and non-streaming).
- Everything else: byte pass-through.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import proxytool as _pkg
from proxytool import trace
from proxytool.conduit import UpstreamConduit
from proxytool.loopguard import ABORT_TEXT, NUDGE_TEXT, LoopGuard
from proxytool.settings import get_settings
from toolpatch import StreamHealer, extract_tool_calls, normalize_chat_request
from toolpatch.logging import get_logger
from toolpatch.request import Backend

_log = get_logger("proxytool.main")


def _can_emit(obj: object) -> bool:
    """True only for a configured stdlib logger (no-op loggers drop lines)."""
    import logging as _std_logging

    return obj is not None and isinstance(obj, _std_logging.Logger)


settings = get_settings()
# When the app module is imported without the CLI (e.g. `uvicorn
# proxytool.main:app`), no one has configured app logging yet; apply the
# env-driven default exactly once. A CLI configure call has already set the
# "proxytool" logger level, so this is a no-op then.
if _pkg._logging.getLogger("proxytool").level == _pkg._logging.NOTSET:
    _pkg.configure_logging()
trace.attach_logger(_log)
trace.enable(settings.trace)
conduit = UpstreamConduit(
    settings.upstream_url,
    timeout=settings.upstream_timeout,
    api_key=settings.upstream_api_key,
)
loop_guard = LoopGuard(
    nudge_after=settings.loop_nudge,
    abort_after=settings.loop_abort,
)
app = FastAPI(title="llama-middleware proxy")

if _can_emit(_log):
    _log.debug("middleware initialized: upstream=%s backend=%s",
               settings.upstream_url, settings.backend)

PASS_HEADERS_OUT = ("content-type", "x-request-id")

_UPSTREAM_MODELS: list[str] | None = None  # lazily discovered, cached

# ---- optional multi-backend routing -----------------------------------------
# When LTP_UPSTREAM_<NAME> vars are present, requests are routed per-model
# through BackendRouter; each routed backend gets its own conduit. With no
# LTP_UPSTREAM_* vars, everything flows through the single `conduit` above
# (LMW_UPSTREAM_URL), preserving single-backend behavior byte-for-byte.

from proxytool.routing import BackendRouter, backends_from_env  # noqa: E402

_ROUTER: BackendRouter | None = None
_CONDUITS: dict[str, UpstreamConduit] = {}


def get_router() -> BackendRouter | None:
    """Build the router from env once (LTP_UPSTREAM_*); None = single-backend."""
    global _ROUTER
    if _ROUTER is None:
        declared = backends_from_env()
        if len(declared) <= 1:
            _ROUTER = BackendRouter()  # empty, stays None forever in single mode
        else:
            router = BackendRouter()
            for name, backend in declared.items():
                router.register(backend)
                _CONDUITS[name] = UpstreamConduit(
                    backend.url,
                    timeout=settings.upstream_timeout,
                    api_key=settings.upstream_api_key,
                )
            _ROUTER = router
            trace.log("MW.ROUTER", {n: b.url for n, b in declared.items()})
    return _ROUTER if _ROUTER.backends else None


def conduit_for(backend_name: str | None) -> UpstreamConduit:
    """Conduit for the routed backend, or the default single conduit."""
    if backend_name and backend_name in _CONDUITS:
        return _CONDUITS[backend_name]
    return conduit


def _request_tool_names(payload: dict[str, Any]) -> set[str] | None:
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    names = set()
    for t in tools:
        fn = t.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str):
            names.add(name)
    return names or None


class _SSEParser:
    """Stateful SSE frame parser for chunked upstream streams.

    HTTP chunks are *not* guaranteed to end on an SSE frame boundary: a
    `data:` JSON payload can be split across two ``aiter_bytes()`` chunks,
    and a multibyte UTF-8 character can be split mid-sequence. Bytes are
    accumulated until a complete ``\n``-terminated line exists and only
    whole lines are decoded, so both cases resolve before ``json.loads``
    ever sees a payload (”data events carry the none-None frame dict).
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[tuple[str, dict[str, Any] | None]]:
        out: list[tuple[str, dict[str, Any] | None]] = []
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            out.append(_sse_line_event(line))
        return out

    def finish(self) -> list[tuple[str, dict[str, Any] | None]]:
        """Drain a trailing unterminated line (tolerates sloppy servers)."""
        if not self._buf:
            return []
        line, self._buf = self._buf, b""
        return [_sse_line_event(line)]


def _sse_line_event(line_bytes: bytes) -> tuple[str, dict[str, Any] | None]:
    """Parse one complete SSE line into a (kind, data-or-None) tuple."""
    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
    body = None
    if line.startswith("data: "):
        body = line[6:]
    elif line.startswith("data:"):
        body = line[5:].lstrip()
    if body is None:
        return ("raw" if line.strip() else "comment", None)
    if body == "[DONE]":
        return ("done", None)
    try:
        return ("data", json.loads(body))
    except json.JSONDecodeError:
        return ("raw", None)


async def _resolve_model(model: str) -> str:
    """Map a client-facing model id to one the upstream actually serves.

    Order: LMW_MODEL_MAP → upstream /v1/models membership → single loaded
    model. mlx_lm.server resolves unknown model ids against HF (401/404
    "Repository Not Found"), so an unmapped alias must never be forwarded
    raw when the real id is discoverable.
    """
    mapped = settings.model_map.get(model)
    if mapped:
        return mapped
    global _UPSTREAM_MODELS
    if _UPSTREAM_MODELS is None:
        try:
            upstream = await conduit.passthrough("GET", "v1/models", {}, b"")
            data = upstream.json().get("data", [])
            _UPSTREAM_MODELS = [m.get("id", "") for m in data if isinstance(m, dict)]
        except Exception:
            _UPSTREAM_MODELS = []  # unreachable/mock — keep alias as-is
        trace.log("MW.MODELS", _UPSTREAM_MODELS)
    known = [m for m in _UPSTREAM_MODELS if m]
    if model in known:
        return model
    best = _pick_by_alias(model, known)
    return best or model


def _pick_by_alias(alias: str, known: list[str]) -> str | None:
    """Best upstream id for an unmapped alias, or None to pass through.

    Scores each upstream id by token overlap with the alias (e.g.
    ``qwen-mlx`` → tokens {qwen, mlx}). Ties prefer the artifact the server
    was actually launched with — a filesystem path beats an HF repo id,
    which matters when the cache contains several Qwen mlx snapshots.
    """
    tokens = {t for t in re.split(r"[-/_ .]", alias.lower()) if t}
    if not tokens:
        return None
    scored: list[tuple[int, int, str]] = []
    for mid in known:
        low = mid.lower()
        overlap = sum(1 for t in tokens if t in re.split(r"[-/_ .]", low))
        if overlap == 0:
            continue
        path_like = 1 if (mid.startswith("/") or "snapshots/" in mid) else 0
        scored.append((overlap, path_like, mid))
    if not scored:
        return None
    scored.sort(reverse=True)
    top_overlap, top_path, _ = scored[0]
    winners = [s for s in scored if (s[0], s[1]) == (top_overlap, top_path)]
    if len(winners) != 1:
        return None  # genuinely ambiguous — don't guess
    return winners[0][2]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "upstream": settings.upstream_url}


def _resolve_backend(payload: dict[str, Any]) -> Backend:
    """Backend decision: payload heuristics first, else LMW_BACKEND env (gguf default).

    A model id mentioning mlx, or an explicit mlx-community/HF repo id,
    implies mlx; anything else defers to settings.backend.
    """
    model = str(payload.get("model") or "")
    if "mlx" in model.lower() or model.startswith("mlx-community/"):
        return "mlx"
    resolved: Backend = "mlx" if settings.backend == "mlx" else "gguf"
    return resolved


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    trace.log("CLIENT->MW", payload)
    # Route to a multi-backend upstream by the client's model id (no-op in
    # single-backend mode). Routing happens before sanitization because the
    # sanitized payload's model may have been remapped by LMW_MODEL_MAP.
    chat_conduit = conduit
    router = get_router()
    if router is not None:
        client_model = str(payload.get("model") or "")
        decision = router.route(client_model)
        chat_conduit = conduit_for(decision.backend.name)
        trace.log("MW.ROUTE", {"model": client_model,
                               "backend": decision.backend.name,
                               "matched_by": decision.matched_by})
    backend = _resolve_backend(payload)
    payload, notes = normalize_chat_request(payload, backend=backend)
    if backend == "mlx":
        payload.pop("tool_choice", None)
        notes.append("tool_choice stripped (mlx backend)")
    # Remap the client-facing model id to what the upstream actually serves.
    # mlx_lm.server resolves unknown model ids against HF ("Repository Not
    # Found"); llama-server ignores names, but remapping helps logs too.
    model = str(payload.get("model") or "qwen")
    payload["model"] = await _resolve_model(model)
    if notes:
        trace.log("MW.NOTES", notes)
    trace.log("MW->UPSTREAM", payload)

    # LoopGuard: identical consecutive prompts make llama-server replay the
    # same continuation forever. Nudge the model, then cut the loop off.
    decision = loop_guard.check(LoopGuard.request_key(payload))
    trace.log("MW.GUARD", {"action": decision.action, "count": decision.count})
    if decision.action == "abort":
        return _loop_guard_abort(decision.count, stream=bool(payload.get("stream")))
    if decision.action == "nudge":
        payload["messages"] = [
            *payload["messages"],
            {"role": "user", "content": NUDGE_TEXT},
        ]
        trace.log("MW->UPSTREAM", payload)

    if payload.get("stream"):
        return await _streaming(
            request=request, payload=payload,
            client_headers={k.lower(): v for k, v in request.headers.items()},
            conduit=chat_conduit,
        )
    return await _non_streaming(payload=payload, client_headers=
        {k.lower(): v for k, v in request.headers.items()}, conduit=chat_conduit)


async def _non_streaming(payload: dict[str, Any],
                         client_headers: dict[str, str] | None = None,
                         conduit: UpstreamConduit | None = None):
    conduit = conduit or globals()["conduit"]
    assert conduit is not None
    upstream = await conduit.forward_json(
        "/v1/chat/completions", payload, headers=client_headers
    )
    if upstream.status_code >= 400:
        trace.log("UPSTREAM->MW", {"error": upstream.text[:500]})
        return JSONResponse(
            {"error": {"message": f"upstream error (HTTP {upstream.status_code}): "
                       f"{upstream.text[:500]}",
                       "type": "upstream_error",
                       "code": upstream.status_code}},
            status_code=upstream.status_code,
        )
    try:
        data = upstream.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _as_response(upstream, upstream.content)

    trace.log("UPSTREAM->MW", data)
    tool_names = _request_tool_names(payload)
    _repair_response(data, tool_names)
    trace.log("MW->CLIENT", data)
    return JSONResponse(content=data, status_code=upstream.status_code)


def _repair_response(data: dict[str, Any], tool_names: set[str] | None) -> bool:
    """Rewrite leaked tool calls inside data in place. True if repaired."""
    repaired = False
    for choice in data.get("choices", []) or []:
        message = choice.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        ex = extract_tool_calls(content, tool_names, allow_unterminated_hermes=True)
        if ex is None:
            continue
        message["content"] = ex.content
        existing = message.get("tool_calls") or []
        message["tool_calls"] = existing + ex.tool_calls
        choice["message"] = message
        repaired = True
    return repaired


async def _streaming(request: Request, payload: dict[str, Any],
                     client_headers: dict[str, str] | None = None,
                     conduit: UpstreamConduit | None = None):
    conduit = conduit or globals()["conduit"]
    assert conduit is not None
    tool_names = _request_tool_names(payload)
    upstream = await conduit.forward_stream(
        "/v1/chat/completions", payload, headers=client_headers
    )
    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        return _upstream_error_sse(upstream.status_code, body)

    async def gen() -> AsyncGenerator[bytes]:
        healer = StreamHealer(tool_names=tool_names)
        sse = _SSEParser()  # reassembles frames split across HTTP chunks
        meta: dict[str, Any] = {}
        usage: dict[str, Any] | None = None
        upstream_finish: str | None = None
        terminated = False  # a finish_reason has been forwarded downstream
        try:
            async for chunk in upstream.aiter_bytes():
                for kind, data in sse.feed(chunk):
                    if kind in ("raw", "done"):
                        continue
                    if data is None:
                        continue
                    if not meta:
                        # Forward id/created/model so clients correlating
                        # chunks to the request don't see a different object.
                        meta = {
                            k: data[k]
                            for k in ("id", "created", "model", "system_fingerprint")
                            if k in data
                        }
                    if data.get("usage"):
                        usage = data["usage"]
                    choice = (data.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        upstream_finish = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if not text:
                        continue
                    for ev in healer.feed(text):
                        if ev["type"] == "tool_calls" and ev.get("stop"):
                            yield _event_json(ev, meta, "tool_calls", usage)
                            terminated = True
                        else:
                            yield _event_json(ev, meta)
            for ev in healer.finish():
                if ev["type"] == "tool_calls" and ev.get("stop"):
                    yield _event_json(ev, meta, "tool_calls", usage)
                    terminated = True
                else:
                    yield _event_json(ev, meta)
            if not terminated:
                # Repaired/replayed streams still need OpenAI-protocol
                # termination: without a finish_reason chunk, opencode treats
                # every response as an unterminated turn and re-prompts.
                yield _event_json({"type": "control"}, meta, upstream_finish or "stop", usage)
            yield b"data: [DONE]\n\n"
        finally:
            await upstream.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream", status_code=upstream.status_code
    )


def _event_json(
    ev: dict[str, Any],
    meta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> bytes:
    """Render one StreamHealer event as an OpenAI-compatible SSE chunk."""
    if ev["type"] == "tool_calls":
        delta: dict[str, Any] = {"tool_calls": ev["tool_calls"]}
    elif ev["type"] == "content":
        delta = {"content": ev.get("text", "")}
    else:
        delta = {}
    out: dict[str, Any] = {
        **meta,
        "choices": [
            {
                "delta": delta,
                "index": 0,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        out["usage"] = usage
    return f"data: {json.dumps(out, separators=(',', ':'))}\n\n".encode()


def _upstream_error_sse(status: int, body: bytes) -> StreamingResponse:
    """Convert an upstream error into a single clean OpenAI-style SSE stream.

    Without this, error pages / SSE keepalives from the upstream leak raw into
    the client (opencode renders the garbled text or hangs waiting for deltas).
    """
    detail = body.decode("utf-8", errors="replace").strip()[:500]
    msg = f"upstream error (HTTP {status}): {detail}"
    chunk = {
        "choices": [
            {
                "delta": {"content": msg},
                "index": 0,
                "finish_reason": "stop",
            }
        ]
    }
    payload = (
        b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\ndata: [DONE]\n\n"
    )
    return StreamingResponse(
        iter([payload]),
        media_type="text/event-stream",
        status_code=200,
    )


def _loop_guard_abort(count: int, stream: bool) -> Response | StreamingResponse:
    """Return a clean, short completion explaining the cut-off."""
    text = ABORT_TEXT.format(count=count)
    created = int(time.time())
    if stream:
        first = {
            "id": "loopguard-abort",
            "created": created,
            "model": "loopguard",
            "choices": [
                {"delta": {"role": "assistant", "content": text},
                 "index": 0, "finish_reason": None}
            ],
        }
        last = {
            "id": "loopguard-abort",
            "created": created,
            "model": "loopguard",
            "choices": [
                {"delta": {}, "index": 0, "finish_reason": "stop"}
            ],
        }
        sse = "".join(
            f"data: {json.dumps(c, separators=(',', ':'))}\n\n" for c in (first, last)
        ) + "data: [DONE]\n\n"
        return StreamingResponse(
            iter([sse.encode()]), media_type="text/event-stream", status_code=200
        )
    return JSONResponse(
        {
            "id": "loopguard-abort",
            "created": created,
            "model": "loopguard",
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "index": 0,
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
        status_code=200,
    )


@app.get("/v1/models")
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def passthrough(request: Request, path: str = "v1/models"):
    content = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items() if k.lower() != "host"}
    try:
        upstream = await conduit.passthrough(request.method, path, headers, content)
    except httpx.ConnectError:
        return JSONResponse({"error": f"upstream unreachable: {settings.upstream_url}"},
                            status_code=502)
    return _as_response(upstream, upstream.content)


def _as_response(upstream: httpx.Response, content: bytes) -> Response:
    headers = {}
    for h in PASS_HEADERS_OUT:
        if h in upstream.headers:
            headers[h] = upstream.headers[h]
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )
