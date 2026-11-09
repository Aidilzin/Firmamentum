"""Git worktrees for runs. One worktree per run, outside the target repo, on branch orch/<run_id>.

Every destructive git call (reset --hard, clean, worktree remove --force) goes through `_guard`, which
refuses any path that is not a linked worktree under `worktree_root()`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


class GuardError(WorkspaceError):
    """A destructive operation was aimed at a path outside the orch worktree root."""


@dataclass
class Worktree:
    path: Path
    branch: str
    base_sha: str


def worktree_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "orch" / "wt"
    return Path.home() / ".local" / "share" / "orch" / "wt"


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s) or "repo"


def worktree_path(repo_name: str, run_id: str) -> Path:
    return worktree_root() / _safe_name(repo_name) / run_id


def branch_name(run_id: str) -> str:
    return f"orch/{run_id}"


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if check and p.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed ({p.returncode}): {(p.stderr or p.stdout).strip()}")
    return p


def _guard(path: Path, repo: Path | None = None) -> Path:
    """Return the resolved path, or raise GuardError unless it is a linked worktree under the orch root."""
    resolved = Path(path).resolve()
    root = worktree_root().resolve()
    if repo is not None and resolved == Path(repo).resolve():
        raise GuardError(f"refusing to touch the main repo: {resolved}")
    if resolved == root or root not in resolved.parents:
        raise GuardError(f"{resolved} is not inside the orch worktree root {root}")
    if not (resolved / ".git").is_file():  # a main checkout has a .git directory
        raise GuardError(f"{resolved} is not a linked git worktree")
    return resolved


def is_clean(repo: Path) -> bool:
    """No uncommitted changes (the .orch/ run folder is ignored)."""
    out = git(repo, "status", "--porcelain", "--", ".", ":(exclude).orch").stdout
    return not out.strip()


def create(repo: Path, repo_name: str, run_id: str) -> Worktree:
    repo = Path(repo).resolve()
    if git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise WorkspaceError(f"{repo} is not a git repository")
    if not is_clean(repo):
        raise WorkspaceError(f"{repo} has uncommitted changes; commit or stash them first")
    wt = worktree_path(repo_name, run_id)
    if wt.exists():
        raise WorkspaceError(f"worktree path already exists: {wt}")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_name(run_id)
    git(repo, "worktree", "add", "-b", branch, str(wt), base)
    return Worktree(path=wt, branch=branch, base_sha=base)


def changed_files(wt: Path) -> list[str]:
    """Files changed vs HEAD, from git itself: modified, added, deleted, renamed, untracked (not ignored)."""
    out = git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    parts = out.split("\0")
    files: list[str] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        files.append(path)
        if "R" in xy or "C" in xy:  # the next entry is the original path
            if i < len(parts) and parts[i]:
                files.append(parts[i])
            i += 1
    return sorted(set(files))


def _identity_args(wt: Path) -> list[str]:
    args: list[str] = []
    if not git(wt, "config", "user.name", check=False).stdout.strip():
        args += ["-c", "user.name=orch"]
    if not git(wt, "config", "user.email", check=False).stdout.strip():
        args += ["-c", "user.email=orch@localhost"]
    return args


def commit_step(wt: Path, step_id: str, title: str, paths: list[str] | None = None) -> str:
    """Commit as `orch(<id>): <title>`. `paths` limits staging (so test artifacts are not committed)."""
    wt = _guard(wt)
    if paths is None:
        git(wt, "add", "-A")
    elif paths:
        git(wt, "add", "-A", "--", *paths)
    git(wt, *_identity_args(wt), "commit", "--allow-empty", "-m", f"orch({step_id}): {title}")
    return git(wt, "rev-parse", "HEAD").stdout.strip()


def diff(repo_or_wt: Path, a: str, b: str | None = None) -> str:
    """Diff between two refs/shas (works from the main repo, so it still works after `remove`)."""
    return git(repo_or_wt, "diff", a, *([b] if b else [])).stdout


def diff_uncommitted(wt: Path) -> str:
    git(wt, "add", "-N", ".", check=False)  # show new files too (intent-to-add; reset by the next step)
    return git(wt, "diff", "HEAD").stdout


def reset_to_head(wt: Path) -> None:
    """Throw away uncommitted work in the worktree (guarded)."""
    wt = _guard(wt)
    git(wt, "reset", "--hard", "HEAD")
    git(wt, "clean", "-fd")


def remove(repo: Path, repo_name: str, run_id: str, path: Path | None = None) -> bool:
    """Remove the worktree directory, keep the branch. Returns False if there was nothing to remove."""
    target = path if path is not None else worktree_path(repo_name, run_id)
    if not target.exists():
        return False
    wt = _guard(target, repo)
    git(Path(repo), "worktree", "remove", "--force", str(wt))
    git(Path(repo), "worktree", "prune", check=False)
    return True
