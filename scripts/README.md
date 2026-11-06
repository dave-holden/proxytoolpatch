# scripts/ — repository guards

Small, dependency-free checks that run locally (git hooks) and in CI. Nothing
here is needed to build or use proxytool; these only police what gets committed.

| Script | Runs | Rejects |
|---|---|---|
| `check-commit-attribution.mjs` | `.githooks/commit-msg` (local), `test.yml` → *commit attribution gate* (CI) | AI-tool attribution in a commit message: `Generated with …`, `🤖`, or a `Co-Authored-By:` / `Signed-off-by:` trailer naming a tool. Dependabot's own sign-off is not attribution and is allowed. |
| `install-git-hooks.mjs` | by hand, once per clone | — sets `core.hooksPath` and makes the `commit-msg` hook executable |

```sh
node scripts/install-git-hooks.mjs          # after cloning
node scripts/check-commit-attribution.mjs --message "feat: x"   # one message
node scripts/check-commit-attribution.mjs --range HEAD          # one commit
node scripts/check-commit-attribution.mjs --range v0.5.0..HEAD  # a range
node --test "scripts/*.test.mjs"            # the guard's own tests
```

Why a guard rather than a convention: GitHub keeps every pull-request head in
`refs/pull/*` permanently, so a footer removed by a later history rewrite stays
fetchable by anyone with repository access. The message must not exist in the
first place. The local hook can be skipped with `git commit --no-verify`; the CI
job cannot.

Node is the only dependency (no packages). The hook fails closed if `node` is
missing rather than skipping the check.

The checker has no dependencies either, so its tests run on node's built-in
runner (`node --test`) — see `check-commit-attribution.test.mjs`, which runs the
real script and asserts on its exit codes, and the *commit attribution gate*
CI job runs them before the check itself.
