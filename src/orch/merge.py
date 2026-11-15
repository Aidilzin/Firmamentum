"""`orch merge`: merge a finished run's branch into the main repo's current branch. Never pushes.

The merge result is built first without touching your checkout (`git merge-tree` + `git commit-tree`), the tests run on a
throwaway worktree of that candidate commit, and the branch moves (`git merge --ff-only`) only if they pass. A failing run
leaves your branch and working tree exactly as they were (docs/sandbox-design.md, Phase 3).
"""

from __future__ import annotations

import shlex
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from . import runlock, sandbox, warm, workspace
from .adapters.process import child_env, run_process
from .config import Config, PostChecksConfig, ResolvedRepo, SandboxConfig
from .runs import load_meta, write_meta

PROTECTED_BRANCHES = ("main", "master")
TEST_TIMEOUT_S = 10 * 60
TESTS_FILE = "merge-tests.txt"

Outcome = Literal["merged", "already", "tests_failed", "cannot_run", "cancelled"]


class MergeError(RuntimeError):
    """A merge precondition failed; nothing was changed."""


@dataclass
class MergeResult:
    branch: str
    target: str
    sha: str  # the commit the target branch is at because of this merge; "" when nothing was merged
    tests_passed: bool | None  # None = could not run (or not run at all)
    tests_detail: str
    outcome: Outcome = "merged"
    target_sha: str = ""  # where the target branch was (and, unless merged, still is)
    tests_file: Path | None = None


@dataclass
class TestOutcome:
    kind: Literal["ran", "timed_out", "cannot_run", "cancelled"]
    exit_code: int | None
    text: str  # what is written to merge-tests.txt
    detail: str  # one short reason or the tail of the output


TestOutcome.__test__ = False  # not a pytest class, whatever module imports it


def short_request(run_dir: Path, max_len: int = 60) -> str:
    try:
        first = (run_dir / "request.md").read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        first = run_dir.name
    return first if len(first) <= max_len else first[: max_len - 1].rstrip() + "…"


