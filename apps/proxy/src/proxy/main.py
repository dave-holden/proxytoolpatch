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

from llama_qwen import StreamHealer, extract_tool_calls, normalize_chat_request
from llama_qwen.request import Backend
from proxy import trace
from proxy.conduit import UpstreamConduit
from proxy.loopguard import ABORT_TEXT, NUDGE_TEXT, LoopGuard
from proxy.settings import get_settings

settings = get_settings()
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

PASS_HEADERS_OUT = ("content-type", "x-request-id")

_UPSTREAM_MODELS: list[str] | None = None  # lazily discovered, cached


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


def _sse_events(raw: bytes) -> list[tuple[str, dict[str, Any] | None]]:
    """Parse an SSE byte chunk into (kind, data-or-None) tuples.

    kind is "data", "comment", "event", or "raw" (unparsed line).
    """
    out: list[tuple[str, dict[str, Any] | None]] = []
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("data: "):
            body = line[6:]
            if body == "[DONE]":
                out.append(("done", None))
                continue
            try:
                out.append(("data", json.loads(body)))
            except json.JSONDecodeError:
                out.append(("raw", None))
        elif line.strip():
            out.append(("raw", None))
    return out


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
        )
    return await _non_streaming(payload=payload, client_headers=
        {k.lower(): v for k, v in request.headers.items()})


async def _non_streaming(payload: dict[str, Any],
                         client_headers: dict[str, str] | None = None):
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
        ex = extract_tool_calls(content, tool_names)
        if ex is None:
            continue
        message["content"] = ex.content
        existing = message.get("tool_calls") or []
        message["tool_calls"] = existing + ex.tool_calls
        choice["message"] = message
        repaired = True
    return repaired


async def _streaming(request: Request, payload: dict[str, Any],
                     client_headers: dict[str, str] | None = None):
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
        meta: dict[str, Any] = {}
        usage: dict[str, Any] | None = None
        upstream_finish: str | None = None
        terminated = False  # a finish_reason has been forwarded downstream
        try:
            async for chunk in upstream.aiter_bytes():
                for kind, data in _sse_events(chunk):
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
