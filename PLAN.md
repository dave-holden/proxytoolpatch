# PLAN.md — llama-middleware: Qwen Tool-Calling Template-Mismatch Middleware

> Synthesized from research in `Gemini-c44a-MonoRepo/`, `Gemini-77ec-Tool-Calling-Template-Mismatch/`,
> `Google/`, `Google-MIDDLEWARE-Ai-Agent-Client-And-Chat/`, and `LAM-Middleware-Freebuff/`.
> Target root project: `llama-middleware/` (uv workspace, Python 3.14.2, macOS M1 32GB).

---

## 1. What We Are Building

A **proxy middleware that sits between an agent client (opencode, curl, any OpenAI-compatible
TUI) and a Qwen inference backend (`llama-server` for GGUF, `mlx-lm`/`omlx` server for MLX)**.

```
┌──────────────┐  POST /v1/chat/completions  ┌────────────────┐  same request   ┌───────────────┐
│ Agent client ├────────────:8180───────────►│ this middleware├────:8181───────►│ llama-server  │
│ (opencode)   │                             │ (FastAPI)      │                 │ or mlx server │
└──────────────┘◄────────────────────────────┤  sanitize req. │◄────────────────┤ (Qwen 14B)    │
                repaired OpenAI tool_calls   │  repair resp.  │ raw Qwen output └───────────────┘
                                             └────────────────┘
```

**The defect we repair** (confirmed across all four research sets): when Qwen's chat template is
missing or mismatched, the model emits its tool call as *text inside* `choices[0].message.content`
instead of populating `choices[0].message.tool_calls`. Client therefore prints raw JSON in the TUI
instead of executing the tool. Because both GGUF and MLX servers expose the identical
`/v1/chat/completions` surface, **one middleware serves both backends**; the only backend-specific
difference is outbound request sanitization (`tool_choice` support).

**Repair scope, inbound (response):**
1. Hermes tags → extract → `tool_calls`
2. Fenced JSON blocks (```json … ```) → extract → `tool_calls`
3. Bare `{"name": …, "arguments": …}` JSON inside prose → extract → `tool_calls`
4. Heal malformed argument JSON: trailing commas, unescaped raw newlines inside strings,
   smart quotes, code-fence residue.
5. SSE streaming variant: buffered stream repair that never destroys time-to-first-token for
   non-tool text.

**Guard scope, outbound (request):**
- Validate shape of `tools`, drop empty/invalid entries, warn on suspect tool schemas.
- MLX backend: strip `tool_choice` (`mlx_lm.server` ignores/errs on it). GGUF: pass through.

**Non-goals (YAGNI):** no model execution, no tokenizers, no chat-template rendering of our own,
no multi-backend routing (llama-swap already does that — we happily sit in front of it on 8181).

---

## 2. Architecture & Repo Layout

Monorepo via **uv workspace** (matches `Gemini-c44a-MonoRepo/02-Response.md` plan), virtual root:

```
llama-middleware/
├── pyproject.toml                  # workspace root: dev tooling + shared configs ONLY
├── .python-version                 # 3.14.2 (exists)
├── .gitignore                      # add: .venv/, .idea/, .pytest_cache/, __pycache__/, .ruff_cache/
├── README.md                       # quickstart, ports, pointers to PLAN.md
├── PLAN.md                         # this file
├── RELEASE.md                      # version history log (one entry per release tag)
├── apps/
│   └── proxy/
│       ├── pyproject.toml          # deps: llama-middleware (workspace), fastapi, uvicorn[standard], httpx
│       ├── src/proxy/
│       │   ├── __init__.py
│       │   ├── main.py             # uvicorn entrypoint: app = create_app(); `proxy.main:app`
│       │   ├── conduit.py          # httpx conduit to upstream; forward_request(); SSE piping
│       │   └── trace.py            # config-driven request/response dumper (debug aid)
│       └── tests/
│           ├── conftest.py         # shared fixtures (fake_llama, sample request)
│           ├── test_passthrough.py # non-tool requests byte-pass-through
│           ├── test_repair_httpx.py# tool-leak repair of non-streaming responses
│           └── test_repair_stream.py
└── packages/
    └── llama-qwen/
        ├── pyproject.toml          # deps: pydantic + pytest (test group)
        ├── src/llama_qwen/
        │   ├── __init__.py         # re-export public API: heal_json, extract_tool_calls, StreamHealer, normalize_chat_request
        │   ├── schemas.py          # OpenAI-compatible pydantic models
        │   ├── fixer.py            # JSON healing
        │   ├── extractor.py        # tool-call extraction from content
        │   ├── stream.py           # StreamHealer (SSE)
        │   └── request.py          # outbound normalization
        └── tests/
            ├── unit/               # test_fixer.py, test_extractor.py, test_request.py
            │                       # test_stream.py
            ├── regression/         # test_golden_fixtures.py + fixtures/ dir
            └── fixtures/           # frozen pair files (see Task 6)
```

