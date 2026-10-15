"""Nox config for the proxytoolpatch workspace — run from the repo root.

    uv run nox                       # default: lint + full matrix (2 modules x 5 Pythons)
    uv run nox -s test               # the matrix only
    uv run nox -s lint               # ruff
    uv run nox -s pyright            # type-check on the 3.10 floor (as CI does)
    uv run nox -s 'test_toolpatch(python="3.11")'   # one module, one Python
    uv run nox -s 'test_proxytool(python="3.14")'

Per-version test sessions intentionally shell out to the *exact* command CI
uses (``uv run --python X --isolated --all-packages --no-sources --group dev
pytest``): uv builds the workspace members as WHEELS under the target
interpreter in a throwaway env, so the result is the real compatibility test —
not an editable-install shortcut. That also means nox never needs to pip-build
the workspace packages itself, and the uv interpreter/caches are shared with
`scripts/test-matrix.sh` and CI.
"""

from __future__ import annotations

import nox

PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")

nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True
nox.options.sessions = ("lint", "test")

TEST_DIRS = {
    "test_toolpatch": "packages/toolpatch/tests",
    "test_proxytool": "apps/proxytool/tests",
}


def _uv_pytest(session: nox.Session, python: str, tests_dir: str) -> None:
    """Run pytest for ``tests_dir`` under an isolated uv env for one Python."""
    session.run(
        "uv", "run", "--python", python, "--isolated",
        "--all-packages", "--no-sources", "--group", "dev",
        "pytest", "-q", tests_dir,
        external=True,
    )


@nox.session(python="3.13")
def lint(session: nox.Session) -> None:
    """Ruff over the whole workspace."""
    session.install("ruff>=0.14")
    session.run("ruff", "check", ".")


@nox.session(venv_backend="none")
def coverage(session: nox.Session) -> None:
    """Branch coverage over both modules, measured on the 3.10 floor."""
    # --all-packages: the checker's env must contain the real dependencies
    # (fastapi, tomli, …) for import resolution — not just the dev group.
    argv = ["uv", "run", "--python", "3.10", "--isolated", "--all-packages",
            "--group", "dev", "coverage", "run", "-m", "pytest", "-q"]
    session.run(*argv, external=True)
    session.run("uv", "run", "--group", "dev", "coverage", "report",
                "--skip-covered", external=True)


@nox.session(venv_backend="none")
def pyright(session: nox.Session) -> None:
    """Type-check on the lowest supported Python (3.10) — mirrors CI."""
    # --all-packages: the checker's env must contain the real dependencies
    # (fastapi, tomli, …) for import resolution — not just the dev group.
    session.run("uv", "run", "--python", "3.10", "--isolated",
                "--all-packages", "pyright", external=True)


# Param name must literally be `python` so session IDs request the clean
# `test_toolpatch-3.10` form used in the header help above.
@nox.parametrize("python", PYTHON_VERSIONS)
@nox.session(venv_backend="none")
def test_toolpatch(session: nox.Session, python: str) -> None:
    """toolpatch tests under one Python version."""
    _uv_pytest(session, python, TEST_DIRS["test_toolpatch"])


@nox.parametrize("python", PYTHON_VERSIONS)
@nox.session(venv_backend="none")
def test_proxytool(session: nox.Session, python: str) -> None:
    """proxytool tests under one Python version."""
    _uv_pytest(session, python, TEST_DIRS["test_proxytool"])


@nox.session
def test(session: nox.Session) -> None:
    """The full matrix: both modules x every supported Python version."""
    # A session.notify() of a parametrized session would run only one variant,
    # and notify() takes no kwargs in current nox — so drive the matrix
    # explicitly and keep going after failures to report the full picture.
    failed: list[str] = []
    for _py in PYTHON_VERSIONS:
        for _name, _tests_dir in TEST_DIRS.items():
            try:
                _uv_pytest(session, _py, _tests_dir)
            except Exception as exc:
                # session.run raises on non-zero exit; keep the matrix running.
                failed.append(f"{_name} py{_py}: {exc}")
    if failed:
        session.error("matrix failures:\n  " + "\n  ".join(failed))
