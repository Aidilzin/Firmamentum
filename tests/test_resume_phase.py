"""P2 (H6): a crashed step resumes at its stored phase; a rebuild starts a clean attempt. Fakes only."""

import json

import pytest
from git_helpers import git
from plan_factory import good_plan, step
from test_executor import PASS_CMD, Boom, FakeBuilder, edit, env  # noqa: F401  (env is a fixture)
from test_phase4 import ON, review

from orch import workspace
from orch.config import load_config
from orch.executor import Executor, resume_point
from orch.runs import load_meta, write_meta


def boom(req):
    raise Boom("simulated crash")


def one_step(setup, **kw):
    return setup(plan=good_plan(steps=[step("s1")]), **kw)


def crash_once(monkeypatch, cls, name):
    real = getattr(cls, name)

    def wrapper(*a, **k):
        monkeypatch.setattr(cls, name, real)  # only the first call dies
        raise Boom("simulated crash")

    monkeypatch.setattr(cls, name, wrapper)


def test_resume_point_table():
    for p in ("checks", "tests", "review", "need_repair", "commit"):
        assert resume_point(p) == p
    for p in ("build", "repair", None, "nonsense"):
        assert resume_point(p) is None


def test_crash_in_review_resumes_at_review_without_a_new_build(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"}), boom], **ON)
    with pytest.raises(Boom):
        ex.start()
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["status"] == "running" and st["phase"] == "review" and st["resume_phase"] is None
    ex2, fb2 = executor(run_dir, rr, [review("approve")], **ON)
    assert ex2.resume().status == "done"
    assert len(fb2.requests) == 1  # the reviewer only: no second builder, no second review of a rebuilt change
    meta = load_meta(run_dir)
    assert meta["steps"]["s1"]["status"] == "done" and meta["steps"]["s1"]["roles_tokens"]["builder"] == 100
    wt = workspace.Path(meta["exec"]["worktree"])
    assert "a = 1" in (wt / "src" / "s1.py").read_text(encoding="utf-8")
    assert "src/s1.py" in git(wt, "show", "--name-only", "--format=", "HEAD")


def test_crash_in_tests_resumes_at_tests_with_no_agent_call(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    crash_once(monkeypatch, Executor, "_run_cmd")
    with pytest.raises(Boom):
        ex.start()
    assert load_meta(run_dir)["steps"]["s1"]["phase"] == "tests"
    ex2, fb2 = executor(run_dir, rr, [])
    assert ex2.resume().status == "done" and fb2.requests == []


def test_crash_after_git_commit_before_meta_saved_does_not_fail_the_step(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    real = workspace.commit_step

    def commit_then_die(*a, **k):
        real(*a, **k)
        raise Boom("died right after git commit")

    monkeypatch.setattr(workspace, "commit_step", commit_then_die)
    with pytest.raises(Boom):
        ex.start()
    monkeypatch.setattr(workspace, "commit_step", real)
    meta = load_meta(run_dir)
    assert meta["steps"]["s1"]["status"] == "running" and meta["steps"]["s1"]["phase"] == "commit"
    ex2, fb2 = executor(run_dir, rr, [])
    assert ex2.resume().status == "done" and fb2.requests == []
    wt = workspace.Path(load_meta(run_dir)["exec"]["worktree"])
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["commit"] == git(wt, "rev-parse", "HEAD").strip()
    assert git(wt, "log", "--format=%s", f"{meta['exec']['base_sha']}..HEAD").splitlines() == ["orch(s1): Do s1"]


def test_an_agent_made_commit_at_the_commit_phase_is_still_refused(env):
    """The recovery above is only for a step orch itself was committing; a normal run still sees a moved HEAD."""
    repo, setup, executor = env
    run_dir, rr = one_step(setup)

    def commits(req):
        (req.cwd / "src").mkdir(exist_ok=True)
        (req.cwd / "src" / "s1.py").write_text("a\n", encoding="utf-8")
        git(req.cwd, "add", "-A")
        git(req.cwd, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m", "orch(s1): Do s1")
        return edit()(req)

    ex, _ = executor(run_dir, rr, [commits])
    out = ex.start()
    assert out.status == "failed" and "git_state_changed" in out.message


def test_rebuild_after_a_crash_in_build_clears_the_dead_attempt(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [boom])
    with pytest.raises(Boom):
        ex.start()
    meta = load_meta(run_dir)
    meta["steps"]["s1"].update(  # what a dead attempt that got far would have left behind
        rounds=[{"n": 1, "reasons": ["tests"], "session": "fresh", "outcome": "ok", "tests": None, "retest": None}],
        rounds_used=2, rounds_total=2, repair_ctx={"reasons": ["review"], "issues": []}, last_failure="tests_r2.txt",
        reviews=[{"round": 1, "verdict": "reject", "summary": "s", "issues": []}], session_id="old", session_entry="claude:opus",
        approved=[{"file": "pyproject.toml", "rule": "new_dependency"}], answers=["keep me"], risk_approved=True,
        phase="build", tokens=500,
    )  # fmt: skip
    write_meta(run_dir, meta)
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})], max_repair_rounds=3)
    assert ex2.resume().status == "done"
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["rounds"] == [] and st["rounds_used"] == 0 and st["rounds_total"] == 0
    assert st["reviews"] == [] and st["approved"] == [] and st["repair_ctx"] is None
    assert st["session_id"] == "s"  # the new builder's, not "old"
    assert st["answers"] == ["keep me"] and st["risk_approved"] is True and st["tokens"] == 600  # costs and answers kept
    sdir = run_dir / "steps" / "s1"
    assert (sdir / "tests.txt").exists() and not (sdir / "tests_r2.txt").exists()  # a fresh build, not a repair round
    assert "keep me" in fb2.requests[0].prompt