**Root `pyproject.toml`** (virtual workspace root — dev tooling only, nothing built here):

```toml
[project]
name = "llama-middleware-workspace"
version = "0.0.0"
requires-python = ">=3.14.2"

[tool.uv.workspace]
members = ["apps/*", "packages/*"]

[dependency-groups]
dev = [
    "ruff>=0.14",
    "pyright>=1.1.407",
    "pytest>=9.0",
    "pytest-asyncio>=1.0",
    "httpx>=0.28",
]

[tool.pytest.ini_options]
testpaths = ["apps/proxy/tests", "packages/llama-qwen/tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py314"
src = ["apps/proxy/src", "packages/llama-qwen/src"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ASYNC", "RUF"]

[tool.pyright]
typeCheckingMode = "standard"
include = ["apps/proxy/src", "packages/llama-qwen/src",
           "apps/proxy/tests", "packages/llama-qwen/tests"]
venvPath = "."
venv = ".venv"
```

**`packages/llama-qwen/pyproject.toml`**

```toml
[project]
name = "llama-qwen"
version = "0.1.0"
description = "Qwen tool-calling template-mismatch detector and repairer (pure logic, offline-testable)"
requires-python = ">=3.14.2"
dependencies = ["pydantic>=2.9"]

[dependency-groups]
test = ["pytest>=9.0", "pytest-asyncio>=1.0"]

[build-system]
requires = ["uv_build>=0.12.23,<0.13.0"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "llama_qwen"
```

**`apps/proxy/pyproject.toml`**

```toml
[project]
name = "proxy"
version = "0.1.0"
description = "FastAPI middleware conduit standing between agent clients and Qwen servers"
requires-python = ">=3.14.2"
dependencies = ["llama-qwen", "fastapi>=0.115", "uvicorn[standard]>=0.34", "httpx>=0.28"]

[tool.uv.sources]
llama-qwen = { workspace = true }

[build-system]
requires = ["uv_build>=0.12.23,<0.13.0"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "proxy"
```

**Task 0 bootstrap commands** (run from `llama-middleware/`):

```bash
mkdir -p apps/proxy/src/proxy apps/proxy/tests
mkdir -p packages/llama-qwen/src/llama_qwen packages/llama-qwen/tests/unit packages/llama-qwen/tests/regression/fixtures
mv src/llama_middleware packages/llama-qwen/src/llama_qwen   # rename to import name llama_qwen; then delete empty root src/
# Write the three pyproject.toml files above, then:
uv sync                    # creates ONE root .venv with all members installed editable
uv run pytest              # collects 0 tests, exits clean
```

The old `src/llama_middleware/` directory is moved (and renamed to `llama_qwen`) in Task 1;
the empty root `src/` is deleted in the same task.

---

## 3. Global Conventions

- **Ports:** middleware listens on **8180**; upstream (llama-server / mlx / llama-swap) on **8181**.
  Overridable via env `LMW_UPSTREAM_URL` (default `http://127.0.0.1:8181`).
- **Passthrough-first:** a request with no `tools`, and a response whose content contains no tool
  leak, must emerge byte-equivalent. This is regression-tested (Task 6 fixtures).
- **Sub-millisecond rule:** pure-logic packages must not block; stream repair must be O(n) over
  streamed bytes with bounded buffering (never buffer a whole SSE body).
- **Every task ends with `git commit`.** Unit-test → implement → verify → commit, always.
- Python ≥3.14, pydantic v2, no heavyweight deps in packages (stdlib `json`/`re` only).

---

## 4. Milestones → Tasks

**M1 — Workspace bootstrap** → Task 1
**M2 — Inbound repair engine: JSON healing** → Task 2
**M3 — Extractor: pull tool calls out of leaked content** → Task 3
**M4 — Stream healing for SSE** → Task 4
**M5 — Outbound request normalization** → Task 5
**M6 — Regression harness: golden fixtures** → Task 6
**M7 — Composable public API packaging** → Task 7
**M8 — Proxy app: CRUD routes + conduit + passthrough** → Task 8
**M9 — Proxy app: repair-in-transit (non-streaming & streaming)** → Task 9
**M10 — Debug UX + docs** → Task 10
**M11 — Human E2E validation against real model** → Task 11

---

## Task 1 — Bootstrap uv workspace (M1)

**Files:** root `pyproject.toml` (rewrite), `packages/llama-qwen/pyproject.toml`, `apps/proxy/pyproject.toml`
(board content from §2), directory moves per §2, `.gitignore` extension.

**Acceptance criteria:**

1. `uv sync` completes and creates root `.venv/`.
2. `uv run pytest` exits 0 with 0 tests.
3. `uv run ruff check .` and `uv run pyright` clean.

