#!/usr/bin/env python3
"""Plan (and optionally apply) release candidates for every workspace package.

For each ``apps/*`` and ``packages/*`` member this script:

1. finds the last scoped tag for that package (``<name>-v<version>``),
2. collects the unreleased commits that touch only that package's directory,
3. derives the next version from the Conventional Commit types
   (breaking -> major, ``feat`` -> minor, everything else -> patch),
4. bumps ``pyproject.toml`` with ``uv version --package`` (which also refreshes
   ``uv.lock``), and
5. prepends the pending section to that package's ``CHANGELOG.md``.

It is driven by ``.github/workflows/release-pr.yml`` and can be run locally:

    tools/release/plan.py --dry-run          # show what would happen
    tools/release/plan.py                    # bump + update changelogs
    tools/release/plan.py --output-md out.md --output-json plan.json

Packages without a ``<name>-v*`` tag are reported and skipped: there is no
baseline to diff against yet, so every commit would look unreleased.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
)

CHANGELOG_TOOL = ROOT / "tools" / "release" / "changelog.sh"

# Keep in sync with commit_parsers in cliff.toml: a commit that git-cliff skips
# must not trigger a release either.
SKIP_SUBJECTS = (
    re.compile(r"^\s*$"),
    re.compile(r"^Merge "),
    re.compile(r"^chore\(release\)"),
    re.compile(r"^Release: "),
)
BREAKING_SUBJECT = re.compile(r"^[A-Za-z]+(?:\([^)]*\))?!:")
BREAKING_BODY = re.compile(r"BREAKING[ -]CHANGE")
FEAT_SUBJECT = re.compile(r"^feat(?:\([^)]*\))?!?:")

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")


@dataclass
class Package:
    name: str
    path: str  # repo-relative, e.g. "packages/toolpatch"
    version: str

    @property
    def tag_prefix(self) -> str:
        return f"{self.name}-v"


@dataclass
class Commit:
    subject: str
    body: str = ""


@dataclass
class Plan:
    package: str
    path: str
    current_version: str
    next_version: str
    tag: str
    bump: str
    commits: list[str] = field(default_factory=list)
    changelog: str = ""


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def discover_packages() -> list[Package]:
    packages: list[Package] = []
    for pattern in ("apps/*/pyproject.toml", "packages/*/pyproject.toml"):
        for manifest in sorted(ROOT.glob(pattern)):
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            project = data.get("project") or {}
            name, version = project.get("name"), project.get("version")
            if not name or not version:
                continue
            packages.append(
                Package(
                    name=name,
                    path=str(manifest.parent.relative_to(ROOT)),
                    version=version,
                )
            )
    return packages


def last_tag(package: Package) -> str | None:
    result = subprocess.run(
        [
            "git",
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            f"{package.name}-v[0-9]*",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_skipped(subject: str) -> bool:
    return any(pattern.search(subject) for pattern in SKIP_SUBJECTS)


def unreleased_commits(package: Package, since: str) -> list[Commit]:
    """Commits since the package's last tag that touch the package directory."""
    raw = git(
        "log",
        "--no-merges",
        "--pretty=format:%s%x1f%b%x1e",
        f"{since}..HEAD",
        "--",
        package.path,
    )
    commits: list[Commit] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        subject, _, body = record.partition("\x1f")
        subject = subject.strip()
        if is_skipped(subject):
            continue
        commits.append(Commit(subject=subject, body=body))
    return commits


def bump_level(commits: list[Commit]) -> str:
    if any(
        BREAKING_SUBJECT.match(c.subject) or BREAKING_BODY.search(c.body)
        for c in commits
    ):
        return "major"
    if any(FEAT_SUBJECT.match(c.subject) for c in commits):
        return "minor"
    return "patch"


def next_version(current: str, level: str) -> str:
    match = VERSION_RE.match(current)
    if not match:
        raise SystemExit(f"cannot parse version {current!r} as X.Y.Z")
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def bump_manifest(package: Package, version: str, *, dry_run: bool) -> None:
    command = ["uv", "version", "--package", package.name, version, "--no-sync"]
    if dry_run:
        print(f"  would run: {' '.join(command)}")
        return
    subprocess.run(command, cwd=ROOT, check=True)


