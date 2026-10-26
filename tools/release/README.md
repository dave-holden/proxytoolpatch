# tools/release

Per-package release tooling. Every workspace member (`packages/toolpatch`,
`apps/proxytool`) owns an independent `CHANGELOG.md` inside its own directory,
built by [git-cliff](https://git-cliff.org) from the commits that touch only
that directory, and released under its own namespaced tag
(`toolpatch-v0.4.0`, `proxytool-v0.3.1`).

| File | Purpose |
|---|---|
| `changelog.sh` | Rebuild/inspect a package's `CHANGELOG.md`; also resolves scoped tags (`resolve`) |
| `plan.py` | Compute + apply release candidates (version bumps + pending changelog sections) |
| `../../cliff.toml` | The shared git-cliff configuration (conventional-commit parsing, scope prefixes, commit/PR links) |

Three GitHub Actions wire this up:

- **`.github/workflows/release-pr.yml`** — on pushes to main / weekly cron:
  open or update a `release/next` PR with version bumps + pending changelog
  sections for every package that has unreleased commits.
- **`.github/workflows/publish.yml`** — on a scoped tag: re-run the test
  suite, build only the tagged package(s), upload to TestPyPI then PyPI, and
  smoke-install the wheels in a clean venv. Dispatched by hand on a branch it
  is a dry-run that never touches an index.
- **`.github/workflows/release-package.yml`** — same scoped tag: render that
  tag's section from git history and create the GitHub Release for it. It does
  **not** rewrite `CHANGELOG.md`: `main` is PR-protected, so the released
  sections belong to the release PR.

## Local commands

Run everything from the repository root.

```bash
# What gets released right now (no files touched)
tools/release/plan.py --dry-run
```

### Retrospective rebuild (one package, or all)

```bash
tools/release/changelog.sh rebuild toolpatch
tools/release/changelog.sh all
```

### Pending (untagged) section, labelled with the future tag

```bash
tools/release/changelog.sh unreleased toolpatch toolpatch-v0.5.0
```

### Release notes for one tag (to stdout, or a file)

```bash
tools/release/changelog.sh notes toolpatch toolpatch-v0.4.0
tools/release/changelog.sh notes proxytool proxytool-v0.4.0 -o /tmp/notes.md
```

### Audit the workspace ("<name><TAB><dir><TAB><version>")

```bash
tools/release/changelog.sh list
```

`changelog.sh` uses `git-cliff` from `PATH` when present, otherwise the
pinned release via `uvx` (`GIT_CLIFF_VERSION` overrides the pin). Tokenless
runs generate offline; export `GITHUB_TOKEN` to render the PR-number links
from the GitHub API as well.

## The workflow, end to end

1. Ordinary development: conventional commits (`feat: …`, `fix(scope): …`,
   `!:`/`BREAKING CHANGE` for breaking).

   **The commit the planner reads after a squash merge is the PR title.**
   GitHub offers the PR title as the squash subject, so a branch full of
   conventional commits lands as one unconventional commit if the title is
   prose: `Drop Python 3.10 support` was classified as a *patch* even though
   its branch commit was `feat(proxytool,toolpatch)!: drop Python 3.10 support`.
   Either title the PR after its conventional commit, or pass the subject
   explicitly:

   ```bash
   gh pr merge --squash --subject "feat(proxytool,toolpatch)!: drop Python 3.10 support"
   ```

   The PR body becomes the commit body, so a `BREAKING CHANGE:` footer there
   also marks the release as breaking.
2. `tools/release/plan.py` (or just let `release-pr.yml` do it) bumps versions
   and opens `release/next` — breaking → major, `feat` → minor, everything
   else → patch, per package and independently. While a package is still 0.x a
   breaking change bumps the minor (`0.4.2` → `0.5.0`): 1.0.0 stays a
   deliberate decision.
3. Merge the PR, then push the tags listed in its body (toolpatch first when
   both packages moved — proxytool depends on toolpatch unpinned).
4. Each tag triggers `publish.yml` (TestPyPI → PyPI) and `release-package.yml`
   (GitHub Release) for exactly one package.
5. Rehearse the whole chain on an `rc` tag first when the pipeline itself
   changed — see below.

## Rehearsing a release (TestPyPI only)

An `rc` tag exercises everything downstream of the release PR — tag guard,
build, TestPyPI upload, wheel smoke-install, prerelease GitHub Release — while
`publish-pypi` skips itself (`if: github.ref_type == 'tag' && !contains(
github.ref_name, 'rc')`). Nothing can reach real PyPI, where a version number
can never be reused once uploaded.

The tag version has to equal the declared `pyproject.toml` version *literally*,
and the normalised PEP 440 form is what `uv version` writes (`0.4.2rc1`, not
`0.4.2-rc.1`), so the version needs its own small PR on either side:

```bash
# 1. bump to the rc through a PR (chore(release) commits are skipped by the planner)
uv version --package proxytool 0.4.2rc1 --no-sync

# 2. tag the merged main and let CI run
git tag -a proxytool-v0.4.2rc1 -m "proxytool 0.4.2rc1 (rehearsal)"
git push origin proxytool-v0.4.2rc1

# 3. restore the released version through a second PR, so the planner's next
#    patch bump is 0.4.2 again rather than burning it on 0.4.3
uv version --package proxytool 0.4.1 --no-sync
```

Expect two pauses: the `testpypi` and `pypi` environments are approval-gated, so
the upload job waits for a reviewer, and the run leaves a prerelease on the
repo. Clean up after a rehearsal with
`gh release delete proxytool-v0.4.2rc1 --cleanup-tag`.

For a check that touches no index at all, dispatch `publish.yml` by hand on a
branch: `prepare` reads both declared versions, `build` runs, and both publish
jobs are skipped because they require `ref_type == 'tag'`.

## Notes & gotchas

- `git-cliff` 2.x removed `--path` → use `--include-path` (needs a glob,
  `<dir>/**`), and `--all` → full history is the default. `changelog.sh`
  exists precisely so these flags stay in one place.
- Bump decisions come from `plan.py` (same skip list as `cliff.toml`'s
  `commit_parsers`), so `chore(release)`/`Merge`/`Release: …` commits never
  trigger or appear in a release.
- Packages without a `<name>-v*` tag are skipped by the planner: there is no
  baseline to diff against until the first per-package tag lands.
- Don't run `plan.py` twice without committing — it prepends from the *last
  tag*, so an uncommitted bump makes the next run bump again. Workflows are
  immune (they always start from the default branch).
- The changelog files are generated; edit them through commits, or regenerate
  with `changelog.sh`. Manual edits to an `## [Unreleased]` section survive
  until the next tag regenerates the released sections.
