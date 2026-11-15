"""Git worktrees for runs. One worktree per run, outside the target repo, on branch orch/<run_id>.

Every destructive git call (reset --hard, clean, worktree remove --force) goes through `_guard`, which
refuses any path that is not a linked worktree under `worktree_root()`.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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


def warm_root() -> Path:
    """Scratch folders for the sandbox warm step (a run's base commit exported without .git), next to the worktrees."""
    return worktree_root().parent / "warm"


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


def _guard(path: Path, repo: Path | None = None, branch: str | None = None) -> Path:
    """Return the resolved path, or raise GuardError unless it is a linked worktree under the orch root.

    The `.git` file inside a worktree is writable by the builder, so what it points to is checked too: git's
    own git-dir must be a `worktrees/<name>` folder of the common dir (not the main repo's `.git`), and when
    `branch` is given the worktree must have exactly that branch checked out."""
    resolved = Path(path).resolve()
    root = worktree_root().resolve()
    if repo is not None and resolved == Path(repo).resolve():
        raise GuardError(f"refusing to touch the main repo: {resolved}")
    if resolved == root or root not in resolved.parents:
        raise GuardError(f"{resolved} is not inside the orch worktree root {root}")
    if not (resolved / ".git").is_file():  # a main checkout has a .git directory
        raise GuardError(f"{resolved} is not a linked git worktree")
    p = git(resolved, "rev-parse", "--absolute-git-dir", "--git-common-dir", check=False)
    lines = p.stdout.splitlines()
    if p.returncode != 0 or len(lines) != 2:
        raise GuardError(f"{resolved}: cannot read its git directory")
    git_dir = Path(lines[0]).resolve()
    common = Path(lines[1])
    common = (resolved / common if not common.is_absolute() else common).resolve()
    if git_dir.parent != common / "worktrees":
        raise GuardError(f"{resolved}: its .git file does not point at a linked-worktree folder ({git_dir})")
    if repo is not None and common != (Path(repo).resolve() / ".git"):
        raise GuardError(f"{resolved}: belongs to a different repository ({common})")
    if branch is not None and current_branch(resolved) != branch:
        raise GuardError(f"{resolved}: expected branch {branch}, found {current_branch(resolved)}")
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


def _no_hooks_args() -> list[str]:
    """`-c` options for orch's own git writes (commit, merge): repo hooks never run, and no fsmonitor daemon is
    started. A tracked hooks folder (`.githooks`, husky) is editable by the builder, so orch must not execute it."""
    empty = Path(tempfile.gettempdir()) / "orch-no-hooks"
    empty.mkdir(parents=True, exist_ok=True)
    return ["-c", f"core.hooksPath={empty}", "-c", "core.fsmonitor=false"]


def _identity_args(wt: Path) -> list[str]:
    args: list[str] = []
    if not git(wt, "config", "user.name", check=False).stdout.strip():
        args += ["-c", "user.name=orch"]
    if not git(wt, "config", "user.email", check=False).stdout.strip():
        args += ["-c", "user.email=orch@localhost"]
    return args


def head_sha(wt: Path) -> str:
    return git(wt, "rev-parse", "HEAD").stdout.strip()


def check_head(wt: Path, base_sha: str, branch: str) -> str | None:
    """None when the worktree is still on `branch` at `base_sha`; otherwise a description of what moved.
    Every check compares against HEAD, so a commit made by the agent would hide its own change."""
    try:
        _guard(wt, branch=branch)
        now = head_sha(wt)
    except WorkspaceError as e:
        return str(e)
    if now != base_sha:
        return f"HEAD moved from {base_sha[:8]} to {now[:8]} during the step (the agent committed or reset)"
    return None


def step_commit_at_head(wt: Path, base_sha: str, tree: str, branch: str | None = None) -> str | None:
    """HEAD's sha when it is exactly the commit orch was about to make: its parent is `base_sha` and its tree is `tree`
    (the tree id orch stored from the staged index before `git commit`). The message is never consulted.
    Used after a crash between `git commit` and saving meta.json, so a finished commit is not seen as an agent's."""
    wt = _guard(wt, branch=branch)
    head = head_sha(wt)
    if head == base_sha:
        return None
    parent = git(wt, "rev-parse", "HEAD^", check=False).stdout.strip()
    head_tree = git(wt, "rev-parse", "HEAD^{tree}", check=False).stdout.strip()
    return head if parent == base_sha and head_tree == tree else None


def commit_step(
    wt: Path, step_id: str, title: str, paths: list[str] | None = None, branch: str | None = None,
    on_staged: Callable[[str], None] | None = None,
) -> str:  # fmt: skip
    """Commit as `orch(<id>): <title>`. `paths` limits staging (so test artifacts are not committed).
    `on_staged(tree_id)` is called with `git write-tree` of the staged index just before `git commit`."""
    wt = _guard(wt, branch=branch)
    if paths is None:
        git(wt, "add", "-A")
    elif paths:
        git(wt, "add", "-A", "--", *paths)
    if on_staged is not None:
        on_staged(git(wt, "write-tree").stdout.strip())
    git(wt, *_no_hooks_args(), *_identity_args(wt), "commit", "--allow-empty", "-m", f"orch({step_id}): {title}")
    return git(wt, "rev-parse", "HEAD").stdout.strip()


_REV = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]*")


