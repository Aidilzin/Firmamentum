"""Executor + sandbox (Phase 2): the repo's tests run through sandbox.run_tests, a container problem never reaches repair,
leftover containers are swept, and cancel leaves no container running. All against the fake engine."""

import shlex
import threading
import time

from test_cancel import SLEEP_CMD, one_step
from test_executor import FILE_CMD, PASS_CMD, edit, env, report  # noqa: F401  (env is a fixture)
from test_sandbox_run import exits

from orch import workspace
from orch.adapters.process import ProcessOutput
from orch.config import ResolvedRepo
from orch.runs import load_meta

FAIL_CMD = 'python -c "import sys; sys.exit(1)"'


def started(env, **kw):
    repo, setup, executor = env
    run_dir, rr = one_step(setup, **kw)
    return repo, run_dir, rr, executor


# ---------- the run goes through the sandbox ----------


def test_tests_run_in_the_container_on_the_guarded_worktree(env, fake_docker, isolated_worktree_root):
    repo, run_dir, rr, executor = started(env)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})])
    assert ex.start().status == "done"
    wt = isolated_worktree_root / "demo" / run_dir.name
    (run,) = fake_docker.runs
    assert f"type=bind,src={wt.resolve()},dst=/src,readonly" in run["argv"]
    assert f"orch.run={run_dir.name}" in run["argv"]
    assert run["inner"] == shlex.split(PASS_CMD) and run["src"] == wt.resolve()
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["tests"]["sandbox"] == "docker" and st["tests"]["passed"]
    assert "[sandbox: docker]" in (run_dir / "steps" / "s1" / "tests.txt").read_text(encoding="utf-8")
    assert fake_docker.boxes == {}  # the container is removed again


def test_an_off_repo_never_touches_docker(env, fake_docker):
    repo, run_dir, rr, executor = started(env)
    ex, _ = executor(run_dir, rr.model_copy(update={"sandbox": "off"}), [edit({"src/s1.py": "a = 1\n"})])
    assert ex.start().status == "done"
    assert fake_docker.calls == []
    assert "sandbox" not in load_meta(run_dir)["steps"]["s1"]["tests"]


def test_a_redirected_git_file_or_a_foreign_path_is_refused_before_docker(env, fake_docker):
    repo, run_dir, rr, executor = started(env)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})])
    assert ex.start().status == "done"
    fake_docker.calls.clear()
    out = run_dir / "x.txt"
    (ex.wt / ".git").unlink()
    (ex.wt / ".git").write_text(f"gitdir: {(repo / '.git').as_posix()}\n", encoding="utf-8")  # H4: points at the main repo
    ok, info = ex._run_cmd(PASS_CMD, out)
    assert not ok and info["unrunnable"] and "worktree refused" in info["error"]
    ex.wt = repo  # meta.json read back with the main checkout as the "worktree"
    ok, info = ex._run_cmd(PASS_CMD, out)
    assert not ok and info["unrunnable"] and "worktree refused" in info["error"]
    ex.wt = repo.parent
    assert not ex._run_cmd(PASS_CMD, out)[0]
    assert fake_docker.calls == []


# ---------- tests failed versus the container could not run them ----------


def test_a_container_failure_stops_the_run_and_sends_nothing_to_repair(env, fake_docker):
    repo, run_dir, rr, executor = started(env)
    fake_docker.script = [exits(137, oom=True)]
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})], max_repair_rounds=3)  # a repair call would crash the fake
    out = ex.start()
    assert out.status == "failed" and "out of memory" in out.message and "Nothing was sent to repair" in out.message
    meta = load_meta(run_dir)
    st = meta["steps"]["s1"]
    assert meta["status"] == "failed" and st["status"] == "pending" and st["stage"] == "sandbox"
    assert st["resume_phase"] == "tests" and st["rounds_used"] == 0 and st["rounds"] == [] and len(fb.requests) == 1
    assert st["tests"]["infra"] and not st["tests"]["passed"] and "out of memory" in st["error"]
    # you fix the problem and resume: the same step continues at the tests, no new build, no repair
    ex2, fb2 = executor(run_dir, rr, [], max_repair_rounds=3)
    assert ex2.resume().status == "done" and fb2.requests == []
    assert load_meta(run_dir)["steps"]["s1"]["stage"] is None


