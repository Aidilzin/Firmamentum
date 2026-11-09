"""Tiny temp git repo for workspace/executor tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True)
    return p.stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "tester")
    git(path, "config", "user.email", "tester@example.com")
    (path / ".gitignore").write_text(".orch/\n__pycache__/\n", encoding="utf-8")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path