def _tail(text: str, n: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def _run_on_host(repo: ResolvedRepo, argv: list[str], command: str, folder: Path, cancel: threading.Event | None) -> TestOutcome:
    out = run_process(argv, cwd=folder, env=child_env(passthrough=repo.env_passthrough), timeout_s=TEST_TIMEOUT_S, cancel=cancel)
    text = f"$ {command}\n[exit {out.returncode}]\n{out.stdout}\n--- stderr ---\n{out.stderr}\n"
    if out.cancelled:
        return TestOutcome("cancelled", None, text, "cancelled")
    if out.timed_out:
        return TestOutcome("timed_out", None, text + "\n[timed out]\n", "timed out")
    if out.not_found or out.returncode is None:
        return TestOutcome("cannot_run", None, text, "test command could not run")
    tail = _tail(out.stdout + out.stderr)
    return TestOutcome("ran", out.returncode, text, f"exit {out.returncode}" + (f"\n{tail}" if tail else ""))


def _run_in_sandbox(repo: ResolvedRepo, cfg: SandboxConfig, argv: list[str], command: str, folder: Path, run_id: str,
                    warm_src: sandbox.WarmSource | None, cancel: threading.Event | None,
                    log: Callable[[str], None]) -> TestOutcome:  # fmt: skip
    """The candidate worktree is mounted read-only in a container with no network, the same path a run's own tests take.
    A problem with the sandbox itself is `cannot_run` (never a test failure), so nothing is merged either way."""
    try:
        gone = sandbox.sweep(run_id, log)  # leftovers of a merge that was killed hard (the run lock proves nobody owns them)
    except sandbox.SandboxError as e:
        log(f"warning: no orphan sweep: {e}")
        gone = []
    if gone:
        log(f"removed {len(gone)} sandbox container(s) left over from an earlier process of this run")
    res = sandbox.run_tests(repo, cfg, folder, argv, run_id, warm_src=warm_src, timeout_s=TEST_TIMEOUT_S, cancel=cancel, log=log)
    text = f"$ {command}\n[sandbox: docker]\n[exit {res.exit_code}]\n{res.stdout}\n--- stderr ---\n{res.stderr}\n"
    if res.kind == "cancelled":
        return TestOutcome("cancelled", None, text, "cancelled")
    if res.kind == "timed_out":
        return TestOutcome("timed_out", None, text + "\n[timed out]\n", "timed out")
    if res.kind in ("infra", "unrunnable"):
        return TestOutcome("cannot_run", res.exit_code, text + f"\n[sandbox: {res.detail}]\n", res.detail)
    tail = _tail(res.stdout + res.stderr)
    return TestOutcome("ran", res.exit_code, text, f"exit {res.exit_code}" + (f"\n{tail}" if tail else ""))


def _run_tests(repo: ResolvedRepo, cfg: SandboxConfig, argv: list[str], command: str, folder: Path, run_id: str,
               warm_src: sandbox.WarmSource | None, cancel: threading.Event | None,
               log: Callable[[str], None]) -> TestOutcome:  # fmt: skip
    if repo.sandbox == "off":
        log(sandbox.OFF_WARNING)
        return _run_on_host(repo, argv, command, folder, cancel)
    return _run_in_sandbox(repo, cfg, argv, command, folder, run_id, warm_src, cancel, log)


def merge_run(
    repo: ResolvedRepo, run_dir: Path, allow_main: bool = False, *, cfg: Config | None = None,
    log: Callable[[str], None] = lambda m: None, cancel: threading.Event | None = None,
) -> MergeResult:  # fmt: skip
    """Merge a done run. Holds the run's lock for the whole time, so a second merge (or a resume) of the same run cannot
    race this one, and `orch doctor` can tell these containers from orphans."""
    try:
        lock = runlock.acquire(run_dir)
    except runlock.RunLocked as e:
        raise MergeError(f"this run is in use by {e.info.describe()}; wait for it or stop it, then merge again") from e
    try:
        return _merge_locked(repo, run_dir, allow_main, cfg, log, cancel)
    finally:
        lock.release()


def _merge_locked(repo: ResolvedRepo, run_dir: Path, allow_main: bool, cfg: Config | None, log: Callable[[str], None],
                  cancel: threading.Event | None) -> MergeResult:  # fmt: skip
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
    try:
        argv = shlex.split(repo.test_command)
    except ValueError as e:
        raise MergeError(f"bad test_command: {e}") from e

    target_sha = workspace.commit_sha(path, "HEAD")
    branch_sha = workspace.commit_sha(path, f"refs/heads/{branch}")

    def record(sha: str) -> None:
        meta["merged_sha"] = sha
        meta["merged_into"] = target
        write_meta(run_dir, meta)

    if workspace.is_ancestor(path, branch_sha, target_sha):  # merged by hand, or orch died before it wrote meta.json
        record(target_sha)
        return MergeResult(branch, target, target_sha, None, "the branch is already part of the current branch; no tests run",
                           "already", target_sha)  # fmt: skip

    try:
        candidate = workspace.merge_candidate(path, target_sha, branch_sha, f"Merge orch run: {short_request(run_dir)}")
    except workspace.MergeConflict as e:
        raise MergeError(f"{e}. Nothing was changed.") from e

    repo_name = ex.get("repo_name") or repo.name or path.name
    tmp_name = f"merge-{run_dir.name}"
    leftover = workspace.merge_worktree_path(repo_name, tmp_name)
    if leftover.exists():  # a merge that was killed hard
        try:
            workspace.remove(path, repo_name, tmp_name, path=leftover)
        except workspace.WorkspaceError as e:
            raise MergeError(f"a leftover folder of an earlier merge is in the way ({e}); remove it by hand") from e
    tmp = workspace.add_detached(path, repo_name, tmp_name, candidate)
    try:
        where = "in the sandbox" if repo.sandbox == "docker" else "on this machine"
        log(f"testing the merge result {where}; {target} is not touched until the tests pass")
        folder = workspace._guard(tmp, path)
        patterns = (cfg.post_checks if cfg else PostChecksConfig()).dependency
        approved = warm.approved_dependency_files(meta)

        def warm_src():  # the one container with network: your commit plus the dependency files you approved, never the candidate
            return warm.merge_warm_tree(path, folder, target_sha, branch_sha, candidate, f"merge-{run_dir.name}", patterns, approved)

        res = _run_tests(repo, cfg.sandbox if cfg else SandboxConfig(), argv, repo.test_command, folder, run_dir.name, warm_src,
                         cancel, log)  # fmt: skip
        tests_file = run_dir / TESTS_FILE
        tests_file.write_text(res.text, encoding="utf-8")
    finally:
        try:
            workspace.remove(path, repo_name, tmp_name, path=tmp)
        except workspace.WorkspaceError as e:
            log(f"warning: could not remove the temporary worktree {tmp}: {e}")

    def stopped(outcome: Outcome, passed: bool | None) -> MergeResult:
        return MergeResult(branch, target, "", passed, res.detail, outcome, target_sha, tests_file)

    if res.kind == "cancelled":
        return stopped("cancelled", None)
    if res.kind == "cannot_run":
        return stopped("cannot_run", None)
    if res.kind == "timed_out" or res.exit_code != 0:
        return stopped("tests_failed", False)

    # The tests passed. Look once more at the checkout: you may have worked in it meanwhile.
    if workspace.current_branch(path) != target or not workspace.is_clean(path):
        raise MergeError(f"{path} changed while the tests ran (branch or uncommitted files); nothing was merged. Run it again.")
    try:
        workspace.fast_forward(path, candidate)
    except workspace.WorkspaceError as e:
        raise MergeError(f"{e}. The branch moved while the tests ran; nothing was merged. Run it again.") from e
    record(candidate)
    return MergeResult(branch, target, candidate, True, res.detail, "merged", target_sha, tests_file)