def test_the_stop_shows_up_in_next_actions_and_the_view(env, fake_docker):
    from orch import services
    from orch.view import build_run_view

    repo, run_dir, rr, executor = started(env)
    fake_docker.script = [exits(97)]
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})])
    ex.start()
    meta = load_meta(run_dir)
    acts = services.next_actions(meta, run_dir, "demo")
    assert [a.kind for a in acts] == ["resume"] and "sandbox" in acts[0].reason
    view = build_run_view(meta)
    assert view.stop is not None and view.stop.kind == "failed" and "stage sandbox" in view.stop.why
    assert view.steps[0].tests == "sandbox error"


def test_a_real_test_failure_still_goes_to_repair_in_the_same_sandbox(env, fake_docker):
    repo, run_dir, rr, executor = started(env, test_command=FILE_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"}), edit({"ok.txt": "1"})], max_repair_rounds=3)
    assert ex.start().status == "done"
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["rounds_used"] == 1 and len(fb.requests) == 2
    assert fake_docker.labels_run() == [f"orch.run={run_dir.name}"] * 2  # the repair round's run used the same label
    assert [r["inner"] for r in fake_docker.runs] == [shlex.split(FILE_CMD)] * 2


def test_repair_rounds_skip_the_retest_in_the_sandbox_and_say_so_once(env, fake_docker):
    repo, run_dir, rr, executor = started(env, test_command=FILE_CMD)
    rr = rr.model_copy(update={"retest_command": FAIL_CMD})  # would fail if it ran
    logs = []
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a"}), edit({"src/s1.py": "b"}), edit({"ok.txt": "1"})],
                     logs=logs, max_repair_rounds=3)  # fmt: skip
    assert ex.start().status == "done"
    assert all(r["inner"] == shlex.split(FILE_CMD) for r in fake_docker.runs) and len(fake_docker.runs) == 3
    assert sum("quick retest is skipped" in m for m in logs) == 1


def test_a_timeout_in_the_container_is_a_failed_test_not_an_infra_stop(env, fake_docker, monkeypatch):
    from orch import executor as executor_mod

    monkeypatch.setattr(executor_mod, "TEST_TIMEOUT_S", 1)
    repo, run_dir, rr, executor = started(env, test_command=SLEEP_CMD)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a"})], max_repair_rounds=0)
    out = ex.start()
    st = load_meta(run_dir)["steps"]["s1"]
    assert out.status != "done" and st["tests"]["error"] == "timed out" and not st["tests"].get("infra")
    assert st["stage"] != "sandbox" and fake_docker.boxes == {}


# ---------- cancel ----------


def test_cancel_during_the_tests_stops_the_container_and_resumes_at_tests(env, fake_docker):
    repo, run_dir, rr, executor = started(env, test_command=SLEEP_CMD)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    threading.Timer(2.0, ex.cancel).start()
    t0 = time.perf_counter()
    out = ex.start()
    assert out.status == "cancelled" and time.perf_counter() - t0 < 30
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["resume_phase"] == "tests" and st["tests"] is None and st["status"] == "pending"
    assert fake_docker.running() == [] and fake_docker.boxes == {}  # no container left behind
    ex2, fb2 = executor(run_dir, ResolvedRepo(name="demo", path=repo, test_command=PASS_CMD), [])
    assert ex2.resume().status == "done" and fb2.requests == []


# ---------- orphans: containers an earlier process of this run left behind ----------


def first_call_after(sim, n, verb):
    return next(i for i, c in enumerate(sim.calls[n:], n) if c[1] == verb)


def test_start_sweeps_this_runs_leftovers_before_the_first_test_run(env, fake_docker):
    repo, run_dir, rr, executor = started(env)
    mine = fake_docker.plant(run_dir.name, "running")
    other = fake_docker.plant("some-other-run", "running")
    logs = []
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})], logs=logs)
    assert ex.start().status == "done"
    assert mine.name not in fake_docker.boxes and other.name in fake_docker.boxes  # another run's container is not touched
    assert fake_docker.calls[0][1:2] == ["ps"] and fake_docker.calls[1][1:2] == ["rm"]
    assert first_call_after(fake_docker, 0, "rm") < first_call_after(fake_docker, 0, "run")
    assert any("left over from an earlier process" in m for m in logs)