**Steps:**

- [ ] Rewrite three pyproject.toml files per §2.
- [ ] Move `src/llama_middleware` → `packages/llama-qwen/src/llama_qwen` (rename to the import name `llama_qwen`); delete empty root `src/`.
- [ ] `uv sync`.
- [ ] `uv run pytest || true`, `uv run ruff check .`, `uv run pyright`.
- [ ] `git add -A && git commit -m "chore(workspace): uv workspace with llama-qwen package + proxy app scaffolding"`

---

## Task 2 — JSON heal engine `llama_qwen.fixer` (M2)

Fixes malformed tool-call JSON: trailing commas, unescaped newlines in strings, smart quotes,
stray code fences. stdlib only.

**Files:**
- Create: `packages/llama-qwen/src/llama_qwen/fixer.py`
- Test: `packages/llama-qwen/tests/unit/test_fixer.py`

**Interfaces (later tasks consume these exact signatures):**

```python
def heal_json(text: str) -> str | None: ...
```

**Steps (TDD):**

- [ ] **Failing tests** — `test_fixer.py`:

```python
import json
import pytest
from llama_qwen.fixer import heal_json

def _rt(raw):
    h = heal_json(raw)
    return json.loads(h) if h else pytest.fail(f"no heal for {raw!r}")

def test_valid_json_unchanged():
    assert heal_json('{"a": 1}') == '{"a": 1}'

def test_trailing_comma_obj():
    assert _rt('{"a": 1,}') == {"a": 1}

def test_trailing_comma_nested():
    assert _rt('{"p": {"b": [1,2,],},}') == {"p": {"b": [1, 2]}}

def test_unescaped_newline_in_string():
    raw = '{"cmd": "ls\nm"}'
    assert _rt(raw) == {"cmd": "ls\nm"}

def test_smart_quotes():
    raw = "{\u201cname\u201d: \u201cread_file\u201d}"
    assert _rt(raw) == {"name": "read_file"}

def test_fenced_json():
    assert _rt('```json\n{"name":"ls"}\n```') == {"name": "ls"}

def test_unrecoverable():
    assert heal_json('{"a": ') is None
    assert heal_json('') is None
```

- [ ] `uv run pytest packages/llama-qwen/tests/unit/test_fixer.py -q` → FAIL.
- [ ] **Implement `fixer.py`:**

```python
from __future__ import annotations

import json
import re

_TRAILING_RE = re.compile(r",\s*([}\]])")
_FENCE_START = re.compile(r"^\s*```(?:json)?\s*")
_FENCE_END = re.compile(r"\s*```\s*$")
_SMART = str.maketrans({"\u201c": "\"", "\u201d": "\"", "\u2018": "'", "\u2019": "'"})

def _unescape_stray_newlines_in_strings(s: str) -> str:
    """Escape raw newlines that appear inside JSON string literals."""
    out: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str and not esc and ch == "\n":
            out.append("\\n")
            continue
        out.append(ch)
        if ch == '"' and not esc:
            in_str = not in_str
        esc = in_str and ch == "\\" and not esc
    return "".join(out)

def heal_json(text: str) -> str | None:
    """Return repaired JSON string, or None when unrecoverable."""
    if not text:
        return None
    c = _FENCE_START.sub("", text.strip())
    c = _FENCE_END.sub("", c)
    c = c.translate(_SMART)
    for attempt in (c, _TRAILING_RE.sub(r"\1", c),
                    _TRAILING_RE.sub(r"\1", _unescape_stray_newlines_in_strings(c))):
        try:
            json.loads(attempt)
            return attempt
        except json.JSONDecodeError:
            continue
    return None
```

- [ ] Tests → PASS. Then `uv run ruff check . && uv run pyright`.
- [ ] `git add -A && git commit -m "feat(llama-qwen): JSON heal engine for malformed tool-call payloads"`

---

## Task 3 — Tool-call extractor `llama_qwen.extractor` (M3)

**Files:**
- Create: `packages/llama-qwen/src/llama_qwen/extractor.py`
- Test: `packages/llama-qwen/tests/unit/test_extractor.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ExtractedToolCalls:
    content: str | None                 # cleaned message content (None if nothing left)
    tool_calls: list[dict]              # OpenAI-shaped tool_calls entries
    source: Literal["hermes", "fenced", "raw_json"]

def extract_tool_calls(content: str, tool_names: set[str] | None = None) -> ExtractedToolCalls | None: ...
```

Recognition order (first match wins): `<tool_call>…</tool_call>` (Hermes) → fenced blocks →
bare object. When `tool_names`
provided (we know the request's tools), require the extracted name to be in that set — prevents
extracting the JSON *example* inside a normal answer, or a fenced code sample of a tool call.

**Steps (TDD):** failing unit tests first (spanning both the streaming and non-streaming shapes),
then implementation, verify, then `git add -A && git commit -m "feat(llama-qwen): extract leaked tool calls from content"`.

Test sketch (fill full file at implementation time; current 5 required cases):

```python
from llama_qwen.extractor import extract_tool_calls

