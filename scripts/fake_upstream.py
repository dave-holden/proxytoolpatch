"""Minimal OpenAI-compatible upstream that always leaks a Hermes tool call.

Used to debug the middleware without loading the ~8GB Qwen model.
Run:  uv run uvicorn scripts.fake_upstream:app --host 127.0.0.1 --port 9999
(assume scripts is importable: run from workspace root with PYTHONPATH=. )
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Build tags from hex to survive any copy/paste mangling.
OPEN_TAG = bytes.fromhex("3c746f6f6c5f63616c6c3e").decode()
CLOSE_TAG = bytes.fromhex("3c2f746f6f6c5f63616c6c3e").decode()
LEAKED = OPEN_TAG + '{"name": "list_dir", "arguments": {"path": "."}}\n' + CLOSE_TAG

app = FastAPI(title="fake leaking upstream")


@app.post("/v1/chat/completions")
async def leak(request: Request):
    body = await request.json()
    if body.get("stream"):
        async def gen() -> AsyncGenerator[bytes, None]:
            for i in range(0, len(LEAKED), 3):
                chunk = LEAKED[i : i + 3]
                event = {"choices": [{"delta": {"content": chunk}}]}
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse(
        {"choices": [{"message": {"role": "assistant", "content": LEAKED}}]}
    )


@app.get("/v1/models")
async def models():
    return JSONResponse({"data": [{"id": "qwen-fake"}]})


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 9999
    uvicorn.run(app, host="127.0.0.1", port=port)
