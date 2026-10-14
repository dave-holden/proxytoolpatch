# Development workflow

## PRs & branch protection (why they aren't "on" yet)

This repo currently has **no git remote** — branch protection and PR checks
are GitHub-side settings that need a hosted repo to attach to. Everything on
the CI/hook side is already prepared:

1. Create the GitHub repo and add the remote:
   ```bash
   git remote add origin git@github.com:<you>/proxytoolpatch.git
   git push -u origin main
   ```
2. In *Settings → Branches → Add branch protection rule* for `main`:
   - ✅ Require a pull request before merging
   - ✅ Require status checks: `lint`, `test-matrix (…)`, `pyright`
     (from `.github/workflows/test.yml` — they appear after the first PR run)
   - ✅ Require branches to be up to date before merging
   - ✅ Do not allow bypassing (applies to admins too)
3. From then on, work on feature branches and open PRs; `main` is push-protected:
   ```bash
   git checkout -b feat/my-change
   git push -u origin feat/my-change   # opens the PR via the GH URL printed
   ```

## Local quality gates (active now, remote or not)

```bash
./scripts/install-hooks.sh     # wire pre-commit/pre-push gates into git
```

- **pre-push** (and lighter **pre-commit**) run `ruff` + full local pytest.
- Full 3.10–3.14 matrix on demand: `FULL_MATRIX=1 git push` or
  `./scripts/test-matrix.sh` any time.
- Bypass in a genuine emergency: `git push --no-verify` (the CI gate will
  still report the truth after the push).

## Test matrix

```bash
./scripts/test-matrix.sh                  # all Python versions 3.10–3.14
./scripts/test-matrix.sh 3.11 3.12        # subset
./scripts/test-matrix.sh --module toolpatch  # one module's tests only
./scripts/pyright-matrix.sh               # type-check on the 3.10 floor
```

Same matrix via Nox (`uv run nox` from the repo root — `noxfile.py`):

```bash
uv run nox -s lint                        # ruff only
uv run nox -s pyright                     # type-check on the 3.10 floor (as CI)
uv run nox -s test                        # full matrix: 2 modules × 3.10–3.14
uv run nox -s 'test_toolpatch(python="3.11")'   # one slice
```

CI (`.github/workflows/test.yml`) runs the same nox sessions directly —
`nox -s lint`, one parametrized `nox -s 'test_<module>(python=…)'` per matrix
cell, and `nox -s pyright` — so **`noxfile.py` is the single source of truth**:
adding a Python version or a session there updates CI automatically. New
misformatting fixtures run everywhere automatically.

## Publish flow (library only)

Tag-driven and test-gated — see `.github/workflows/publish.yml`:

1. Merge the release PR (tests must be green to merge at all).
2. `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`
3. CI: re-runs the whole matrix → builds `packages/toolpatch` wheel →
   (rc tags go to TestPyPI) → publishes to PyPI → smoke-installs the wheel in
   a clean venv and imports `toolpatch`.
4. One-time setup: enable **Trusted Publishing** on PyPI for the project
   (or add a `PYPI_TOKEN` repo secret) — see `PUBLISHING.md`.

The app (*proxytool*) is not published; it runs from a workspace checkout.

## Daemon-style CLI (serve / start / stop / status / health / models)

```bash
proxytool start --backend fake      # detached serve; waits for /health
proxytool status                    # running / stopped / stale
proxytool health                    # JSON: middleware + each backend
proxytool models                    # JSON: each upstream's /v1/models
proxytool stop                      # SIGTERM → grace → SIGKILL, clears state
proxytool serve --upstream-url http://127.0.0.1:8181   # foreground (as before)
```

All daemon flags have a TOML config-file equivalent
(`~/.config/proxytool/config.toml` or `./proxytool.toml` — see the app
README's [Config file](apps/proxytool/README.md#config-file) section and the
annotated `config/proxytool-example.toml`). With the file in place the CLI
stays minimal — `proxytool start` / `proxytool stop` — while flags/env keep
winning per-key (flag > env > file > default).

State (pid, port, log path) lives in `$LTP_STATE_PATH` or
`/tmp/proxytool-serve-state.json`; the log sits next to it. `stop` only stops
servers started via `start` (foreground `serve` stops with Ctrl-C, as usual).