def test_hermes_single():
    r = extract_tool_calls('<tool_call>{"name": "list_dir", "arguments": {}}<|im_end|></tool_call>')
    assert r.tool_calls[0]["function"]["name"] == "list_dir"
    assert r.tool_calls[0]["function"]["arguments"] == "{}"
    assert r.content in (None, "")
    assert r.source == "hermes"

def test_fenced_trailing_comma():
    raw = '```json\n{"name": "read_file", "arguments": {"path": "/x",},}\n```'
    r = extract_tool_calls(raw)
    assert r.source == "fenced"
    assert "read_file" in r.tool_calls[0]["function"]["name"]

def test_raw_json_in_prose():
    r = extract_tool_calls('Let me look. {"name":"list_dir","arguments":{"path":"."}} Going now.')
    assert r.source == "raw_json"
    assert "list_dir" in r.tool_calls[0]["function"]["name"]
    assert "Going now." in r.content

def test_tool_name_filter_blocks_prose_json():
    r = extract_tool_calls('{"name":"whatever","arguments":{}}', tool_names={"list_dir"})
    assert r is None

def test_clean_content_passthrough():
    assert extract_tool_calls("No tool here.") is None
```

---

## Task 4 — StreamHealer for SSE (M4)

**Files:**
- Create: `packages/llama-qwen/src/llama_qwen/stream.py`
- Test: `packages/llama-qwen/tests/unit/test_stream.py`

**Interfaces:**

```python
class StreamHealer:
    """Buffers streamed content until a tool-call leak is decided; emits repair events."""

    def feed(self, content_delta: str) -> list[dict]: ...
    def finish(self) -> list[dict]: ...
```

Event shapes (both are plain dicts Consumed by `proxy/conduit.py` _without_ modification):

```python
{"type": "content", "text": str}                 # safe to forward verbatim
{"type": "tool_calls", "tool_calls": [ ...OpenAI tool_calls dicts... ],
 "stop": True}                                   # replace content stream with these
{"type": "content", "text": "", "stop": False}   # collapsed parse-back flush
```

**Buffering rule (bounded):** hold at most `max_buffer=512` chars. Whenever the buffer starts
looking like a fenced `json` block, or a bare `{"name":...}` object forms, decide by running through
`extract_tool_calls`: if a tool call is found, emit `{"type": "tool_calls", ...}`; if exhausted
buffer with no tool-call pattern (by 512 chars or on stream end), flush buffer as content verbatim
and stop holding. When in doubt: `feed` and `finish` return *all* events synthesized so far.

**Steps (TDD):**

- [ ] **Failing tests** — stream a full `<tool_call>call:list_dir {...}<|im_end|>` payload
      across many 3-char chunks (fake SSE ordering) and assert a
      stream of `{"type": "content", ...}` deltas followed by exactly one
      `{"type": "tool_calls", ...}` whose `tool_calls[0].function.name == "list_dir"`.
- [ ] Also stream a **non-tool** long answer (1200 chars, no tool syntax) across 7-char chunks and
      assert events reassemble byte-identically to the input (63 forward events, no tool_calls).
- [ ] Implement → tests pass → ruff + pyright clean.
- [ ] `git commit -m "feat(llama-qwen): StreamHealer heals leaked tool calls inside SSE deltas"`

---

## Task 5 — Outbound request normalization `llama_qwen.request` (M5)

Sanitizes the request body **before** it leaves the proxy so the backend has the best chance of
matching its chat template. Never mutates the caller's payload.

**Files:**
- Create: `packages/llama-qwen/src/llama_qwen/request.py`
- Test: `packages/llama-qwen/tests/unit/test_request.py`

**Interfaces:**

```python
def normalize_chat_request(payload: dict,
                           backend: Literal["gguf", "mlx"] = "gguf") -> tuple[dict, list[str]]:
    """Returns (sanitized_payload, operator_notes).
    Never mutates the input; returns deep copy plus notes for logging."""
```

**Rules:**
1. `tool_choice` only forwarded to GGUF backend — dropped for MLX (`mlx_lm.server` ignores it).
2. `tools=[]` (empty array) or `tools: null`: drop the key entirely.
3. Each `tool.function.parameters` must be a JSON object schema (`type: object` or absent) else
   drop tool + append operator note `"tool "<<name>> parameters not object-schema; dropped"`.
4. Preserve `model`, `messages`, `stream`, and every unknown key verbatim (we are a pass-through
   middleware, not a re-serializer).
