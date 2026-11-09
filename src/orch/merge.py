"""`orch merge`: merge a finished run's branch into the main repo's current branch. Never pushes."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from . import workspace
from .adapters.process import child_env, run_process
from .config import ResolvedRepo
from .runs import load_meta, write_meta

PROTECTED_BRANCHES = ("main", "master")
TEST_TIMEOUT_S = 10 * 60


class MergeError(RuntimeError):
    """A merge precondition failed; nothing was changed."""


@dataclass
class MergeResult:
    branch: str
    target: str
    sha: str
    tests_passed: bool | None  # None = could not run
    tests_detail: str


def short_request(run_dir: Path, max_len: int = 60) -> str:
    try:
        first = (run_dir / "request.md").read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        first = run_dir.name
    return first if len(first) <= max_len else first[: max_len - 1].rstrip() + "…"


def merge_run(repo: ResolvedRepo, run_dir: Path, allow_main: bool = False) -> MergeResult:
    meta = load_meta(run_dir)
    ex = meta.get("exec")
    if meta.get("status") != "done" or not ex:
        raise MergeError(f"run is {meta.get('status')!r}, not fully done; finish it first (orch status {run_dir.name})")
    if meta.get("merged_sha"):
        raise MergeError(f"run was already merged ({meta['merged_sha'][:8]})")
    if not repo.test_command:
        raise MergeError("this repo has no test_command in config.yaml")
    path = repo.path
    branch = ex["branch"]
    if not workspace.branch_exists(path, branch):
        raise MergeError(f"branch {branch} does not exist")
    if not workspace.is_clean(path):
        raise MergeError(f"{path} has uncommitted changes; commit or stash them first")
    target = workspace.current_branch(path)
    if target is None:
        raise MergeError("the main repo is on a detached HEAD; check out a branch first")
    if target in PROTECTED_BRANCHES and not allow_main:
        raise MergeError(f"the current branch is {target!r}; check out another branch or pass --allow-main")
    sha = workspace.merge_no_ff(path, branch, f"Merge orch run: {short_request(run_dir)}")
    meta["merged_sha"] = sha
    meta["merged_into"] = target
    write_meta(run_dir, meta)

    try:
        argv = shlex.split(repo.test_command)
    except ValueError as e:
        return MergeResult(branch, target, sha, None, f"bad test_command: {e}")
    out = run_process(argv, cwd=path, env=child_env(), timeout_s=TEST_TIMEOUT_S)
    if out.not_found or out.timed_out or out.returncode is None:
        return MergeResult(branch, target, sha, None, "timed out" if out.timed_out else "test command could not run")
    tail = "\n".join((out.stdout + out.stderr).strip().splitlines()[-15:])
    return MergeResult(branch, target, sha, out.returncode == 0, f"exit {out.returncode}\n{tail}")
