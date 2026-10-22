#!/usr/bin/env bash
# Per-package changelog helpers (git-cliff) for the proxytoolpatch workspace.
#
#   tools/release/changelog.sh list
#   tools/release/changelog.sh rebuild <package>            # full history -> <pkg>/CHANGELOG.md
#   tools/release/changelog.sh all                          # rebuild every package
#   tools/release/changelog.sh unreleased <package> <tag>    # prepend the pending section
#   tools/release/changelog.sh notes <package> <tag> [-o F]  # release-notes body for a tag
#   tools/release/changelog.sh resolve <tag>                 # "<name>\t<dir>\t<version>"
#
# Every package is independent: commits are filtered with --include-path
# <dir>/** and releases with --tag-pattern ^<package>-v[0-9]. git-cliff 2.x
# removed --path (it is --include-path now) and --all (whole history is the
# default), so this script is the single place that knows the flags.
#
# git-cliff itself: uses `git-cliff` from PATH when present (CI installs it via
# uv), otherwise runs the pinned release through `uvx` so a fresh checkout needs
# no global tool install.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

GIT_CLIFF_VERSION="${GIT_CLIFF_VERSION:-2.14.2}"

cliff() {
  local runner=(uvx "git-cliff@${GIT_CLIFF_VERSION}")
  command -v git-cliff >/dev/null 2>&1 && runner=(git-cliff)
  # Without a token git-cliff tries the GitHub API for PR metadata while
  # rendering a tagged range and panics on 401/403 (private repo, rate limit).
  # Offline keeps local runs and tokenless CI deterministic; export
  # GITHUB_TOKEN to get PR numbers in the changelog.
  if [ -z "${GITHUB_TOKEN:-}" ]; then
    "${runner[@]}" --offline "$@"
  else
    "${runner[@]}" "$@"
  fi
}

die() { printf 'changelog.sh: %s\n' "$*" >&2; exit 1; }

# git-cliff's template blank-line handling is uneven: it glues the header to
# the first "## [" heading, leaves stray blank lines between groups and a
# trailing blank line at EOF. Normalize so regenerating never produces
# whitespace-only diffs. (perl ships on both macOS and the ubuntu runners,
# unlike GNU sed -i.)
normalize_changelog() {
  perl -0pi -e '
    s/\A[ \t]*\n+//;                # no leading blank lines
    s/([^\n])\n(## \[)/$1\n\n$2/g;  # blank line before every section heading
    s/\n{3,}/\n\n/g;                # collapse stray blank runs
    s/[ \t]*\n+\z/\n/;              # end the file with a single newline
  ' "$1"
}

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# Read the project name declared in a pyproject.toml (first `name = "..."`).
project_name() {
  sed -n 's/^name = "\(.*\)"$/\1/p' "$1" | head -n1
}

project_version() {
  sed -n 's/^version = "\(.*\)"$/\1/p' "$1" | head -n1
}

# All workspace packages as "<name>\t<dir>" lines.
package_pairs() {
  local f dir name
  for f in "$ROOT"/apps/*/pyproject.toml "$ROOT"/packages/*/pyproject.toml; do
    [ -f "$f" ] || continue
    name="$(project_name "$f")"
    [ -n "$name" ] || die "no project name in ${f#"$ROOT"/}"
    printf '%s\t%s\n' "$name" "${f%/pyproject.toml}"
  done
}

# Resolve a package name (or a scoped tag) to "<name>\t<absolute dir>".
resolve_package() {
  local want="$1" name dir
  while IFS=$'\t' read -r name dir; do
    if [ "$name" = "$want" ]; then
      printf '%s\t%s\n' "$name" "$dir"
      return 0
    fi
  done < <(package_pairs)
  return 1
}

# "<name>\t<dir>\t<version>" for the package a scoped tag refers to.
cmd_resolve() {
  local tag="${1:-}" name dir version declared
  [ -n "$tag" ] || die "resolve needs a tag, e.g. toolpatch-v0.4.0"
  case "$tag" in
    *-v*) ;;
    *) die "tag '$tag' is not scoped as <package>-v<version>" ;;
  esac
  name="${tag%%-v*}"
  version="${tag#*-v}"
  [ -n "$name" ] && [ -n "$version" ] || die "cannot split '$tag' into package and version"

  local pair
  pair="$(resolve_package "$name")" || die "no workspace package named '$name' (looked in apps/*, packages/*)"
  dir="${pair#*$'\t'}"

  declared="$(project_version "$dir/pyproject.toml")"
  if [ "$declared" != "$version" ]; then
    die "tag '$tag' says $version but ${dir#"$ROOT"/}/pyproject.toml declares '${declared}'"
  fi
  printf '%s\t%s\t%s\n' "$name" "${dir#"$ROOT"/}" "$version"
}

tag_pattern_for() {
  printf '^%s-v[0-9]' "$1"
}

