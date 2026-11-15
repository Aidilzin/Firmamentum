"""`orch merge` for a docker repo (Phase 3): the merge result is tested in the container, on a throwaway worktree, and the
branch moves only on a pass. Everything against the fake engine; nothing starts Docker."""


import pytest
from git_helpers import git
from plan_factory import good_plan, step
from test_executor import PASS_CMD, edit, env  # noqa: F401  (env is a fixture)

from orch import sandbox
from orch.adapters.process import ProcessOutput
from orch.config import SandboxConfig
from orch.merge import MergeError, merge_run
from orch.runs import load_meta

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
    r = merge_run(rr, run_dir, cfg=SandboxConfig())
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
    r = merge_run(rr.model_copy(update={"test_command": FAIL_CMD}), run_dir, cfg=SandboxConfig())
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
    r = merge_run(rr, run_dir, cfg=SandboxConfig())
    assert r.outcome == "cannot_run" and "out of memory" in r.tests_detail and r.sha == ""
    assert git(repo, "rev-parse", "HEAD") == before and fake_docker.boxes == {}
    assert "out of memory" in r.tests_file.read_text(encoding="utf-8")


def test_docker_not_running_stops_before_anything_is_merged(done, fake_docker, monkeypatch):
    repo, run_dir, rr = done
    monkeypatch.setattr(sandbox, "docker_probe", lambda image: sandbox.ProbeResult(False, detail="engine stopped"))
    before = git(repo, "rev-parse", "HEAD")
    r = merge_run(rr, run_dir, cfg=SandboxConfig())
    assert r.outcome == "cannot_run" and "Docker is not running" in r.tests_detail and "Nothing was run" in r.tests_detail
    assert git(repo, "rev-parse", "HEAD") == before and fake_docker.runs == []


def test_a_container_left_by_a_killed_merge_is_removed_first(done, fake_docker):
    repo, run_dir, rr = done
    fake_docker.plant(run_dir.name, "running")
    logs = []
    r = merge_run(rr, run_dir, cfg=SandboxConfig(), log=logs.append)
    assert r.outcome == "merged" and fake_docker.boxes == {}
    assert any("left over" in m for m in logs)


def test_an_off_repo_uses_the_host_and_says_so(done, fake_docker):
    repo, run_dir, rr = done
    logs = []
    r = merge_run(rr.model_copy(update={"sandbox": "off"}), run_dir, log=logs.append)
    assert r.outcome == "merged" and fake_docker.calls == []
    assert sandbox.OFF_WARNING in logs