def test_resume_approve_and_answer_sweep_too(env, fake_docker):
    repo, setup, executor = env
    # approve: a step that touched a protected file waits for you
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a", ".env.example": "x"})])
    assert ex.start().status == "awaiting_approval"
    fake_docker.plant(run_dir.name)
    n = len(fake_docker.calls)
    ex2, _ = executor(run_dir, rr, [])
    assert ex2.approve().status == "done"
    assert fake_docker.calls[n][1] == "ps" and fake_docker.calls[n + 1][1] == "rm"
    assert first_call_after(fake_docker, n, "rm") < first_call_after(fake_docker, n, "run")
    assert fake_docker.boxes == {}
    # answer: the builder was blocked and asks a question
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({}, rep=report(blocked=True, question="which db?"))])
    assert ex.start().status == "needs_input"
    fake_docker.plant(run_dir.name)
    n = len(fake_docker.calls)
    ex2, _ = executor(run_dir, rr, [edit({"src/s1.py": "a"})])
    assert ex2.answer(None, "sqlite").status == "done"
    assert first_call_after(fake_docker, n, "rm") < first_call_after(fake_docker, n, "run")
    assert fake_docker.boxes == {}
    # resume: a cancelled run
    run_dir, rr = one_step(setup, test_command=SLEEP_CMD)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a"})])
    threading.Timer(1.5, ex.cancel).start()
    assert ex.start().status == "cancelled"
    fake_docker.plant(run_dir.name)
    n = len(fake_docker.calls)
    ex2, _ = executor(run_dir, ResolvedRepo(name="demo", path=repo, test_command=PASS_CMD), [])
    assert ex2.resume().status == "done"
    assert first_call_after(fake_docker, n, "rm") < first_call_after(fake_docker, n, "run")
    assert fake_docker.boxes == {}


def test_reject_makes_no_docker_call(env, fake_docker):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a", ".env.example": "x"})])
    assert ex.start().status == "awaiting_approval"
    n = len(fake_docker.calls)
    ex2, _ = executor(run_dir, rr, [])
    assert ex2.reject("s1").status == "rejected"
    assert len(fake_docker.calls) == n


def test_a_failing_sweep_warns_and_the_run_goes_on(env, fake_docker):
    repo, run_dir, rr, executor = started(env)
    fake_docker.plant(run_dir.name)
    fake_docker.fail["rm"] = ProcessOutput(stderr="denied", returncode=1)
    logs = []
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a"})], logs=logs)
    assert ex.start().status == "done"
    assert any("docker rm -f" in m for m in logs)


# ---------- the warm step through the executor ----------


def test_the_warm_step_sees_the_base_commit_and_the_approved_lockfile_not_the_worktree(env, fake_docker):
    repo, run_dir, rr, executor = started(env)
    seen = {}

    def warm_ok(sim, box, argv):
        src = sim.runs[-1]["src"]
        seen["src"] = src
        seen["lock"] = (src / "uv.lock").read_text(encoding="utf-8")
        seen["agent_file"] = (src / "src").exists()
        seen["readme"] = (src / "README.md").is_file()
        return exits(0)(sim, box, argv)

    fake_docker.script = [exits(96), warm_ok]
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n", "uv.lock": "locked\n"})])
    assert ex.start().status == "awaiting_approval"  # a new lockfile is a dependency change: you approve it first
    ex2, _ = executor(run_dir, rr, [])
    assert ex2.approve().status == "done"
    wt = workspace.worktree_path("demo", run_dir.name).resolve()
    assert seen["lock"] == "locked\n" and seen["readme"] and not seen["agent_file"]
    assert seen["src"] != wt and wt not in seen["src"].parents
    assert not seen["src"].exists()  # deleted afterwards
    runs = fake_docker.runs
    assert [("--network" in r["argv"]) for r in runs] == [True, False, True]  # test, warm (network), test again


def test_an_unapproved_lockfile_change_is_refused_at_warm_time(env, fake_docker):
    repo, run_dir, rr, executor = started(env)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})])
    assert ex.start().status == "done"
    (ex.wt / "uv.lock").write_text("sneaky\n", encoding="utf-8")  # nobody approved this
    fake_docker.script = [exits(96)]
    n = len(fake_docker.runs)
    ok, info = ex._run_cmd(PASS_CMD, run_dir / "x.txt")
    assert not ok and info["infra"] and "dependency change not approved: uv.lock" in info["error"]
    assert len(fake_docker.runs) == n + 1  # the failed first attempt only: no warm container was started
