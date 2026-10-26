# Development workflow

## PRs & branch protection

`main` is protected on the hosted repo (`github.com/dave-holden/proxytoolpatch`
— remote `origin`). Note it is a **ruleset** (`Settings → Rules → Rulesets`,
named `General`, target `refs/heads/main`), not the older classic branch
protection: the REST path `branches/main/protection` returns 404 here. Changes
land through a PR, and merging requires these three checks from
`.github/workflows/test.yml`:

| Required check | Job |
|---|---|
| `lint` | `lint` |
| `pyright` | `pyright` (type-check on the 3.11 floor) |
| `test-matrix (3.11, proxytool)` | one cell of the `test-matrix` job |

The ruleset does not require branches to be up to date before merging and has
no bypass actors (admins included). Only three checks are listed to keep the
gate short; the rest of the matrix still runs and is still visible on the PR.

**The requirement names the new floor**, so changing the Python matrix means
editing the ruleset too — `test-matrix (3.10, proxytool)` outlived the 3.10 row
and blocked every PR until it was renamed (the check never reports, so it is
not "missing", it is permanently pending).

Recreate these settings if the repo is ever set up again:

1. Create the GitHub repo and add the remote:
   ```bash
   git remote add origin git@github.com:<you>/proxytoolpatch.git
   git push -u origin main
   ```

2. In *Settings → Rules → Rulesets → New branch ruleset*, targeting `main`:

  - Enforcement **Active**, empty bypass list

  - ✅ Require a pull request before merging

  - ✅ Require status checks: `lint`, `pyright`, `test-matrix (3.11, proxytool)`
     (they appear in the picker after the first PR run)

  - ✅ Restrict deletions, ✅ Block force pushes

  - Leave *Require branches to be up to date* **off**: the matrix is slow and
     the required checks are cheap to reason about on a stale base.


3. From then on, work on feature branches and open PRs; `main` is push-protected:
   ```bash
   git checkout -b feat/my-change
   git push -u origin feat/my-change   # opens the PR via the GH URL printed
   ```

## Local quality gates

A local, untracked `dev/scripts/` directory (dev rig, test matrix, the fake
upstream) may exist on your machine — it is per-developer tooling, not part
of either published module. Git hooks are tracked in `.githooks/` (hook shims
plus the quality gate they run). Everything below works from a **fresh
clone**:

```bash
uv run nox -s lint        # ruff
uv run pytest             # full local test suite
uv run nox -s coverage    # branch coverage over both modules (3.11 floor)
FULL_MATRIX=1 git push    # run the whole 3.11–3.14 matrix in the pre-push gate (if wired)
```

The `dev` dependency group includes `httpx2` — since starlette ≥ 1.7 the
`fastapi.testclient` TestClient prefers it, and without it every pytest run
warns via starlette's legacy `httpx` fallback. Plain `httpx` stays the
runtime dependency (the proxy conduit imports it directly).

- **pre-push/pre-commit hooks** (tracked in `.githooks/`, once per clone):
  ```bash
  ./.githooks/install.sh   # sets git config core.hooksPath .githooks
  ```
  Thereafter every commit runs the reduced gate (ruff + default-python
  pytest) and every push runs the same without extra scope.

## Test matrix

```bash
uv run nox -s lint                        # ruff only
uv run nox -s pyright                     # type-check on the 3.11 floor (as CI)
uv run nox -s test                        # full matrix: 2 modules × 3.11–3.14
uv run nox -s coverage                    # branch coverage, local convenience
uv run nox -s 'test_toolpatch(python="3.11")'   # one slice
```

### How to run the full matrix

"Full matrix" = every real compatibility cell: **2 modules (toolpatch,
proxytool) × 4 Pythons (3.11–3.14) = 8 pytest runs**, built as wheels in
throwaway envs (exactly what CI runs).

1. From the repository root, run:

   ```bash
   uv run nox -s test
   ```

2. What to expect:

   - First run is the slow one: uv fetches the 3.11–3.13 interpreters it
     doesn't have yet and builds both workspace packages as wheels per cell
     (`nox reuse_existing_virtualenvs` keeps later runs fast).
   - Each cell prints its own pytest line; a cell failing means nox stops on
     that cell and reports `Session test_* failed` with the traceback.
   - Everything green ends with a `nox > Session test was successful.` line
     and exit code 0.

3. Run just one cell while iterating:

   ```bash
   uv run nox -s 'test_proxytool(python="3.11")'   # the 3.11 floor, most fragile
   uv run nox -s 'test_toolpatch(python="3.14")'   # the newest cell
   ```

