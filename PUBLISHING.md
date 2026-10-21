# PUBLISHING.md

How to publish `toolpatch` and `proxytool` to PyPI.

**Both packages publish from this workspace**, each with its own version and
tag — push `toolpatch-vX.Y.Z` or `proxytool-vX.Y.Z` and
`.github/workflows/publish.yml` builds + uploads only that package, gated on
the full test matrix. The older combined tag `vX.Y.Z` still releases both at
the same version. Because the tags are independent, the two packages do not
have to share a version:

- **`toolpatch`** — the pure-logic repair engine. Import-name dependency-free
  beyond `pydantic`; installable by any Python server needing leak repair.
- **`proxytool`** — the FastAPI proxytool + CLI. Depends on `toolpatch`,
  `fastapi`, `uvicorn`, `httpx`. Its `[project.scripts]` entry-point puts the
  `proxytool` command on the installer's PATH automatically.

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
# 1. Bump only the modules that changed, each by its own Patch/Minor/Major rules
grep -H '^version' packages/toolpatch/pyproject.toml apps/proxytool/pyproject.toml
uv lock                            # keep uv.lock's workspace versions in sync

# 2. Checks green
uv run pytest                      # full suite
uv run ruff check .
uv run pyright
uv run nox -s test                 # full 3.10–3.14 matrix (same sessions as CI)
uv run nox -s coverage             # branch coverage snapshot worth recording

# 3. Build test (both members)
(cd packages/toolpatch && uv build) && (cd apps/proxytool && uv build)
```

### Version bump rules

| Change type                       | Bump | Example                          |
| --------------------------------- | ---- | -------------------------------- |
| Bug fix, no new API               | Patch| `1.0.0` → `1.0.1`                |
| New extractor pattern, new option | Minor| `1.0.0` → `1.1.0`                |
| Breaking change to public API     | Major| `1.0.0` → `2.0.0`                |

New **real-world** misformatting fixtures and their heal rules = **Minor**
(they add capability without removing any).

---

## 3. Name availability — check **before** relying on a name

```bash
# is toolpatch free on PyPI?
curl -s https://pypi.org/pypi/toolpatch/json | head -n 1
# HTTP 200 → taken; 404 → free
```

Name quality criteria for `toolpatch`:

- **Describable:** says what it does (llama + qwen).
- **Caveat:** `toolpatch` strongly implies "for llama.cpp and Qwen" — which is
  exactly what the package is today, but if you ever want to support
  non-Qwen/tool-calling models generically, consider a more general name such
  as **`toolcall-repair`**, **`hermes-heal`**, or **`sse-toolfix`**.
- **Searchable:** lowercase, hyphen friendly (PyPI normalizes `_`/`-`/`.` —
  `toolpatch`, `toolpatch`, `llama.qwen` all resolve to the same project).
- **Unique enough:** avoid a name already in heavy use; test with the curl above.

**Recommendation for v0.x:** keep `toolpatch` (exact + descriptive), and
consider a general rename only before `1.0.0` because that is a breaking
change to imports (`from toolpatch import ...`).

---

## 4. Build

From the repo root:

```bash
# build the package into dist/
cd packages/toolpatch
uv build
ls dist/                   # toolpatch-0.2.0-py3-none-any.whl + .tar.gz
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
# confirm at https://test.pypi.org/project/toolpatch/

# then real PyPI
uvx twine upload --non-interactive dist/*
# confirm at https://pypi.org/project/toolpatch/
```

With keyring instead of a plaintext token:

```bash
export KEYRING_PROVIDER=macos            # on macOS
uvx twine upload dist/*                  # prompts for user/token via keyring
```

`__token__` is the username; the token itself (`pypi-...`) is the password.

---

## 6. Tag the release (per package)

Tags are namespaced per package, so each one can move on its own:

```bash
git tag -a toolpatch-vX.Y.Z -m "toolpatch X.Y.Z: <one-line summary>"
git tag -a proxytool-vX.Y.Z -m "proxytool X.Y.Z: <one-line summary>"
git push origin toolpatch-vX.Y.Z proxytool-vX.Y.Z     # push only what you tag
```

One command per package if you prefer:

```bash
git push origin toolpatch-vX.Y.Z   # publishes toolpatch only
git push origin proxytool-vX.Y.Z   # publishes proxytool only
```

Rules:

- The tag's version must equal the version in that package's `pyproject.toml`;
  CI's `prepare` job fails the run on a mismatch (nothing is uploaded).
- Publish `toolpatch` **before** `proxytool` when both moved: proxytool depends
  on toolpatch unpinned and its smoke test installs toolpatch from PyPI.
- `proxytool-vX.Y.Zrc1` goes to TestPyPI only (any `rc` in the tag skips the
  PyPI job), so the two packages never share a release gate. Write the tag
  exactly as the version appears in `pyproject.toml` (`0.5.0rc1`, not
  `0.5.0-rc1`), or `prepare` rejects it.
- The combined `vX.Y.Z` tag still works and publishes both at one version.
- Deleting a tag that was already pushed is not a rollback: PyPI refuses
  re-uploads of an existing version, so bump instead.

---

## 7. Install & smoke-test the published wheel (critical!)

In a throwaway venv **outside** the workspace:

```bash
cd /tmp && uv venv smoke && source smoke/bin/activate
uv pip install --index-url https://pypi.org/simple toolpatch
python - <<'PY'
import toolpatch
print(toolpatch.__all__)
out, notes = toolpatch.normalize_chat_request(
    {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
)
print(out, notes)
PY
deactivate
```

If that fails, yank or re-upload; do **not** push a broken release to `latest`.

---

## 8. CI automation (already wired)

`.github/workflows/publish.yml` is the canonical path; the manual steps above
stay as the fallback. On a `toolpatch-v*` / `proxytool-v*` / `v*` tag push it:

1. runs the full test matrix (`tests-gate`), then validates the tag version
   against the package's `pyproject.toml` (`prepare`)
2. builds only the tagged package(s) (`build`)
3. uploads to TestPyPI and smoke-installs the wheel(s) in a clean venv
   (`publish-testpypi`; every tag goes here, so rc tags stop at this step)
4. uploads to PyPI and re-runs the smoke install (`publish-pypi`, skipped when
   the tag contains `rc`)

Trusted Publishing is used (`--trusted-publishing always`), so no token is
needed once the PyPI/TestPyPI environments are configured.