def update_changelog(package: Package, tag: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  would run: tools/release/changelog.sh unreleased {package.name} {tag}")
        return
    subprocess.run(
        [str(CHANGELOG_TOOL), "unreleased", package.name, tag],
        cwd=ROOT,
        check=True,
    )


def build_plan(*, dry_run: bool) -> tuple[list[Plan], list[Package]]:
    plans: list[Plan] = []
    untagged: list[Package] = []
    for package in discover_packages():
        since = last_tag(package)
        if since is None:
            untagged.append(package)
            continue
        commits = unreleased_commits(package, since)
        if not commits:
            continue
        level = bump_level(commits)
        version = next_version(package.version, level)
        tag = f"{package.tag_prefix}{version}"
        plans.append(
            Plan(
                package=package.name,
                path=package.path,
                current_version=package.version,
                next_version=version,
                tag=tag,
                bump=level,
                commits=[c.subject for c in commits],
                changelog=f"{package.path}/CHANGELOG.md",
            )
        )
        print(f"{package.name}: {package.version} -> {version} ({level}) [{since}]")
        bump_manifest(package, version, dry_run=dry_run)
        update_changelog(package, tag, dry_run=dry_run)
    for package in untagged:
        print(f"{package.name}: no {package.tag_prefix}* tag yet — skipped")
    plans.sort(key=lambda plan: plan.package)
    return plans, untagged


def render_markdown(plans: list[Plan], untagged: list[Package]) -> str:
    lines: list[str] = ["## Pending releases", ""]
    if not plans:
        lines.append("No package has unreleased changes.")
    else:
        lines += [
            "| Package | Bump | Current | Next | Commits |",
            "| --- | --- | --- | --- | --- |",
        ]
        for plan in plans:
            lines.append(
                f"| `{plan.package}` | {plan.bump} | {plan.current_version} "
                f"| {plan.next_version} | {len(plan.commits)} |"
            )
        lines.append("")
        for plan in plans:
            lines += [
                f"### `{plan.package}` {plan.current_version} → {plan.next_version}",
                "",
                f"Changelog: `{plan.changelog}`",
                "",
                "<details><summary>Unreleased commits</summary>",
                "",
            ]
            lines += [f"- {subject}" for subject in plan.commits]
            lines += ["", "</details>", ""]
        lines += [
            "### Publish after merging",
            "",
            "Merge this PR, then push the tag(s) — each one builds, publishes "
            "to TestPyPI then PyPI, and opens the GitHub release:",
            "",
            "```bash",
        ]
        # toolpatch must be published first when it also moves (proxytool's
        # dependency on it is unpinned).
        ordered = sorted(plans, key=lambda plan: (plan.package != "toolpatch", plan.package))
        lines += [
            f'git tag -a {plan.tag} -m "{plan.package} {plan.next_version}"'
            for plan in ordered
        ]
        lines.append("git push origin " + " ".join(plan.tag for plan in ordered))
        lines += ["```", ""]
        if any(plan.package == "proxytool" for plan in plans):
            lines += [
                "Push `toolpatch-v*` before `proxytool-v*` when both move: "
                "proxytool depends on toolpatch unpinned.",
                "",
            ]
    if untagged:
        lines += ["### Packages without a baseline tag", ""]
        lines += [
            f"- `{pkg.name}` — no `{pkg.tag_prefix}*` tag exists yet, so its "
            "history is not treated as unreleased. Tag a baseline release first."
            for pkg in untagged
        ]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report only, change nothing"
    )
    parser.add_argument("--output-md", help="write the PR body to this path")
    parser.add_argument("--output-json", help="write the plan as JSON to this path")
    parser.add_argument(
        "--github-output",
        help="append changed/title outputs to this file (default $GITHUB_OUTPUT)",
    )
    args = parser.parse_args(argv)

    plans, untagged = build_plan(dry_run=args.dry_run)
    markdown = render_markdown(plans, untagged)

    print("")
    print(markdown)

    if args.output_md:
        Path(args.output_md).write_text(markdown + "\n", encoding="utf-8")
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(
                {
                    "has_changes": bool(plans),
                    "packages": [asdict(plan) for plan in plans],
                    "untagged": [pkg.name for pkg in untagged],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    github_output = args.github_output or _github_output_from_env()
    if github_output:
        title = (
            "chore(release): "
            + ", ".join(f"{p.package} {p.next_version}" for p in plans)
            if plans
            else "chore(release): no package releases pending"
        )
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if plans else 'false'}\n")
            handle.write(f"title={title}\n")

    return 0


def _github_output_from_env() -> str | None:
    import os

    return os.environ.get("GITHUB_OUTPUT")


if __name__ == "__main__":
    sys.exit(main())
