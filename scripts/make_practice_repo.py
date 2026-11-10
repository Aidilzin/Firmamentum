"""Create the throwaway practice repo used for real end-to-end checks of orch.

    uv run python scripts/make_practice_repo.py [--path C:\\projects\\orch-practice] [--force] [--no-lock]

Creates TWO sibling folders:
  <path>          a separate git repo (uv project "practice", one initial commit)
  <path>-hidden   NOT a git repo, never committed: tests the builder must not see.
                  Use it with `orch run ... --hidden-tests ../orch-practice-hidden`.

Refuses if either exists. --force replaces them, but only folders this script created (marker files).
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

DEFAULT_PATH = Path(r"C:\projects\orch-practice")
REPO_MARKER = Path(".git") / "orch-practice-marker"  # inside .git: never part of the tree
HIDDEN_MARKER = ".orch-practice-hidden"  # a dotfile: --hidden-tests skips dotfiles

PYPROJECT = """[project]
name = "practice"
version = "0.1.0"
description = "Throwaway repo for orch end-to-end checks"
requires-python = ">=3.12"
dependencies = []

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
"""

TEXT_PY = '''"""Tiny text helpers."""


def slugify(text: str) -> str:
    """Turn a title into a URL slug (basic version)."""
    return text.lower().replace(" ", "-")


def old_slug(text: str) -> str:
    """Deprecated: use slugify instead. Kept for old callers."""
    return text.lower().replace(" ", "_")
'''

TEST_TEXT = '''from practice.text import old_slug, slugify


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_single_word():
    assert slugify("Python") == "python"


def test_slugify_many_words():
    assert slugify("a b c") == "a-b-c"


def test_old_slug_underscores():
    assert old_slug("Hello World") == "hello_world"


def test_old_slug_lowercase():
    assert old_slug("ABC") == "abc"
'''

HIDDEN_TEST = '''from practice.text import slugify


def test_strips_accents():
    assert slugify("Café Déjà Vu") == "cafe-deja-vu"


def test_collapses_repeated_separators():
    assert slugify("a  --  b") == "a-b"
    assert slugify("one___two") == "one-two"


def test_trims_dashes_at_both_ends():
    assert slugify("  -Hello World-  ") == "hello-world"
'''

GITIGNORE = ".venv/\n__pycache__/\n.pytest_cache/\n.orch/\n"


class PracticeError(RuntimeError):
    pass


def hidden_path_for(path: Path) -> Path:
    return path.parent / (path.name + "-hidden")


def _git(cwd: Path, *args: str) -> None:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise PracticeError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")


def _rmtree(p: Path) -> None:
    def onerror(func, path, _exc):  # read-only files inside .git on Windows
        os.chmod(path, stat.S_IWRITE)
        func(path)

    shutil.rmtree(p, onerror=onerror)


def _is_ours(repo: Path, hidden: Path, which: str) -> bool:
    return (repo / REPO_MARKER).is_file() if which == "repo" else (hidden / HIDDEN_MARKER).is_file()


def make_practice_repo(path: Path, force: bool = False, lock: bool = True) -> tuple[Path, Path]:
    """Create the repo and the hidden-tests folder. Returns (repo, hidden)."""
    repo = Path(path).resolve()
    hidden = hidden_path_for(repo)
    existing = [(p, w) for p, w in ((repo, "repo"), (hidden, "hidden")) if p.exists()]
    if existing and not force:
        raise PracticeError(f"{existing[0][0]} already exists; use --force to replace it")
    for p, which in existing:
        if not _is_ours(repo, hidden, which):
            raise PracticeError(f"{p} exists but was not created by this script (no marker); refusing to delete it")
    for p, _ in existing:
        _rmtree(p)

    (repo / "src" / "practice").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (repo / ".python-version").write_text("3.12\n", encoding="utf-8")
    (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (repo / "src" / "practice" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "practice" / "text.py").write_text(TEXT_PY, encoding="utf-8")
    (repo / "tests" / "test_text.py").write_text(TEST_TEXT, encoding="utf-8")

    hidden.mkdir()
    (hidden / "test_slug_extra.py").write_text(HIDDEN_TEST, encoding="utf-8")
    (hidden / HIDDEN_MARKER).write_text("created by make_practice_repo.py\n", encoding="utf-8")

    if lock:  # a committed uv.lock, otherwise the first `uv run` in a worktree creates one and orch flags new_dependency
        p = subprocess.run(["uv", "lock"], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise PracticeError(f"uv lock failed: {(p.stderr or p.stdout).strip()}")

    _git(repo, "init", "-q", "-b", "main")
    (repo / REPO_MARKER).write_text("created by make_practice_repo.py\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=practice", "-c", "user.email=practice@localhost", "commit", "-q", "-m", "Initial practice repo")
    return repo, hidden


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--force", action="store_true", help="replace an existing practice repo (only if this script made it)")
    ap.add_argument("--no-lock", action="store_true", help="skip `uv lock` (tests only)")
    a = ap.parse_args(argv)
    try:
        repo, hidden = make_practice_repo(a.path, force=a.force, lock=not a.no_lock)
    except PracticeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(f"Created {repo} (1 commit) and {hidden} (hidden tests, not a git repo).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
