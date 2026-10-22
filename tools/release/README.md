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

Two GitHub Actions wire this up:

- **`.github/workflows/release-package.yml`** — on a `*-v*.*.*` tag push:
  rebuild `<pkg>/CHANGELOG.md`, push it to the default branch, create the
  GitHub Release for the tag. (PyPI publishing on the same tag is
  `publish.yml`.)
- **`.github/workflows/release-pr.yml`** — on pushes to main / weekly cron:
  open or update a `release/next` PR with version bumps + pending changelog
  sections for every package that has unreleased commits.

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

### Audit the workspace ("<name><TAB><dir><TAB><version>")```bash
tools/release/changelog.sh list
```

`changelog.sh` uses `git-cliff` from `PATH` when present, otherwise the
pinned release via `uvx` (`GIT_CLIFF_VERSION` overrides the pin). Tokenless
runs generate offline; export `GITHUB_TOKEN` to render the PR-number links
from the GitHub API as well.

## The workflow, end to end

1. Ordinary development: conventional commits (`feat: …`, `fix(scope): …`,
   `!:`/`BREAKING CHANGE` for breaking).
2. `tools/release/plan.py` (or just let `release-pr.yml` do it) bumps versions
   and opens `release/next` — breaking → major, `feat` → minor, everything
   else → patch, per package and independently.
3. Merge the PR, then push the tags listed in its body (toolpatch first when
   both packages moved — proxytool depends on toolpatch unpinned).
4. Each tag triggers `publish.yml` (PyPI) and `release-package.yml`
   (changelog commit + GitHub Release) for exactly one package.

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