4. Extra options (run before/with the matrix, same sessions as CI):

   ```bash
   uv run nox -s lint          # ruff (prereq: keep it green, it is a required check)
   uv run nox -s pyright       # type-check on the 3.11 floor (as CI)
   uv run nox -s coverage      # branch coverage snapshot on the 3.11 floor
   ```

5. Troubleshooting:

   - `InterpreterNotFound` / missing 3.x: let uv fetch it (`uv python install
     3.11 3.12 3.13 3.14`) — nox hands the lookup to uv.
   - A cell that fails only under the matrix (not plain `uv run pytest`):
     suspect wheel-based builds (`uv run --isolated --no-sources`) vs the
     editable install, missing `--group dev`, or a stale uv cache (`uv cache
     clean`).
   - The matrix shells out to the same command CI uses, so if a cell fails
     locally it will fail on CI too — fix it before pushing.

CI (`.github/workflows/test.yml`) runs the same nox sessions directly —
`nox -s lint`, one parametrized `nox -s 'test_<module>(python=…)'` per matrix
cell, and `nox -s pyright` — so **`noxfile.py` is the single source of truth**:
adding a Python version or a session there updates CI automatically. New
misformatting fixtures run everywhere automatically. Every job pins
`runs-on: ubuntu-24.04` rather than `ubuntu-latest`, because `ubuntu-latest` is
being rolled from 24.04 to 26.04 between 19 Oct and 19 Nov 2026 and the matrix
resolves wheels per interpreter/glibc: a mid-window base-image swap could flip a
compatibility cell with no code change. Move to 26.04 in one deliberate PR that
re-runs the whole matrix — Dependabot does not bump `runs-on`.

## Publish flow (both packages)

Tag-driven and test-gated — see `.github/workflows/publish.yml`. Each package
is tagged and published on its own schedule, so their versions may differ:

1. Merge the release PR (tests must be green to merge at all).

2. Tag only what changed, from `main`:

   ```bash
   git tag -a toolpatch-vX.Y.Z -m "toolpatch X.Y.Z: ..." && git push origin toolpatch-vX.Y.Z
   git tag -a proxytool-vX.Y.Z -m "proxytool X.Y.Z: ..." && git push origin proxytool-vX.Y.Z
   ```

   A tag whose version differs from the package's `pyproject.toml` fails the
   `prepare` job before anything is built. The older combined tag `vX.Y.Z`
   still publishes both packages at once.

3. CI: re-runs the whole matrix → builds only the tagged package(s) →
   (rc tags stop at TestPyPI) → publishes to PyPI → smoke-installs the wheel(s)
   in a clean venv and imports them.

4. Publish `toolpatch` before `proxytool` when both moved: proxytool's
   dependency on toolpatch is unpinned, and the proxytool smoke test takes
   toolpatch from PyPI.

5. One-time setup: enable **Trusted Publishing** on PyPI for both projects
   (or add a `PYPI_TOKEN` repo secret) in each project's PyPI *Publishing*
   settings; the workflow uses `uv publish --trusted-publishing always`, so no
   token needs to live in the repo.

6. Rehearse before a real publish when the pipeline changed: dispatch
   `publish.yml` on a branch for a no-index dry-run, or push an `rc` tag to run
   the whole chain against TestPyPI (the `testpypi`/`pypi` environments also
   need a manual approval). Steps and cleanup are in
   [`tools/release/README.md`](tools/release/README.md#rehearsing-a-release-testpypi-only).

## Daemon-style CLI (serve / start / stop / status / health / models)

```bash
proxytool start                     # detached serve; waits for /health
                                    # (fronts your already-running upstream)
proxytool status                    # running / stopped / stale
proxytool health                    # JSON: proxytool + each backend
proxytool models                    # JSON: each upstream's /v1/models
proxytool stop                      # SIGTERM → grace → SIGKILL, clears state
proxytool serve --upstream-url http://127.0.0.1:8181   # foreground (as before)
```

All daemon flags have a TOML config-file equivalent
(`~/.config/proxytool/config.toml` or `./proxytool.toml` — see the app
README's [Configuration](apps/proxytool/README.md#configuration) section and the
annotated `examples/proxytool/proxytool.toml`). With the file in place the CLI
stays minimal — `proxytool start` / `proxytool stop` — while flags/env keep
winning per-key (flag > env > file > default).

State (pid, port, log path) lives in `$PTP_STATE_PATH` or
`/tmp/proxytool-serve-state.json`; the log sits next to it. `stop` only stops
servers started via `start` (foreground `serve` stops with Ctrl-C, as usual).