# Full history -> <dir>/CHANGELOG.md
cmd_rebuild() {
  local name="$1" pair dir
  pair="$(resolve_package "$name")" || die "unknown package '$name'"
  dir="${pair#*$'\t'}"
  cliff \
    --include-path "${dir#"$ROOT"/}/**" \
    --tag-pattern "$(tag_pattern_for "$name")" \
    -o "$dir/CHANGELOG.md"
  normalize_changelog "$dir/CHANGELOG.md"
  printf 'wrote %s/CHANGELOG.md\n' "${dir#"$ROOT"/}" >&2
}

# Prepend the pending (unreleased) section labelled with the upcoming tag.
cmd_unreleased() {
  local name="$1" tag="$2" pair dir target
  pair="$(resolve_package "$name")" || die "unknown package '$name'"
  dir="${pair#*$'\t'}"
  target="$dir/CHANGELOG.md"
  local section
  section="$(mktemp)"
  cliff \
    --include-path "${dir#"$ROOT"/}/**" \
    --tag-pattern "$(tag_pattern_for "$name")" \
    --unreleased --tag "$tag" --strip header \
    -o "$section"
  # Section body only: no H1/header block, no leading or trailing blank lines
  # (normalize_changelog also collapses template artifacts inside the section).
  perl -0pi -e 's/\A[ \t]*\n+//' "$section"
  normalize_changelog "$section"

  if [ -f "$target" ]; then
    # Insert right before the newest existing "## [...]" heading, keeping the
    # file header, and leave exactly one blank line between sections.
    awk -v sec="$section" '
      BEGIN { inserted = 0 }
      !inserted && /^## \[/ {
        while ((getline line < sec) > 0) print line
        print ""
        inserted = 1
      }
      { print }
      END {
        if (!inserted) {
          print ""
          while ((getline line < sec) > 0) print line
        }
      }
    ' "$target" > "$target.new"
    mv "$target.new" "$target"
  else
    { cat "$section"; printf '\n'; } > "$target"
  fi
  rm -f "$section"
  normalize_changelog "$target"
  printf 'updated %s/CHANGELOG.md (%s)\n' "${dir#"$ROOT"/}" "$tag" >&2
}

# Release-notes body for one tag (no file header, no version heading: the
# GitHub release title already carries the version).
cmd_notes() {
  local name="$1" tag="$2" out="-" pair dir prev range
  shift 2
  while [ $# -gt 0 ]; do
    case "$1" in
      -o|--output) out="$2"; shift 2 ;;
      *) die "unexpected argument '$1' to notes" ;;
    esac
  done
  pair="$(resolve_package "$name")" || die "unknown package '$name'"
  dir="${pair#*$'\t'}"

  prev="$(git describe --tags --abbrev=0 --match "${name}-v[0-9]*" "${tag}^" 2>/dev/null || true)"
  local sha
  if [ -n "$prev" ] && [ "$prev" != "$tag" ]; then
    range=("${prev}..${tag}")
  else
    # No earlier tag for this package: range over everything reachable from
    # the tag. git-cliff takes A..B ranges only, so pass the tag's parent
    # commit id, not the tag name (a bare name fails to parse as an OID).
    sha="$(git rev-parse "${tag}^" 2>/dev/null || true)"
    if [ -n "$sha" ]; then
      range=("${sha}..${tag}")
    else
      # First commit ever — the whole history *is* this release.
      range=()
    fi
  fi

  local rendered
  rendered="$(mktemp)"
  cliff \
    --include-path "${dir#"$ROOT"/}/**" \
    --tag-pattern "$(tag_pattern_for "$name")" \
    --strip header --tag "$tag" "${range[@]+"${range[@]}"}" -o "$rendered"
  # Drop the "## [version] - date" line (the release title carries it) and any
  # leading blank lines; keep the sections so the body mirrors CHANGELOG.md.
  awk 'BEGIN{drop=0; started=0}
       !drop && /^## \[/ {drop=1; next}
       !started && /^[[:space:]]*$/ {next}
       {started=1; print}' \
    "$rendered" > "$rendered.body"
  rm -f "$rendered"

  if [ "$out" = "-" ]; then
    cat "$rendered.body"
  else
    mv "$rendered.body" "$out"
  fi
  rm -f "$rendered.body"
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  list)
    while IFS=$'\t' read -r name dir; do
      printf '%s\t%s\t%s\n' "$name" "${dir#"$ROOT"/}" "$(project_version "$dir/pyproject.toml")"
    done < <(package_pairs)
    ;;
  rebuild)  [ $# -eq 1 ] || die "rebuild takes exactly one package"; cmd_rebuild "$1" ;;
  all)
    while IFS=$'\t' read -r name _dir; do cmd_rebuild "$name"; done < <(package_pairs)
    ;;
  unreleased) [ $# -eq 2 ] || die "unreleased takes <package> <tag>"; cmd_unreleased "$1" "$2" ;;
  notes)    [ $# -ge 2 ] || die "notes takes <package> <tag> [-o file]"; cmd_notes "$@" ;;
  resolve)  [ $# -eq 1 ] || die "resolve takes a scoped tag"; cmd_resolve "$1" ;;
  -h|--help|help) usage 0 ;;
  "") usage 1 ;;
  *) die "unknown command '$cmd' (try --help)" ;;
esac
