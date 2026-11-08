#!/usr/bin/env node
/**
 * @file scripts/check-commit-attribution.test.mjs
 * @description Tests for check-commit-attribution.mjs — `node --test scripts/`.
 *
 * Every case runs the real script in a child process, because the exit code is
 * the contract: the hook and the CI job only look at that. The range cases get
 * a throwaway repository, so they exercise real `git log` output (the `-1`
 * handling and the `a..b` walk) rather than a hand-built log.
 */
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { appendFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'check-commit-attribution.mjs')

/** Run the checker the way the hook does: node <script> <args>, cwd inherited. */
function run(args, { cwd } = {}) {
  const result = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8', cwd })
  return { code: result.status, stdout: result.stdout, stderr: result.stderr }
}

function scrub(text) {
  return text.replace(/[^\x20-\x7E\n]/g, '.')
}

function assertClean(args, options) {
  const result = run(args, options)
  assert.equal(result.code, 0, `expected a clean message, stderr:\n${scrub(result.stderr)}`)
  return result
}

function assertBlocked(args, reason, options) {
  const result = run(args, options)
  assert.equal(result.code, 1, `expected exit 1, got ${result.code}:\n${scrub(result.stderr)}`)
  assert.match(result.stderr, new RegExp(reason))
  return result
}

function git(dir, ...args) {
  const result = spawnSync('git', args, { cwd: dir, encoding: 'utf8' })
  if (result.status !== 0) throw new Error(`git ${args.join(' ')} failed: ${result.stderr}`)
  return result.stdout
}