def export_tree(repo: Path, rev: str, dest: Path) -> None:
    """Write the files of commit `rev` into `dest` (created; it must not exist or be empty). No `.git`, no history.
    Unpacked with tarfile's `data` filter, so a link or path in the archive cannot point outside `dest`."""
    if not _REV.fullmatch(rev or ""):
        raise WorkspaceError(f"bad revision {rev!r}")
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise WorkspaceError(f"export folder is not empty: {dest}")
    p = subprocess.run(["git", "archive", "--format=tar", rev], cwd=repo, capture_output=True, check=False)
    if p.returncode != 0:
        raise WorkspaceError(f"git archive {rev} failed ({p.returncode}): {p.stderr.decode('utf-8', 'replace').strip()}")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(p.stdout), mode="r:") as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        raise WorkspaceError(f"cannot unpack {rev} into {dest}: {e}") from e


def diff(repo_or_wt: Path, a: str, b: str | None = None) -> str:
    """Diff between two refs/shas (works from the main repo, so it still works after `remove`)."""
    return git(repo_or_wt, "diff", a, *([b] if b else [])).stdout


def diff_uncommitted(wt: Path) -> str:
    git(wt, "add", "-N", ".", check=False)  # show new files too (intent-to-add; reset by the next step)
    return git(wt, "diff", "HEAD").stdout


def reset_to_head(wt: Path, branch: str | None = None) -> None:
    """Throw away uncommitted work in the worktree (guarded)."""
    wt = _guard(wt, branch=branch)
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


# ---------- test-removal scan ----------

_TEST_FILE_RE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$", re.IGNORECASE)
_PY_TEST_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
_JS_TEST_RE = re.compile(r"""^\s*(?:it|test)\s*\(\s*['"`]([^'"`]+)['"`]""")


def is_test_path(path: str) -> bool:
    return bool(_TEST_FILE_RE.search(path.replace("\\", "/")))


def _test_name(line: str) -> str | None:
    for rx in (_PY_TEST_RE, _JS_TEST_RE):
        m = rx.match(line)
        if m:
            return m.group(1)
    return None


def removed_tests(wt: Path) -> list[str]:
    """Existing tests the working tree removed vs HEAD: deleted test files (`path`) and removed test
    functions (`path::name`, only when no test of that name was added back in the same file)."""
    found: list[str] = []
    status = git(wt, "diff", "HEAD", "--name-status", "--no-renames").stdout
    for line in status.splitlines():
        code, _, path = line.partition("\t")
        if code.startswith("D") and is_test_path(path):
            found.append(path)
    removed: dict[str, set[str]] = {}
    added: dict[str, set[str]] = {}
    current: str | None = None
    for line in git(wt, "diff", "HEAD", "-U0", "--no-color", "--no-renames").stdout.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("--- "):
            continue
        elif current and line[:1] in "+-" and is_test_path(current):
            name = _test_name(line[1:])
            if name:
                (removed if line[0] == "-" else added).setdefault(current, set()).add(name)
    for path, names in removed.items():
        found += [f"{path}::{n}" for n in sorted(names - added.get(path, set()))]
    return sorted(set(found))


# ---------- merge ----------