5. `stream_options.include_usage` preserved as-is.

**Steps:** TDD: write unit tests forcing each rule; verify each red → green; commit.

---

## Task 6 — Regression harness: golden fixtures (M6)

**What a "regression test" means here:** fixed input pairs (raw Qwen output, expected OpenAI
output) stored as files in the repo; whenever upstream behavior drifts we add a new pair — no code
re-baselines by hand.

**Files:**
- Create: `packages/llama-qwen/tests/regression/test_golden_fixtures.py`
- Create: `packages/llama-qwen/tests/regression/fixtures/` + initial pairs
- Create: `packages/llama-qwen/tests/regression/fixtures/README.md` — how to add a new pair

**Fixture format** — JSON object at `fixtures/<name>.json`:

```json
{
  "kind": "nonstream",              // "nonstream" | "stream"
  "upstream": { "content": "<tool_call>call:list_dir <|im_end|>" },
  "expect": { "content": null,
              "tool_calls": [ { "id": "call_0", "type": "function",
                                "function": { "name": "list_dir", "arguments": "{\"path\": \".\"}" } } ] }
}
```

Initial pairs (write these five real content str examples):

1. `hermes-single` — upstream content is `<tool_call>{"name":"list_dir","arguments":{"path":"."}}<|im_end|>`;
      — the pair expects a normalized OpenAI `tool_calls` entry (not a content leak);
      `content` expected to be null/empty.
2. `hermes-in-prose` — prose prefix + Hermes block; expect cleaned prose + `tool_calls`.
3. `fenced-trailing-comma` — fenced JSON with `,"}` trailing-comma damage; expect healed
      arguments string.
4. `clean-passthrough` — ordinary Qwen answer (no tool syntax); expect byte-equal content, no
      tool_calls. This is the regression guard for "did we mangle a normal answer?"
5. `stream-hermes` — a capture of Qwen *streaming* a Hermes leak across several deltas;
      expected output is the StreamHealer event list.

**Steps:**

- [ ] Write `test_golden_fixtures.py`: `pytest.mark.parametrize` over every `fixtures/*.json`
      file:

```python
import json, pathlib
import pytest
from llama_qwen import extract_tool_calls, StreamHealer   # consumed below

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

def ids():
    return sorted(p.stem for p in FIXTURES.glob("*.json"))

@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=ids())
def test_golden_pair(path):
    case = json.loads(path.read_text())
    if case["kind"] == "stream":
        healer = StreamHealer()
        events: list[dict] = []
        for chunk in case["upstream"]["chunks"]:
            events += healer.feed(chunk)
        events += healer.finish()
        result = {"kind": "stream", "events": events}
    else:
        ex = extract_tool_calls(case["upstream"]["content"])
        result = {"kind": "nonstream",
                  "content": ex.content if ex else case["upstream"]["content"],
                  "tool_calls": ex.tool_calls if ex else []}
    assert result == case["expect"], f"{path.name}: {result} != {case['expect']}"
```

- [ ] Fill the initial 5 fixture files (copy the shapes above from Task 2/3/4 test snippets).
- [ ] Red → implement glue — should be green immediately because Task 2/3/4 code already landed.
- [ ] `git commit -m "test(llama-qwen): regression harness with 5 golden fixture pairs"`

---

## Task 7 — Public API package `llama_qwen/__init__.py` (M7)

**Files:** modify `packages/llama-qwen/src/llama_qwen/__init__.py`

Public re-exports:

```python
from llama_qwen.extractor import ExtractedToolCalls, extract_tool_calls
from llama_qwen.fixer import heal_json
from llama_qwen.request import normalize_chat_request
from llama_qwen.stream import StreamHealer

__all__ = ["ExtractedToolCalls", "StreamHealer", "extract_tool_calls", "heal_json", "normalize_chat_request"]
```

**Steps:**

- [ ] write `packages/llama-qwen/tests/unit/test_public_api.py` re-importing all five symbols
      from `llama_qwen` (catches accidental shadowing via `__init__`).
- [ ] `uv sync`, `uv run pytest`, ruff + pyright.
- [ ] `git commit -m "feat(llama-qwen): public API surface"`

---

## Task 8 — Proxy app: CRUD routes + conduit + passthrough (M8)

**Files:**
- Create: `apps/proxy/src/proxy/__init__.py` (empty)
- Create: `apps/proxy/src/proxy/main.py` (FastAPI app)
- Create: `apps/proxy/src/proxy/conduit.py` (httpx forwarding)
- Test: `apps/proxy/tests/conftest.py` + `apps/proxy/tests/test_passthrough.py`

**Behavior:**

- `POST /v1/chat/completions` — the main route (transit logic lands in Task 9; here it forwards).
- GET `/health` — upstream alive + middleware ready.
- GET `/v1/models` — passes through.
- Every other path/method: full byte pass-through (passthrough-first rule).

