"""FastAPI middleware: sits between the agent client and the Qwen server.

- POST /v1/chat/completions : sanitize the outbound request, repair leaked
  tool calls in the inbound response (both streaming SSE and non-streaming).
- Everything else: byte pass-through.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from llama_qwen import StreamHealer, extract_tool_calls, normalize_chat_request
from proxy import trace
from proxy.conduit import UpstreamConduit
from proxy.settings import get_settings

settings = get_settings()
trace.enable(settings.trace)
conduit = UpstreamConduit(settings.upstream_url)

app = FastAPI(title="llama-middleware proxy")

PASS_HEADERS_OUT = ("content-type", "x-request-id")


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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "upstream": settings.upstream_url}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    trace.log("CLIENT->MW", payload)
    payload, notes = normalize_chat_request(payload, backend="gguf")
    if notes:
        trace.log("MW.NOTES", notes)
    trace.log("MW->UPSTREAM", payload)

    if payload.get("stream"):
        return await _streaming(request=request, payload=payload)
    return await _non_streaming(payload=payload)


async def _non_streaming(payload: dict[str, Any]):
    upstream = await conduit.forward_json("/v1/chat/completions", payload)
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


async def _streaming(request: Request, payload: dict[str, Any]):
    tool_names = _request_tool_names(payload)
    upstream = await conduit.forward_stream("/v1/chat/completions", payload)

    async def gen() -> AsyncGenerator[bytes]:
        healer = StreamHealer(tool_names=tool_names)
        try:
            async for chunk in upstream.aiter_bytes():
                for kind, data in _sse_events(chunk):
                    if kind in ("raw", "done"):
                        continue
                    if data is None:
                        continue
                    delta = (data.get("choices") or [{}])[0].get("delta") or {}
                    text = delta.get("content")
                    if not text:
                        continue
                    for ev in healer.feed(text):
                        yield _event_json(ev)
            for ev in healer.finish():
                yield _event_json(ev)
            yield b"data: [DONE]\n\n"
        finally:
            await upstream.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream", status_code=upstream.status_code
    )


def _event_json(ev: dict[str, Any]) -> bytes:
    """Render one StreamHealer event as an OpenAI-compatible SSE chunk."""
    if ev["type"] == "tool_calls":
        out = {
            "choices": [
                {
                    "delta": {"tool_calls": ev["tool_calls"]},
                    "index": 0,
                    "finish_reason": "tool_calls" if ev.get("stop") else None,
                }
            ]
        }
    else:
        out = {
            "choices": [
                {
                    "delta": {"content": ev.get("text", "")},
                    "index": 0,
                    "finish_reason": None,
                }
            ]
        }
    return f"data: {json.dumps(out, separators=(',', ':'))}\n\n".encode()


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