def current_branch(repo: Path) -> str | None:
    """Current branch name, or None for a detached HEAD."""
    out = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def branch_exists(repo: Path, branch: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def merge_no_ff(repo: Path, branch: str, message: str) -> str:
    """`git merge --no-ff` into the current branch. On conflict the merge is aborted and WorkspaceError raised."""
    repo = Path(repo)
    p = git(repo, *_no_hooks_args(), *_identity_args(repo), "merge", "--no-ff", "-m", message, branch, check=False)
    if p.returncode != 0:
        git(repo, "merge", "--abort", check=False)
        raise WorkspaceError(f"merge of {branch} failed and was aborted: {(p.stderr or p.stdout).strip()[:500]}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------- merge candidate: build and test the merge result before the branch moves ----------

MIN_GIT = (2, 38)  # `git merge-tree --write-tree`
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


class MergeConflict(WorkspaceError):
    """The two sides do not merge cleanly. Nothing was changed. `files` are the conflicted paths."""

    def __init__(self, files: list[str]):
        self.files = files
        super().__init__("merge conflict in: " + (", ".join(files) or "(git named no file)"))


def _full_sha(sha: str) -> str:
    if not _SHA.fullmatch(sha or ""):
        raise WorkspaceError(f"not a full commit id: {sha!r}")
    return sha


def commit_sha(repo: Path, rev: str) -> str:
    """The full commit id of a branch name or revision."""
    if not _REV.fullmatch(rev or ""):
        raise WorkspaceError(f"bad revision {rev!r}")
    return git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").stdout.strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when `descendant` already contains `ancestor`."""
    p = git(repo, "merge-base", "--is-ancestor", _full_sha(ancestor), _full_sha(descendant), check=False)
    if p.returncode not in (0, 1):
        raise WorkspaceError(f"git merge-base --is-ancestor failed ({p.returncode}): {p.stderr.strip()}")
    return p.returncode == 0


def merge_candidate(repo: Path, target_sha: str, branch_sha: str, message: str) -> str:
    """The commit a `--no-ff` merge of `branch_sha` into `target_sha` would make, built with `git merge-tree` and
    `git commit-tree`: no checkout, index, ref or hook is touched. Raises MergeConflict when it does not merge cleanly."""
    repo = Path(repo)
    _full_sha(target_sha)
    _full_sha(branch_sha)
    p = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", target_sha, branch_sha, check=False)
    parts = [x for x in p.stdout.split("\0") if x]
    if p.returncode == 1 and parts and _SHA.fullmatch(parts[0]):
        raise MergeConflict(sorted(set(parts[1:])))
    if p.returncode != 0 or not parts or not _SHA.fullmatch(parts[0]):
        why = (p.stderr or p.stdout).strip()[:300]
        raise WorkspaceError(f"git merge-tree failed ({p.returncode}; it needs git {MIN_GIT[0]}.{MIN_GIT[1]} or newer): {why}")
    out = git(repo, *_identity_args(repo), "commit-tree", parts[0], "-p", target_sha, "-p", branch_sha, "-m", message)
    return out.stdout.strip()


def merge_worktree_path(repo_name: str, name: str) -> Path:
    return worktree_root() / _safe_name(repo_name) / _safe_name(name)


def add_detached(repo: Path, repo_name: str, name: str, sha: str) -> Path:
    """A throwaway linked worktree of commit `sha` under the orch worktree root, on no branch. Repo hooks do not run
    (a tracked hooks folder would make `post-checkout` agent-written code). Remove it with `remove(..., path=...)`."""
    repo = Path(repo).resolve()
    wt = merge_worktree_path(repo_name, name)
    if wt.exists():
        raise WorkspaceError(f"worktree path already exists: {wt}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "prune", check=False)  # forget a registration whose folder is gone
    git(repo, *_no_hooks_args(), "worktree", "add", "--detach", str(wt), _full_sha(sha))
    return wt


def fast_forward(repo: Path, sha: str) -> None:
    """`git merge --ff-only <sha>` in the checkout, without repo hooks. Refuses (WorkspaceError, nothing changed) when
    the current branch is no longer an ancestor of `sha` or a local change is in the way."""
    p = git(Path(repo), *_no_hooks_args(), "merge", "--ff-only", _full_sha(sha), check=False)
    if p.returncode != 0:
        raise WorkspaceError(f"fast-forward refused: {(p.stderr or p.stdout).strip()[:300]}")


def git_version() -> tuple[int, int] | None:
    """(major, minor) of the git on PATH, or None when it cannot be read."""
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None