**`conduit.py` interface:**

```python
import httpx
from starlette.requests import Request

class UpstreamConduit:
    def __init__(self, base_url: str):
        self._base = base_url
        self._client_opts: dict = {"base_url": base_url, "timeout": httpx.Timeout(300.0)}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(**self._client_opts)

    async def forward(self, request: Request, path: str = "") -> httpx.Response:
        ...
```

**Tests (respx-mock the upstream):**

```python
# apps/proxy/tests/conftest.py
import pytest, httpx, respx, json
from fastapi.testclient import TestClient
from proxy.main import app
from proxy.conduit import UpstreamConduit

@pytest.fixture
def mock_upstream():
    with respx.mock(base_url="http://127.0.0.1:8181") as mock:
        yield mock

@pytest.fixture
def client(mock_upstream, monkeypatch):
    monkeypatch.setenv("LMW_UPSTREAM_URL", "http://127.0.0.1:8181")
    yield TestClient(app)
```

Passthrough test requires: with no `tools` in request and no tool leak in the response, HTTP
status + body + `content-type` must equal upstream byte-for-byte.

Another test asserts `upstream` call got `stream=True` propagated to _both_ `httpx.AsyncClient.send(stream=…)`
and `starlette.responses.StreamingResponse` (so we never buffer) — code-review sensitive; add one
explicit assertion to keep regressions honest.

**Steps:** TDD: tests first (red), then `main.py` + `conduit.py` (green), commit.

---

## Task 9 — Proxy repair-in-transit (M9)

**Files:**
- Modify: `apps/proxy/src/proxy/main.py` (route handler upgrades)
- Modify: `apps/proxy/src/proxy/conduit.py` (streaming buffer plumbing)
- Test: `apps/proxy/tests/test_repair_httpx.py`, `apps/proxy/tests/test_repair_stream.py`

**Non-streaming (`stream=false`) handler sketch:**

```python
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.body()
    payload, notes = normalize_chat_request(json.loads(body), backend=BACKEND)
    if notes:
        log(f"[NOTE] request sanitized: {notes}")
    upstream = await conduit.forward_json("/v1/chat/completions", payload)
    return await repair_upstream_response(upstream)
```

`repair_upstream_response(upstream)`: if `stream` false → `upstream.json()` →
`extract_tool_calls(content, tool_names={names from request tools})` → if extraction hit, rewrite
`choices[*].message.{content, tool_calls}`; else pass through unchanged.

**Streaming (`stream=true`) handler sketch:** re-pipe using `conduit.forward_stream`,
feeding SSE `data:` deltas through `StreamHealer` (see Task 4 events), re-serializing each
chunk. Constant rule: **constant chunks out** — chunk boundaries never need to survive byte-exact;
only the final OpenAI semantic payload does.

**Tests:**

