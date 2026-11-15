"""`orch merge` for a docker repo (Phase 3): the merge result is tested in the container, on a throwaway worktree, and the
branch moves only on a pass. Everything against the fake engine; nothing starts Docker."""


import threading
from pathlib import Path

import pytest
from git_helpers import git
from plan_factory import good_plan, step
from test_executor import PASS_CMD, edit, env  # noqa: F401  (env is a fixture)

from orch import runlock, sandbox, warm, workspace
from orch.adapters.process import ProcessOutput
from orch.checks import RULE_DEPENDENCY
from orch.merge import MergeError, merge_run
from orch.runs import load_meta, write_meta

FAIL_CMD = 'python -c "import sys; sys.exit(1)"'


@pytest.fixture
def done(env, fake_docker):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})])
    assert ex.start().status == "done"
    git(repo, "checkout", "-q", "-b", "work")
    fake_docker.calls.clear()  # the run's own test container is not what these tests look at
    fake_docker.runs.clear()
    return repo, run_dir, rr



def test_the_tests_run_in_a_container_on_the_candidate_never_on_your_checkout(done, fake_docker, isolated_worktree_root):
    repo, run_dir, rr = done
    r = merge_run(rr, run_dir)
    assert r.outcome == "merged" and r.tests_passed is True
    (run,) = fake_docker.runs
    src = run["src"]
    assert src.name == f"merge-{run_dir.name}" and isolated_worktree_root.resolve() in src.parents
    assert f"type=bind,src={src},dst=/src,readonly" in run["argv"] and "--network" in run["argv"]
    assert f"orch.run={run_dir.name}" in run["argv"]
    assert str(repo.resolve()) not in " ".join(run["argv"])
    assert "[sandbox: docker]" in r.tests_file.read_text(encoding="utf-8")
    assert fake_docker.boxes == {} and not src.exists()  # no container and no worktree left


def test_a_failing_test_in_the_container_merges_nothing(done, fake_docker):
    repo, run_dir, rr = done
    before = git(repo, "rev-parse", "HEAD")
    r = merge_run(rr.model_copy(update={"test_command": FAIL_CMD}), run_dir)
    assert r.outcome == "tests_failed" and r.sha == ""
    assert git(repo, "rev-parse", "HEAD") == before and "merged_sha" not in load_meta(run_dir)
    assert fake_docker.boxes == {}


def _infra(sim, box, argv):
    box.status, box.oom, box.exit_code = "exited", True, 137
    return ProcessOutput(returncode=137, stderr="")


def test_an_out_of_memory_container_is_a_sandbox_error_and_merges_nothing(done, fake_docker):
    repo, run_dir, rr = done
    fake_docker.script = [_infra]
    before = git(repo, "rev-parse", "HEAD")
    r = merge_run(rr, run_dir)
    assert r.outcome == "cannot_run" and "out of memory" in r.tests_detail and r.sha == ""
    assert git(repo, "rev-parse", "HEAD") == before and fake_docker.boxes == {}
    assert "out of memory" in r.tests_file.read_text(encoding="utf-8")


def test_docker_not_running_stops_before_anything_is_merged(done, fake_docker, monkeypatch):
    repo, run_dir, rr = done
    monkeypatch.setattr(sandbox, "docker_probe", lambda image: sandbox.ProbeResult(False, detail="engine stopped"))
    before = git(repo, "rev-parse", "HEAD")
    r = merge_run(rr, run_dir)
    assert r.outcome == "cannot_run" and "Docker is not running" in r.tests_detail and "Nothing was run" in r.tests_detail
    assert git(repo, "rev-parse", "HEAD") == before and fake_docker.runs == []


def test_a_container_left_by_a_killed_merge_is_removed_first(done, fake_docker):
    repo, run_dir, rr = done
    fake_docker.plant(run_dir.name, "running")
    logs = []
    r = merge_run(rr, run_dir, log=logs.append)
    assert r.outcome == "merged" and fake_docker.boxes == {}
    assert any("left over" in m for m in logs)


def test_an_off_repo_uses_the_host_and_says_so(done, fake_docker):
    repo, run_dir, rr = done
    logs = []
    r = merge_run(rr.model_copy(update={"sandbox": "off"}), run_dir, log=logs.append)
    assert r.outcome == "merged" and fake_docker.calls == []
    assert sandbox.OFF_WARNING in logs


# ---------- the warm step for a merge ----------

BASE_DEPS = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\nf = 6\n"


@pytest.fixture
def done_deps(env, fake_docker):
    """A done run whose base commit already has a pyproject.toml, so the branch and the target can both change it."""
    repo, setup, executor = env
    (repo / "pyproject.toml").write_text(BASE_DEPS, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "deps")
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})])
    assert ex.start().status == "done"
    git(repo, "checkout", "-q", "-b", "work")
    fake_docker.calls.clear()
    fake_docker.runs.clear()
    return repo, run_dir, rr


def commit_in(where: Path, name: str, text: str, msg: str) -> None:
    (where / name).write_text(text, encoding="utf-8")
    git(where, "add", "-A")
    git(where, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)


def approve_dependency(run_dir, name):
    meta = load_meta(run_dir)
    meta["steps"]["s1"].setdefault("approved", []).append({"rule": RULE_DEPENDENCY, "file": name})
    write_meta(run_dir, meta)


def not_ready(sim, box, argv):
    box.status, box.exit_code = "exited", 96
    return ProcessOutput(returncode=96, stderr="orch sandbox: the venv volume is not prepared")


