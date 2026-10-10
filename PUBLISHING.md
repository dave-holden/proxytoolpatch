# PUBLISHING.md

How to publish `llama-qwen` to PyPI (and optionally the `proxy` app privately).

**You publish ONE package: `llama-qwen`** (the pure-logic repair engine).
`proxy` is the application wrapper — typically run from source via `uv run`,
not installed from PyPI.

---

## 1. Prerequisites

```bash
# one-time PyPI registration
uv tool install keyring          # optional but recommended, avoids plaintext tokens
pip install build twine          # or use `uvx twine`
```

Create a PyPI account and an API token at <https://pypi.org/manage/account/token/>
(token-scoped to "upload" and, later, the specific project).

---

## 2. Pre-publish checklist (every release)

```bash
# 1. Versions bumped in ALL relevant pyproject.toml files (Patch/Minor/Major rules below)
grep -H '^version' packages/llama-qwen/pyproject.toml apps/proxy/pyproject.toml

# 2. RELEASE.md has a new section: "## vX.Y.Z (YYYY-MM-DD)" + bullet changes
# 3. Checks green
uv run pytest                      # full suite
uv run ruff check .
uv run pyright

# 4. Build test
uv run build -- Wheel ./
```

### Version bump rules

| Change type                       | Bumb | Example                          |
| --------------------------------- | ---- | -------------------------------- |
| Bug fix, no new API               | Patch| `0.2.0` → `0.2.1`                |
| New extractor pattern, new option | Minor| `0.2.0` → `0.3.0`                |
| Breaking change to public API     | Major| `0.x.0` → `1.0.0`                |

New **real-world** misformatting fixtures and their heal rules = **Minor**
(they add capability without removing any).

---

## 3. Name availability — check **before** relying on a name

```bash
# is llama-qwen free on PyPI?
curl -s https://pypi.org/pypi/llama-qwen/json | head -n 1
# HTTP 200 → taken; 404 → free
```

Name quality criteria for `llama-qwen`:

- **Describable:** says what it does (llama + qwen).
- **Caveat:** `llama-qwen` strongly implies "for llama.cpp and Qwen" — which is
  exactly what the package is today, but if you ever want to support
  non-Qwen/tool-calling models generically, consider a more general name such
  as **`toolcall-repair`**, **`hermes-heal`**, or **`sse-toolfix`**.
- **Searchable:** lowercase, hyphen friendly (PyPI normalizes `_`/`-`/`.` —
  `llama_qwen`, `llama-qwen`, `llama.qwen` all resolve to the same project).
- **Unique enough:** avoid a name already in heavy use; test with the curl above.

**Recommendation for v0.x:** keep `llama-qwen` (exact + descriptive), and
consider a general rename only before `1.0.0` because that is a breaking
change to imports (`from llama_qwen import ...`).

---

## 4. Build

From the repo root:

```bash
# build the package into dist/
cd packages/llama-qwen
uv build
ls dist/                   # llama_qwen-0.2.0-py3-none-any.whl + .tar.gz
```

Inspect the wheel contents (verify README, LICENSE included):

```bash
unzip -l dist/*.whl
```

---

## 5. Publish

```bash
# test first on TestPyPI (safe, throws away nothing)
uv build
uvx twine upload --repository testpypi --non-interactive dist/*
# confirm at https://test.pypi.org/project/llama-qwen/

# then real PyPI
uvx twine upload --non-interactive dist/*
# confirm at https://pypi.org/project/llama-qwen/
```

With keyring instead of a plaintext token:

```bash
export KEYRING_PROVIDER=macos            # on macOS
uvx twine upload dist/*                  # prompts for user/token via keyring
```

`__token__` is the username; the token itself (`pypi-...`) is the password.

---

## 6. Tag the release

```bash
git tag -a vX.Y.Z -m "vX.Y.Z: <one-line summary>"
# if a remote exists:
git push origin vX.Y.Z
```

Match this to the RELEASE.md section just added (version number, no hash).

---

## 7. Install & smoke-test the published wheel (critical!)

In a throwaway venv **outside** the workspace:

```bash
cd /tmp && uv venv smoke && source smoke/bin/activate
uv pip install --index-url https://pypi.org/simple llama-qwen
python - <<'PY'
import llama_qwen
print(llama_qwen.__all__)
out, notes = llama_qwen.normalize_chat_request(
    {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
)
print(out, notes)
PY
deactivate
```

If that fails, yank or re-upload; do **not** push a broken release to `latest`.

---

## 8. Automate later (optional)

When you set up a remote, add a GitHub Actions job that:

1. runs pytest/ruff/pyright
2. builds on `git tag vX.Y.Z`
3. does `twine upload` with `PYPI_TOKEN` from repo secrets

Tools like [`hatch`](https://hatch.pypa.io/) or `uv publish` can simplify
steps 4–5 once CI exists; the manual steps above are the canonical fallback.
