#!/usr/bin/env node
/**
 * @file scripts/check-commit-attribution.mjs
 * @description Reject AI-tool attribution in a commit message.
 *
 * GitHub keeps every pull-request head in `refs/pull/*` permanently, so a
 * footer removed by rewriting history stays fetchable by anyone with repository
 * access. Prevention is therefore the only reliable control (see the
 * "Old commit messages inside refs/pull/*" section of
 * `.agents/repo-hardening.md`): this rejects a message as it is written, and CI
 * rejects the tip of a pushed branch.
 *
 * The rule is the one in AGENTS.md: a commit message ends at its own last line.
 * No `Generated with …`, no `🤖`, and no `Co-Authored-By:` / `Signed-off-by:`
 * trailer naming an AI tool. Merge commits are covered by the same rule — a
 * squash-merged PR body lands in the message. An automated service signing off
 * its own commit (Dependabot) is not attribution and is left alone.
 *
 * Usage:
 *   node scripts/check-commit-attribution.mjs <msgfile>      # commit-msg hook
 *   node scripts/check-commit-attribution.mjs --range <rev>  # one commit
 *   node scripts/check-commit-attribution.mjs --range a..b   # a revision range
 *   node scripts/check-commit-attribution.mjs --message "…"  # a message string
 *
 * A bare revision means that commit alone (`HEAD` is one message, not the
 * history behind it); `a..b` is git's range syntax.
 *
 * Exit codes: 0 clean, 1 attribution found, 2 bad usage.
 *
 * Reusing it in another repository — there are no dependencies and no
 * repository-specific paths, so the copy is the whole installation:
 *
 *   cp scripts/check-commit-attribution.mjs <other-repo>/scripts/
 *   printf '#!/bin/sh\nset -e\nnode scripts/check-commit-attribution.mjs "$1"\n' \
 *     > <other-repo>/.git/hooks/commit-msg
 *   chmod +x <other-repo>/.git/hooks/commit-msg
 *
 * and in CI, on the pushed tip: `node scripts/check-commit-attribution.mjs
 * --range HEAD` (or `--range "$BASE..$HEAD"` when full history is checked out).
 */
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

/** One way a message can credit a tool. Each rule is line-scoped on purpose. */
const RULES = [
  {
    id: 'generated-with',
    why: 'a "Generated with …" line',
    test: line => /^\s*(?:\p{Extended_Pictographic}\s*)?generated with\b/iu.test(line)
  },
  {
    id: 'attribution-emoji',
    why: 'an attribution emoji (🤖)',
    test: line => /\u{1F916}/u.test(line)
  },
  {
    id: 'tool-trailer',
    why: 'a trailer naming an AI tool',
    test: (line) => {
      const trailer = /^\s*(?:co-authored-by|signed-off-by)\s*:\s*(.+)$/i.exec(line)
      if (!trailer) return false
      // Named tools only. An automated service signing off its own commit
      // (`Signed-off-by: dependabot[bot] <support@github.com>`) is a dependency
      // bump nobody can edit, not a credit for writing the change.
      return /\b(?:codebuff|freebuff|claude|chatgpt|openai|copilot|cursor|devin|aider|windsurf|gemini|anthropic)\b/i.test(trailer[1] ?? '')
    }
  }
]

const RULE_SUMMARY = 'a message must end at its own last line: no `Generated with …`, no 🤖, no AI-tool trailer'

/**
 * Findings for one message. Comment lines are ignored: git's COMMIT_EDITMSG
 * template is mostly comments, and the scissors line marks anything below the
 * cut as not part of the message.
 */
function check(message) {
  const findings = []
  message.split('\n').forEach((line, index) => {
    if (/^\s*#/.test(line)) return
    for (const rule of RULES) {
      if (rule.test(line)) findings.push({ rule, line: line.trim(), number: index + 1 })
    }
  })
  return findings
}

function fail(...lines) {
  console.error('✕ Commit attribution guard')
  lines.forEach(line => console.error(`  ${line}`))
  console.error('')
  console.error(`  ${RULE_SUMMARY}.`)
  console.error('  See .agents/instructions.md > "Commit messages".')
  console.error('  Drop the line(s), keeping the message as its own last line, and commit again.')
  process.exit(1)
}

function git(...args) {
  const result = spawnSync('git', args, { encoding: 'utf8' })
  if (result.status !== 0) {
    console.error(`✕ Commit attribution guard: git ${args.join(' ')} failed`)
    console.error(result.stderr || result.stdout)
    process.exit(2)
  }
  return result.stdout
}

function report({ label, message }) {
  const findings = check(message)
  if (!findings.length) return
  // `fail` exits, so a finding ends the run: the first message that has one is
  // the one to fix, and a list of every commit would bury the line itself.
  fail(...findings.map(finding => `${label}:${finding.number}  ${finding.rule.why}: ${finding.line}`))
}

const argv = process.argv.slice(2)

if (!argv.length || argv.includes('--help') || argv.includes('-h')) {
  console.error('usage: check-commit-attribution <msgfile> | --range <rev[..rev]> | --message "…"')
  process.exit(argv.length ? 0 : 2)
}

if (argv[0] === '--message') {
  const message = argv[1] ?? ''
  if (!message) {
    console.error('--message needs a value')
    process.exit(2)
  }
  report({ label: 'message', message })
  console.log('✓ Commit attribution guard: no tool attribution found.')
  process.exit(0)
}

if (argv[0] === '--range') {
  const range = argv[1]
  if (!range) {
    console.error('--range needs a revision or range, e.g. v0.5.0..HEAD')
    process.exit(2)
  }
  // Records are `<sha>\x1f<message>\x1e`, so a message may contain anything.
  // `--range HEAD` must mean that commit: `git log HEAD` would walk the whole
  // history behind it.
  const args = ['log', '--no-color']
  if (!range.includes('..')) args.push('-1')
  const raw = git(...args, '--format=%H%x1f%B%x1e', range)
  const records = raw.split('\u001E').map(record => record.replace(/^\n/, '')).filter(Boolean)
  for (const record of records) {
    const separator = record.indexOf('\u001F')
    if (separator < 0) continue
    report({ label: record.slice(0, separator).slice(0, 7), message: record.slice(separator + 1) })
  }
  console.log(`✓ Commit attribution guard: ${records.length} commit message(s) clean.`)
  process.exit(0)
}

const file = argv[0]
let message
try {
  message = readFileSync(file, 'utf8')
} catch (error) {
  console.error(`✕ Commit attribution guard: cannot read ${file} (${error.message})`)
  process.exit(2)
}
report({ label: file, message })
process.exit(0)
