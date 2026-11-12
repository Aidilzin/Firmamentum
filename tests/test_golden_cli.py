"""Golden output of the one-shot commands (P5): the service-layer move must not change a byte. Fakes only."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from golden_helpers import check, invoke
from plan_factory import good_plan, step
from test_cancel import cancels
from test_executor import FILE_CMD, FakeBuilder, edit, env, fail, report  # noqa: F401  (env is a fixture)
from test_phase4 import write_cfg

from orch import budget, cli
from orch.runs import load_meta, write_meta

ENV_STEP = step("s1", files=["src/s1.py", ".env"])


@pytest.fixture
def g(env, tmp_path, monkeypatch):
    """(repo, setup, executor, golden): a temp repo with the `demo` config, and `golden(name, result)`."""
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)
    return repo, setup, executor, lambda name, result: check(name, result, tmp_path)


def one_step(setup, **kw):
    return setup(plan=good_plan(steps=[step("s1")]), **kw)


# ---------- the Next: lines, one literal per branch ----------


def _run(tmp_path, meta, lock=False):
    d = tmp_path / "r1"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"run_id": "r1", **meta}), encoding="utf-8")
    return d


def S(status, **kw):
    return {"title": "T", "status": status, **kw}


def test_next_line_for_a_plan_that_needs_an_answer(tmp_path):
    d = _run(tmp_path, {"status": "needs_input"})
    assert cli.next_commands(d, "demo") == ['orch answer r1 "<your answer>" --repo demo   # the plan needs an answer']
    assert cli.next_commands(d) == ['orch answer r1 "<your answer>"   # the plan needs an answer']


def test_next_lines_for_every_stopped_step_kind(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    d = _run(tmp_path, {"status": "awaiting_approval", "exec": {"worktree": str(wt)}, "steps": {
        "s1": S("awaiting_approval", stage="repairs_exhausted", rule="tests_fail_after_3_repairs"),
        "s2": S("awaiting_approval", rule="new_dependency"),
        "s3": S("awaiting_approval"),
        "s4": S("needs_input", question="Which db?"),
        "s5": S("failed", error="boom"),
        "s6": S("pending", error="rate limited"),
        "s7": S("pending", error="cancelled"),
        "s8": S("done", commit="abc"),
        "s9": S("failed", error="late", commit="def"),
    }})  # fmt: skip
    assert cli.next_commands(d, "demo") == [
        "orch approve r1 --step s1 --retry --repo demo   # another set of repair rounds  (or: orch reject r1 --step s1 --repo demo)",
        "orch approve r1 --step s2 --repo demo   # new_dependency  (or: orch reject r1 --step s2 --repo demo)",
        "orch approve r1 --step s3 --repo demo   # None  (or: orch reject r1 --step s3 --repo demo)",
        'orch answer r1 --step s4 "<your answer>" --repo demo   # Which db?',
        f"(step s5 failed: boom; see {d / 'steps' / 's5'}; start a new run)",
        "orch resume r1 --repo demo   # after the usage limit resets",
        "orch resume r1 --repo demo   # stopped by you",
        f"(step s9 failed: late; see {d / 'steps' / 's9'}; start a new run)",
        "orch diff r1 --step s1 --repo demo   # the uncommitted change of this step",
        "orch diff r1 --step s2 --repo demo   # the uncommitted change of this step",
        "orch diff r1 --step s3 --repo demo   # the uncommitted change of this step",
        "orch diff r1 --step s4 --repo demo   # the uncommitted change of this step",
        "orch diff r1 --step s5 --repo demo   # the uncommitted change of this step",
    ]
    no_repo = cli.next_commands(d)
    assert no_repo[0] == "orch approve r1 --step s1 --retry   # another set of repair rounds  (or: orch reject r1 --step s1)"
    assert no_repo[3] == 'orch answer r1 --step s4 "<your answer>"   # Which db?'
    assert no_repo[-1] == "orch diff r1 --step s5   # the uncommitted change of this step"


def test_next_lines_without_a_worktree_have_no_diff_hint(tmp_path):
    d = _run(tmp_path, {"status": "needs_input", "exec": {"worktree": str(tmp_path / "gone")},
                        "steps": {"s1": S("needs_input", question=None)}})  # fmt: skip
    assert cli.next_commands(d, "demo") == ['orch answer r1 --step s1 "<your answer>" --repo demo   # None']


@pytest.mark.parametrize("status", ["paused", "cancelled"])
def test_next_line_for_a_paused_or_cancelled_run_with_no_step_hint_is_a_bare_resume(tmp_path, status):
    d = _run(tmp_path, {"status": status, "steps": {"s1": S("done", commit="abc"), "s2": S("pending")}})
    assert cli.next_commands(d, "demo") == ["orch resume r1 --repo demo"]
    assert cli.next_commands(d) == ["orch resume r1"]


def test_next_line_for_a_paused_run_with_a_step_hint_is_not_repeated(tmp_path):
    d = _run(tmp_path, {"status": "paused", "steps": {"s1": S("pending", error="rate limited")}})
    assert cli.next_commands(d, "demo") == ["orch resume r1 --repo demo   # after the usage limit resets"]


def test_next_line_for_a_run_that_died_mid_step(tmp_path):
    d = _run(tmp_path, {"status": "executing", "steps": {"s1": S("running")}})  # nobody holds the run lock
    assert cli.next_commands(d, "demo") == ["orch resume r1 --repo demo   # the run died mid-step"]


def test_next_line_for_a_done_run_is_diff_then_merge_and_nothing_once_merged(tmp_path):
    d = _run(tmp_path, {"status": "done", "steps": {"s1": S("done", commit="abc")}})
    assert cli.next_commands(d, "demo") == ["orch diff r1 --repo demo   # review, then: orch merge r1 --repo demo"]
    assert cli.next_commands(d) == ["orch diff r1   # review, then: orch merge r1"]
    write_meta(d, {**load_meta(d), "merged_sha": "abc"})
    assert cli.next_commands(d, "demo") == []


def test_next_lines_for_a_planned_run_are_empty(tmp_path):
    assert cli.next_commands(_run(tmp_path, {"status": "planned"}), "demo") == []


# ---------- orch status ----------


def _status(g, name, run_dir):
    g[3](name, invoke(["status", run_dir.name, "--repo", "demo"]))


def test_status_not_started(g):
    run_dir, _ = g[1]()
    _status(g, "status_not_started", run_dir)


def test_status_done(g):
    run_dir, rr = g[1]()
    ex, _ = g[2](run_dir, rr, [edit({"src/s1.py": "a = 1\n"}), edit({"src/s2.py": "b = 2\n"})])
    assert ex.start().status == "done"
    _status(g, "status_done", run_dir)


def test_status_awaiting_approval_for_a_rule(g):
    run_dir, rr = g[1](plan=good_plan(steps=[ENV_STEP]))
    ex, _ = g[2](run_dir, rr, [edit({"src/s1.py": "a = 1\n", ".env": "K=1\n"})])
    assert ex.start().status == "awaiting_approval"
    _status(g, "status_awaiting_approval", run_dir)


def test_status_repairs_exhausted(g):
    run_dir, rr = one_step(g[1], test_command=FILE_CMD)
    ex, _ = g[2](run_dir, rr, [edit({"a.py": "1\n"})])
    assert ex.start().status == "awaiting_approval"
    _status(g, "status_repairs_exhausted", run_dir)


def test_status_needs_input(g):
    run_dir, rr = one_step(g[1])
    ex, _ = g[2](run_dir, rr, [edit(rep=report(blocked=True, question="Which db?"))])
    assert ex.start().status == "needs_input"
    _status(g, "status_needs_input", run_dir)


def test_status_failed(g):
    run_dir, rr = one_step(g[1])
    ex, _ = g[2](run_dir, rr, [fail()])
    assert ex.start().status == "failed"
    _status(g, "status_failed", run_dir)


def test_status_paused(g):
    run_dir, rr = one_step(g[1])
    budget.record_cooldown("claude", "*", datetime.now(timezone.utc) + timedelta(hours=2), "5h limit")
    ex, _ = g[2](run_dir, rr, [])
    assert ex.start().status == "paused"
    _status(g, "status_paused", run_dir)


def test_status_cancelled(g):
    run_dir, rr = one_step(g[1])
    h = {}
    ex, _ = g[2](run_dir, rr, [cancels(h)])
    h["ex"] = ex
    assert ex.start().status == "cancelled"
    _status(g, "status_cancelled", run_dir)


def test_status_stale(g):
    run_dir, rr = one_step(g[1])
    ex, _ = g[2](run_dir, rr, [edit(rep=report(blocked=True, question="Which db?"))])
    ex.start()
    meta = load_meta(run_dir)
    meta["status"] = "executing"
    meta["steps"]["s1"]["status"] = "running"
    write_meta(run_dir, meta)
    _status(g, "status_stale", run_dir)


def test_status_of_an_unknown_run(g):
    g[3]("status_unknown_run", invoke(["status", "nope", "--repo", "demo"]))