def test_rebuild_copies_the_hidden_tests_again(env, tmp_path):
    repo, setup, _ = env
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_hidden.py").write_text("def test_h():\n    assert True\n", encoding="utf-8")
    run_dir, rr = one_step(setup)
    cfg = load_config().model_copy(update={"review": "off", "max_repair_rounds": 0})

    def make(actions):
        fb = FakeBuilder(actions)
        return Executor(rr, run_dir, cfg, lambda n: fb, hidden_tests=hidden), fb

    ex, _ = make([edit({"src/s1.py": "a\n"})])
    real = Executor._run_cmd
    Executor._run_cmd = lambda *a, **k: (_ for _ in ()).throw(Boom("crash in tests"))
    try:
        with pytest.raises(Boom):
            ex.start()
    finally:
        Executor._run_cmd = real
    meta = load_meta(run_dir)
    assert meta["exec"]["hidden_tests"]["step"] == "s1"
    wt = workspace.Path(meta["exec"]["worktree"])
    assert (wt / "tests" / "test_hidden.py").exists()
    meta["steps"]["s1"]["phase"] = "repair"  # as if the process had died inside the repair call
    write_meta(run_dir, meta)
    ex2, fb2 = make([edit({"src/s1.py": "b\n"})])
    assert ex2.resume().status == "done"
    assert len(fb2.requests) == 1
    after = load_meta(run_dir)
    assert after["exec"]["hidden_tests"]["step"] == "s1" and after["exec"]["hidden_tests"]["files"] == ["tests/test_hidden.py"]
    assert (wt / "tests" / "test_hidden.py").exists()  # the reset deleted it; the rebuild copied it back
    assert "tests/test_hidden.py" in git(wt, "show", "--name-only", "--format=", "HEAD")


def test_hidden_record_of_another_step_is_not_cleared_by_a_rebuild(env, tmp_path):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), boom])
    with pytest.raises(Boom):
        ex.start()
    meta = load_meta(run_dir)
    meta["exec"]["hidden_tests"] = {"dir": str(tmp_path), "step": "s1", "files": ["tests/test_x.py"]}
    write_meta(run_dir, meta)
    ex2, _ = executor(run_dir, rr, [edit({"src/s2.py": "b\n"})])
    assert ex2.resume().status == "done"
    assert load_meta(run_dir)["exec"]["hidden_tests"] == {"dir": str(tmp_path), "step": "s1", "files": ["tests/test_x.py"]}