/** A repository with no hooks installed, so commits are not gated by this one. */
function tempRepo(t) {
  const dir = mkdtempSync(join(tmpdir(), 'attribution-guard-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  git(dir, 'init', '-q')
  git(dir, 'config', 'user.name', 'Guard Test')
  git(dir, 'config', 'user.email', 'guard@example.com')
  git(dir, 'config', 'commit.gpgsign', 'false')
  return dir
}

/** Commit `message` in `dir` and return its full sha. */
function commit(dir, message) {
  appendFileSync(join(dir, 'file.txt'), `${message}\n`)
  git(dir, 'add', 'file.txt')
  git(dir, 'commit', '-q', '-m', message)
  return git(dir, 'rev-parse', 'HEAD').trim()
}

function messageFile(t, message) {
  const dir = mkdtempSync(join(tmpdir(), 'attribution-msg-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const file = join(dir, 'COMMIT_EDITMSG')
  writeFileSync(file, message)
  return file
}

// ── Rule coverage, one message at a time ────────────────────────────────────

test('allows a message that ends at its own last line', () => {
  assertClean([
    '--message',
    'fix(repair): stop reporting a native tool call twice\n\n' +
      'The stream handler forwarded the synthetic call and then the parsed one.\n'
  ])
})

test('allows a human co-author trailer', () => {
  assertClean(['--message', 'feat: add routing\n\nCo-Authored-By: Dana Holden <dana@example.com>\n'])
})

test('allows an automated service signing off its own commit', () => {
  assertClean([
    '--message',
    'chore(deps): bump pytest from 9.0 to 9.1\n\n' +
      'Signed-off-by: dependabot[bot] <support@github.com>\n'
  ])
})

test('does not trip over the words in ordinary prose', () => {
  // The rules are line-anchored on purpose: a docs commit describing the footer
  // must still be committable, or the guard is impossible to write about.
  assertClean([
    '--message',
    'docs: explain why a Generated with footer is rejected\n\n' +
      'A `Co-Authored-By:` line naming a tool is attribution too.\n'
  ])
})

test('rejects a "Generated with …" line', () => {
  assertBlocked(
    ['--message', 'feat: add routing\n\nGenerated with Claude Code\n'],
    'a "Generated with …" line'
  )
})

test('rejects a "Generated with …" line carrying a leading emoji', () => {
  assertBlocked(
    ['--message', 'feat: add routing\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n'],
    'an attribution emoji'
  )
})

test('rejects an attribution emoji anywhere in the body', () => {
  assertBlocked(['--message', 'chore: tidy 🤖\n'], 'an attribution emoji')
})

test('rejects a tool trailer regardless of case and tool spelling', () => {
  for (const trailer of [
    'Co-Authored-By: Freebuff <noreply@freebuff.com>',
    'Co-authored-by: Codebuff <noreply@codebuff.com>',
    'signed-off-by: GitHub Copilot <copilot@github.com>'
  ]) {
    assertBlocked(['--message', `feat: add routing\n\n${trailer}\n`], 'a trailer naming an AI tool')
  }
})

// ── COMMIT_EDITMSG shapes ───────────────────────────────────────────────────

test('ignores comment lines, which are most of the commit template', t => {
  const file = messageFile(
    t,
    'feat: add routing\n\n# Generated with Claude Code — a template comment, not the message\n'
  )
  assertClean([file])
})

test('rejects a message file with attribution, naming the file and line', t => {
  const file = messageFile(t, 'feat: add routing\n\nCo-Authored-By: Freebuff <noreply@freebuff.com>\n')
  const result = assertBlocked([file], 'a trailer naming an AI tool')
  assert.match(result.stderr, new RegExp(`${file}:3`))
})

test("ignores git's verbose diff below the scissors line", t => {
  // `git commit -v` appends the diff here and git drops it before recording the
  // message. Without the cut, any diff that adds the emoji blocks the commit.
  const file = messageFile(
    t,
    'docs: document the attribution guard\n\n' +
      '# ------------------------ >8 ------------------------\n' +
      'diff --git a/docs/guards.md b/docs/guards.md\n' +
      '+🤖 Generated with [Claude Code](https://claude.com/claude-code)\n'
  )
  assertClean([file])
})

test('still rejects attribution above the scissors line', t => {
  const file = messageFile(
    t,
    'feat: add routing\n\nGenerated with Claude Code\n\n' +
      '# ------------------------ >8 ------------------------\n+diff\n'
  )
  assertBlocked([file], 'a "Generated with …" line')
})

// ── Revision ranges ─────────────────────────────────────────────────────────

test('a bare revision checks that commit alone', t => {
  const dir = tempRepo(t)
  commit(dir, 'feat: first\n\nGenerated with Claude Code\n')
  // A good tip on top of a bad commit: `git log HEAD` would shame the tip for
  // a commit it did not write, so `--range HEAD` must walk exactly one commit.
  commit(dir, 'feat: second\n')
  const result = assertClean(['--range', 'HEAD'], { cwd: dir })
  assert.match(result.stdout, /1 commit message\(s\) clean/)
})

test('a bare revision reports attribution in that commit', t => {
  const dir = tempRepo(t)
  commit(dir, 'feat: first\n')
  const sha = commit(dir, 'feat: second\n\nCo-Authored-By: Codebuff <noreply@codebuff.com>\n')
  const result = assertBlocked(['--range', 'HEAD'], 'a trailer naming an AI tool', { cwd: dir })
  assert.match(result.stderr, new RegExp(`${sha.slice(0, 7)}:3`))
})

test('a range walks every commit it covers', t => {
  const dir = tempRepo(t)
  const base = commit(dir, 'feat: first\n')
  const bad = commit(dir, 'feat: second\n\n🤖 Generated with Claude Code\n')
  commit(dir, 'feat: third\n')
  assertClean(['--range', `${base}..${base}`], { cwd: dir })
  const result = assertBlocked(['--range', `${base}..HEAD`], 'an attribution emoji', { cwd: dir })
  assert.match(result.stderr, new RegExp(`${bad.slice(0, 7)}`))
})

// ── Usage ───────────────────────────────────────────────────────────────────

test('is a usage error with no arguments', () => {
  const result = run([])
  assert.equal(result.code, 2)
  assert.match(result.stderr, /usage:/)
})

test('explains itself for --help', () => {
  const result = run(['--help'])
  assert.equal(result.code, 0)
  assert.match(result.stderr, /usage:/)
})

test('is a usage error for an option missing its value', () => {
  assert.equal(run(['--message']).code, 2)
  assert.equal(run(['--range']).code, 2)
})

test('is a usage error for a message file that cannot be read', t => {
  const dir = mkdtempSync(join(tmpdir(), 'attribution-missing-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  assert.equal(run([join(dir, 'nope.txt')]).code, 2)
})

test('is a usage error when git cannot resolve the range', t => {
  const dir = tempRepo(t)
  assert.equal(run(['--range', 'no-such-rev'], { cwd: dir }).code, 2)
})