def warm_ok(seen):
    def script(sim, box, argv):
        src = sim.runs[-1]["src"]
        seen.update(src=src, deps=(src / "pyproject.toml").read_text(encoding="utf-8"), has_git=(src / ".git").exists(),
                    has_agent_code=(src / "src" / "s1.py").exists(), network="--network" in argv)
        box.status, box.exit_code = "exited", 0
        return ProcessOutput(returncode=0)

    return script


def test_a_cold_venv_is_warmed_from_your_commit_not_from_the_candidate(done_deps, fake_docker):
    repo, run_dir, rr = done_deps
    seen = {}
    fake_docker.script = [not_ready, warm_ok(seen)]
    r = merge_run(rr, run_dir)
    assert r.outcome == "merged" and len(fake_docker.runs) == 3  # first try, warm, second try
    assert seen["deps"] == BASE_DEPS and not seen["has_git"] and not seen["has_agent_code"] and not seen["network"]
    assert seen["src"] != fake_docker.runs[0]["src"] and warm.workspace.warm_root() in seen["src"].parents
    assert not seen["src"].exists() and fake_docker.boxes == {}


def test_a_dependency_change_you_approved_reaches_the_warm_step(done_deps, fake_docker):
    repo, run_dir, rr = done_deps
    commit_in(Path(load_meta(run_dir)["exec"]["worktree"]), "pyproject.toml", BASE_DEPS.replace("a = 1", "a = 10"), "dep")
    approve_dependency(run_dir, "pyproject.toml")
    seen = {}
    fake_docker.script = [not_ready, warm_ok(seen)]
    assert merge_run(rr, run_dir).outcome == "merged"
    assert seen["deps"].startswith("a = 10")


def test_an_unapproved_dependency_change_stops_before_the_warm_container(done_deps, fake_docker):
    repo, run_dir, rr = done_deps
    commit_in(Path(load_meta(run_dir)["exec"]["worktree"]), "pyproject.toml", BASE_DEPS.replace("a = 1", "a = 10"), "dep")
    fake_docker.script = [not_ready]
    before = git(repo, "rev-parse", "HEAD")
    r = merge_run(rr, run_dir)
    assert r.outcome == "cannot_run" and "not approved: pyproject.toml" in r.tests_detail
    assert len(fake_docker.runs) == 1 and git(repo, "rev-parse", "HEAD") == before


def test_a_dependency_file_changed_on_both_sides_is_never_warmed_even_if_approved(done_deps, fake_docker):
    repo, run_dir, rr = done_deps
    commit_in(Path(load_meta(run_dir)["exec"]["worktree"]), "pyproject.toml", BASE_DEPS.replace("a = 1", "a = 10"), "dep")
    approve_dependency(run_dir, "pyproject.toml")
    commit_in(repo, "pyproject.toml", BASE_DEPS.replace("f = 6", "f = 60"), "your own dependency change")
    fake_docker.script = [not_ready]
    r = merge_run(rr, run_dir)
    assert r.outcome == "cannot_run" and "changed on both sides" in r.tests_detail
    assert len(fake_docker.runs) == 1 and git(repo, "log", "-1", "--format=%s").strip() == "your own dependency change"


# ---------- lock, leftovers, Ctrl-C ----------

def test_a_run_that_is_in_use_cannot_be_merged(done, fake_docker):
    repo, run_dir, rr = done
    held = runlock.acquire(run_dir)
    try:
        before = git(repo, "rev-parse", "HEAD")
        with pytest.raises(MergeError, match="in use by"):
            merge_run(rr, run_dir)
        assert git(repo, "rev-parse", "HEAD") == before and fake_docker.runs == []
    finally:
        held.release()
    assert merge_run(rr, run_dir).outcome == "merged"  # the lock is released again after a merge
    assert runlock.holder(run_dir) is None


def test_the_lock_is_released_after_a_refusal_too(done):
    repo, run_dir, rr = done
    git(repo, "checkout", "-q", "main")
    with pytest.raises(MergeError, match="allow-main"):
        merge_run(rr, run_dir)
    assert runlock.holder(run_dir) is None


def test_a_worktree_left_by_a_killed_merge_is_replaced(done, isolated_worktree_root):
    repo, run_dir, rr = done
    name = f"merge-{run_dir.name}"
    stale = workspace.add_detached(repo, load_meta(run_dir)["exec"]["repo_name"], name, workspace.commit_sha(repo, "HEAD"))
    (stale / "stale.txt").write_text("x", encoding="utf-8")
    assert merge_run(rr, run_dir).outcome == "merged"
    assert not stale.exists()


def test_cancel_merges_nothing_and_cleans_up(done, fake_docker):
    repo, run_dir, rr = done
    cancel = threading.Event()
    cancel.set()
    before = git(repo, "rev-parse", "HEAD")
    r = merge_run(rr, run_dir, cancel=cancel)
    assert r.outcome == "cancelled" and r.sha == "" and r.tests_passed is None
    assert git(repo, "rev-parse", "HEAD") == before and "merged_sha" not in load_meta(run_dir)
    assert fake_docker.boxes == {} and runlock.holder(run_dir) is None
    assert not any("merge-" in ln for ln in git(repo, "worktree", "list").splitlines())


def test_an_interrupt_during_the_tests_still_cleans_up(done, fake_docker, monkeypatch):
    repo, run_dir, rr = done

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(sandbox, "docker_run", boom)
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(KeyboardInterrupt):
        merge_run(rr, run_dir)
    assert git(repo, "rev-parse", "HEAD") == before and runlock.holder(run_dir) is None
    assert not any("merge-" in ln for ln in git(repo, "worktree", "list").splitlines())
