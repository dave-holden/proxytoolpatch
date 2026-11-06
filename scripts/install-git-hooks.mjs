#!/usr/bin/env node
/**
 * Install the local commit-message guard: the attribution check as a commit-msg
 * hook.
 *
 * This repo keeps its hooks in the tracked `.githooks/` directory and points
 * git at it with `core.hooksPath` (see .githooks/install.sh). That matters here:
 * hooks written to `.git/hooks/` are ignored entirely once `core.hooksPath` is
 * set, so an installer that only writes there would silently do nothing.
 *
 *   node scripts/install-git-hooks.mjs
 *
 * Idempotent — re-run it any time. There is deliberately no package.json in
 * this repo (it is a Python workspace), so the command is plain `node` rather
 * than an npm script.
 *
 * The guard matters because GitHub keeps every pull-request head in
 * `refs/pull/*` permanently: a `Generated with …` footer removed by rewriting
 * history stays fetchable, so a message has to be rejected before it exists.
 */
import { chmodSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))

function git(...args) {
  const result = spawnSync('git', args, { cwd: root, encoding: 'utf8' })
  if (result.status !== 0) {
    console.error(`✕ install-git-hooks: git ${args.join(' ')} failed`)
    console.error(result.stderr || result.stdout)
    process.exit(1)
  }
  return result.stdout.trim()
}

git('rev-parse', '--git-dir')

const hook = join(root, '.githooks', 'commit-msg')
if (!existsSync(hook)) {
  console.error(`✕ install-git-hooks: ${hook} is missing; it is tracked, so restore it from git.`)
  process.exit(1)
}
chmodSync(hook, 0o755)

const configured = spawnSync('git', ['config', '--get', 'core.hooksPath'], { cwd: root, encoding: 'utf8' })
if (configured.stdout.trim() !== '.githooks') git('config', 'core.hooksPath', '.githooks')

console.log('hooks installed: core.hooksPath = .githooks')
console.log('  commit-msg → scripts/check-commit-attribution.mjs (AI-tool attribution)')
console.log('  pre-commit / pre-push → .githooks/pre-push-gate.sh (ruff + pytest)')