- `test_repair_httpx.py`: upstream returns a Qwen-leak body (fixture #1); client receives
  `tool_calls` populated, `content` cleaned.
- `test_repair_stream.py`: upstream streams a fixed SSE byte stream of a Hermes-leak; client
  receives SSE with one or more repaired tool-call chunks and NO leaked raw JSON.
  Assert `Content-Type: text/event-stream` not rewritten.

**Steps:** TDD → implement → commit.

---

## Task 10 — Debug UX + docs (M10)

**Files:**
- Create: `apps/proxy/src/proxy/trace.py` (env `LMW_TRACE=1` dumps full request/response bodies to
  stdout, idempotent, toggled off by default).
- Modify: root `README.md` (Quickstart §8).
- Create: `RELEASE.md` (v0.1.0 entry).

**Trace behavior:** when `LMW_TRACE=1` origin request is logged verbatim to stderr (pretty JSON);
response is logged before and after repair (with `[REPAIRED]` / `[CLEAN]` prefix).

**Steps:**

- [ ] implement `trace.py` wire-in `main.py`; unit test asserting the env toggle works.
- [ ] `README.md` (§8), `RELEASE.md` scaffold.
- [ ] Run whole suite + ruff + pyright.
- [ ] `git commit -m "feat(proxy): tracing + release docs"`

---

## Task 11 — Live E2E validation (M11) (human step, not agent)

Run **without PyCharm** in three terminals to confirm zero alterations between human-friendly
streams and reality:

```bash
# T1 — model (GGUF; MLX alternative below)
llama-server --host localhost --port 8181 \
  --model $MPATH/qwen2.5-coder-14b-instruct-q4_k_m.gguf \
  --ctx-size 16384 --jinja
#   MLX alternative:
#   python -m mlx_lm.server --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit --port 8181

# T2 — middleware (dev w/ auto-reload)
cd llama-middleware && uv run uvicorn proxy.main:app --host 127.0.0.1 --port 8180 --reload

# T3 — client (curl = cheapest, least-noisy agent client)
LMW_TRACE=1 uv run uvicorn proxy.main:app --host 127.0.0.1 --port 8180   # restart w/ trace if preferred
curl -s http://127.0.0.1:8180/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-coder","stream":false,
       "messages":[{"role":"user","content":"list the files here"}],
       "tools":[{"type":"function","function":{"name":"list_dir","parameters":{"type":"object","properties":{"path":{}}}}}]}' \
  | jq .
```

**Success:** response JSON contains `"tool_calls"` with `"name": "list_dir"`, `content` **does not**
contain an unclosed ````  ````/fence leak, and `uv run pytest` stays green.
Log any *new* upstream misformattings as **new regression fixture pairs** (Task 6).

For opencode on top of the middleware: point `baseURL` to `http://127.0.0.1:8180/v1`
(config snippet lives in the older research files; not re-testable in Task 11 — feature-flag it).

---

## 5. Shared Config (Tooling)

- **Ruff** — set at root (§2): 100-col, py314 target, rule group `[E, F, I, UP, B, SIM, ASYNC, RUF]`.
- **Pyright** — `standard` type-checking, workspace venv; enforced in **pre-push** by a tiny
  `justfile` or Makefile check (`make check`) — users can opt out in IDE but must pass CLI.
- **Pytest** — `asyncio_mode = "auto"`; testpaths under `apps/*` + `packages/*`.
- **uv** — one shared root `.venv` for everything (workspace members resolve as editable).

---

## 6. PyCharm — Run/Debug Instructions

Steps below set up: (1) the right interpreter, (2) Run/Debug configs for the middleware and the
tests, (3) some human-visible breakpoints so you can watch prompts & responses flowing through.

### 6.1 One-time setup

1. Open the **`llama-middleware`** folder (workspace root) as a PyCharm project.
2. `File ▸ Settings ▸ Project: llama-middleware ▸ Python Interpreter ▸ Add Interpreter ▸ Add
   Local Interpreter ▸ Existing` → **`$PROJECT_DIR$/.venv/bin/python`**
   (the uv workspace's shared root venv).
   PyCharm will refresh; verify packages `llama-qwen`, `proxy`, `pydantic`, `fastapi`, `httpx`
   are listed.
3. Confirm the workspace `.venv` resolved - run `uv sync` once outside PyCharm after any
   `pyproject.toml` edit so the venv is fresh (PyCharm does not invoke `uv sync` for you).
4. `Settings ▸ Tools ▸ Python Integrated Tools ▸ Test runner ▸ pytest`.

### 6.2 Run/Debug configuration — "proxy"

`Run ▸ Edit Configurations… ▸ + ▸ Python`

| Field | Value |
|---|---|
| Name | `proxy` |
| Module name | `uvicorn` (pick the "module name" radio, not "script path") |
| Parameters | `proxy.main:app --host 127.0.0.1 --port 8180` |
| Working directory | `$PROJECT_DIR$` (workspace root, so `.venv` + imports resolve) |
| Environment variables | `PYTHONPATH=apps/proxy/src;LMW_UPSTREAM_URL=http://127.0.0.1:8181;LMW_TRACE=1` |
| Before launch | — none needed |

Save two copies: **`proxy`** (needs T1 upstream running, real or fake) and **`proxy (no model)`**
(same, plus `LMW_UPSTREAM_URL=http://127.0.0.1:9999` and using the fake-upstream fixture below).

### 6.3 Debugging techniques for watching prompts/responses

1. **Breakpoints — "content wash" checkpoints.** Put line breakpoints in:
   - `packages/llama-qwen/src/llama_qwen/extractor.py` — inside `extract_tool_calls`, top of
     function: inspect `content` arriving raw from Qwen, then step over to watch the repaired
     `tool_calls` list materialize.
   - `packages/llama-qwen/src/llama_qwen/fixer.py` — inside `heal_json`: inspect the raw broken
     JSON string and its healed result.
   - `apps/proxy/src/proxy/main.py` — in the `chat_completions` route: inspect `body` (what the
     client sent) and the payload we forward upstream (sanitized).
   Use pyCharm's inline *Evaluate Expression* (⌥F8 / Alt+F8) on `json.dumps(x, indent=2)` for
   readable payload inspection.

2. **Watch the trace dumper (no debugger).** Start with `LMW_TRACE=1` and keep `tail -f` on the
   run window (or enable PyCharm's Run panel pinning). You'll see:
   `[CLIENT→]` (raw client request), `[MW→UPSTREAM]` (after `normalize_chat_request`),
   `[UPSTREAM→]` (raw Qwen reply), `[MW→CLIENT]` (post-repair).
   This gives throughput behavior without stopping the mechanism.

3. **`pytest` Run/Debug configs.**
   - Config name `pytest: unit` → Target: `packages/llama-qwen/tests/unit` (directory).
   - Config name `pytest: regression` → Target: `packages/llama-qwen/tests/regression` + custom
     fixture path.
   - Config name `pytest: proxy` → Target: `apps/proxy/tests`.
   - Debug one test: put a breakpoint inside the test body, hit the Debug (bug icon) button.

4. **Debug WITHOUT the model (fake upstream).** Create `scripts/fake_upstream.py`:

```python
"""Minimal OpenAI-compatible upstream that always leaks a Hermes tool call.
Used to debug the middleware without loading the 8GB model."""
import json, uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

@app.post("/v1/chat/completions")
async def leak(request: Request):
    body = await request.json()
    leaked = "<tool_call>call:list_dir " + json.dumps({"path": "."}) + "<|im_end|>"
    if body.get("stream"):
        async def gen():
            for chunk in [leaked[i:i+3] for i in range(0, len(leaked), 3)]:
                yield ("data: " + json.dumps({"choices":[{"delta": {"content": chunk}}]}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return {"choices": [{"message": {"role": "assistant", "content": leaked}}]}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9999)
```

Run config "fake_upstream": Module `uvicorn`, Parameters `scripts.fake_upstream:app --port 9999`,
Working dir `$PROJECT_DIR$`. Then run "proxy (no model)" against it. Set a breakpoint in
`extract_tool_calls` and fire a curl at 8180 - you'll watch the leak hit the extractor.

### 6.4 Recommended debugging loop

1. Start Run config **`fake_upstream`** (if not running a real model).
2. Start Debug config **`proxy (no model)`**; put your breakpoints in Tasks 2-4 modules.
3. In PyCharm's **HTTP Client** scratch file (`Tools ▸ HTTP Client`), or in any terminal:

```http
POST http://127.0.0.1:8180/v1/chat/completions
Content-Type: application/json

{"model":"qwen","stream":false,
 "messages":[{"role":"user","content":"show files"}],
 "tools":[{"type":"function","function":{"name":"list_dir","parameters":{"type":"object","properties":{"path":{}}}}}]}
```

4. Watch each breakpoint hit in sequence. ⌥F8 (Evaluate) to inspect & re-attempt variants live.
5. If real model behavior surprises you: capture raw upstream response via `LMW_TRACE=1` logs,
   add it as a new regression fixture pair (Task 6), re-run `pytest: regression`.

---

## 7. Versioning & Release Process

- **`RELEASE.md`** gets a new entry per release tag:

```markdown
## v<X.Y.Z> — <abbrev-commit-hash> (YYYY-MM-DD)
- <bullet summary of changes>
```

- Bump convention: `patch` = fixes/tests only; `minor` = new feature (e.g., new extraction
  pattern); `major` = breaking API change in `llama_qwen` public surface.
- Bump via `uv version bump patch|minor|major` (root pyproject `version` is informational,
  real versions live in sub-projects: `packages/llama-qwen/pyproject.toml`, `apps/proxy/pyproject.toml`).
- Tag: `git tag vX.Y.Z && git push origin vX.Y.Z` where possible.

---

## 8. README (to be written in Task 10)

One-pager: what/why, 3-terminal rig diagram + ports table, `uv run pytest` /
`uv run ruff check . && uv run pyright` commands, env vars (`LMW_UPSTREAM_URL`, `LMW_TRACE`),
pointer to PLAN.md §6 for PyCharm debugging, and "how to capture a new regression fixture".

---

## 9. Self-Review (spec → plan coverage)

| Requirement | Covered by |
|---|---|
| uv workspace replacing scaffold, `src/` layout | Task 1 |
| Root hosting pyright/ruff/pytest configs used by sub-projects | Task 1 (§2 root pyproject) |
| FastAPI proxy app module `apps/proxy` | Tasks 8–9 (+fakes in §6.3) |
| Core logic package (pure, no sockets) `packages/llama-qwen` | Tasks 2–7 |
| MLX + GGUF support | Task 5 (backend flag), Task 11 (both T1 variants) |
| PyCharm Run/Debug as payloads transit | §6 (full instructions incl. fake upstream) |
| Unit tests | Tasks 2–5, 7–9 (`tests/unit`, `apps/proxy/tests`) |
| Regression tests | Task 6 (golden fixture pairs, captured from real Qwen runs) |
| Streams stay low-overhead (TTFT) | Task 4 bounded buffering + Task 9 test |
| Passthrough-first | Tasks 8 (byte pass-through test), 6 fixture #4/#5 |
| Versioning & RELEASE.md | §7 + Task 10 |

Known deferred (deliberately): llama-swap integration, `vLLM`-style response-format normalization,
MCP tooling — all out of scope per §1 non-goals.
