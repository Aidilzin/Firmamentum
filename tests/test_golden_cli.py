"""Golden output of the one-shot commands (P5): the service-layer move must not change a byte. Fakes only."""

import json
import signal
from datetime import datetime, timedelta, timezone

import pytest
from golden_helpers import check, invoke
from plan_factory import good_plan, step
from test_cancel import cancelled_result, cancels
from test_executor import FILE_CMD, FakeBuilder, edit, env, fail, report  # noqa: F401  (env is a fixture)
from test_phase4 import issue, review, write_cfg

from orch import budget, cli
from orch.adapters.base import AgentResult, ErrorKind
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


# ---------- orch plan / run / answer (plan) ----------

ASK = good_plan(needs_input=True, question="Which database?", steps=[])
ONE = good_plan(steps=[step("s1")])


def planner(structured, tokens=70, session="p"):
    return lambda req: AgentResult(agent="claude", ok=True, error_kind=ErrorKind.OK, structured=structured,
                                   session_id=session, total_tokens=tokens)  # fmt: skip


def scout(text="A small Python package with a src layout.", tokens=30):
    return lambda req: AgentResult(agent="agy", ok=True, error_kind=ErrorKind.OK, text=text, total_tokens=tokens)


def fakes(monkeypatch, actions):
    fb = FakeBuilder(actions)
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    return fb


def cool_claude():
    budget.record_cooldown("claude", "*", datetime.now(timezone.utc) + timedelta(hours=2), "5h limit")


def only_run(repo):
    (d,) = (repo / ".orch" / "runs").iterdir()
    return d


def test_plan_planned(g, monkeypatch):
    fakes(monkeypatch, [planner(good_plan(), tokens=1234)])
    g[3]("plan_planned", invoke(["plan", "Add a version command", "--repo", "demo"]))


def test_plan_with_scout_and_model_overrides(g, monkeypatch):
    fb = fakes(monkeypatch, [scout(), planner(good_plan())])
    g[3]("plan_scout", invoke(["plan", "x y z", "--repo", "demo", "--scout", "--model", "planner=sonnet"]))
    assert len(fb.requests) == 2


def test_plan_needs_input(g, monkeypatch):
    fakes(monkeypatch, [planner(ASK)])
    g[3]("plan_needs_input", invoke(["plan", "add storage", "--repo", "demo"]))


def test_plan_failed_twice(g, monkeypatch):
    fb = fakes(monkeypatch, [planner(good_plan(steps=[])), planner(good_plan(steps=[]))])
    g[3]("plan_failed", invoke(["plan", "bad one", "--repo", "demo"]))
    assert len(fb.requests) == 2


def test_plan_agent_error(g, monkeypatch):
    fakes(monkeypatch, [fail()])
    g[3]("plan_agent_error", invoke(["plan", "bad one", "--repo", "demo"]))


def test_plan_paused(g, monkeypatch):
    fb = fakes(monkeypatch, [])
    cool_claude()
    g[3]("plan_paused", invoke(["plan", "later", "--repo", "demo"]))
    assert fb.requests == []


def test_plan_bad_model_and_bad_repo(g, monkeypatch):
    fb = fakes(monkeypatch, [])
    g[3]("plan_bad_model", invoke(["plan", "x", "--repo", "demo", "--model", "nobody=x"]))
    g[3]("plan_bad_repo", invoke(["plan", "x", "--repo", "no-such-repo-or-dir"]))
    assert fb.requests == []


def test_run_plans_and_executes_to_done(g, monkeypatch):
    fb = fakes(monkeypatch, [planner(ONE), edit({"src/s1.py": "a = 1\n"})])
    g[3]("run_done", invoke(["run", "do it", "--repo", "demo"]))
    assert len(fb.requests) == 2 and load_meta(only_run(g[0]))["status"] == "done"


def test_run_stops_at_a_step(g, monkeypatch):
    fakes(monkeypatch, [planner(good_plan(steps=[ENV_STEP])), edit({"src/s1.py": "a = 1\n", ".env": "K=1\n"})])
    g[3]("run_awaiting_approval", invoke(["run", "do it", "--repo", "demo"]))


def test_run_plan_needs_input(g, monkeypatch):
    fb = fakes(monkeypatch, [planner(ASK)])
    g[3]("run_needs_input", invoke(["run", "add storage", "--repo", "demo"]))
    assert len(fb.requests) == 1


def test_run_planning_failed(g, monkeypatch):
    fakes(monkeypatch, [fail()])
    g[3]("run_planning_failed", invoke(["run", "bad one", "--repo", "demo"]))


def test_run_planning_paused(g, monkeypatch):
    fakes(monkeypatch, [])
    cool_claude()
    g[3]("run_planning_paused", invoke(["run", "later", "--repo", "demo"]))


def test_run_refuses_a_bad_hidden_tests_dir_before_any_call(g, monkeypatch):
    fb = fakes(monkeypatch, [])
    g[3]("run_bad_hidden_tests", invoke(["run", "do it", "--repo", "demo", "--hidden-tests", str(g[0])]))
    assert fb.requests == [] and not (g[0] / ".orch").exists()


def test_run_refuses_a_repo_without_a_test_command(g, tmp_path, monkeypatch):
    other = tmp_path / "unconfigured"
    other.mkdir()
    fb = fakes(monkeypatch, [])
    g[3]("run_no_test_command", invoke(["run", "do it", "--repo", str(other)]))
    assert fb.requests == [] and not (other / ".orch").exists()


def _asked(g, monkeypatch, then):
    """A stored plan that needs an answer; `then` are the planner's next replies."""
    fb = fakes(monkeypatch, [planner(ASK), *then])
    assert invoke(["plan", "add storage", "--repo", "demo"]).exit_code == 2
    return fb, only_run(g[0])


def test_answer_plan_gives_a_plan(g, monkeypatch):
    fb, run_dir = _asked(g, monkeypatch, [planner(ONE)])
    g[3]("answer_plan_planned", invoke(["answer", run_dir.name, "sqlite", "--repo", "demo"]))
    assert load_meta(run_dir)["status"] == "planned" and "Answer: sqlite" in fb.requests[1].prompt


def test_answer_plan_asks_again(g, monkeypatch):
    again = good_plan(needs_input=True, question="Which sqlite driver?", steps=[])
    _, run_dir = _asked(g, monkeypatch, [planner(again)])
    g[3]("answer_plan_needs_input", invoke(["answer", run_dir.name, "sqlite", "--repo", "demo"]))


def test_answer_plan_replan_fails(g, monkeypatch):
    _, run_dir = _asked(g, monkeypatch, [fail()])
    g[3]("answer_plan_failed", invoke(["answer", run_dir.name, "sqlite", "--repo", "demo"]))


def test_answer_plan_replan_is_rate_limited(g, monkeypatch):
    _, run_dir = _asked(g, monkeypatch, [])
    cool_claude()
    g[3]("answer_plan_paused", invoke(["answer", run_dir.name, "sqlite", "--repo", "demo"]))


def test_answer_plan_that_is_not_waiting(g, monkeypatch):
    fb, run_dir = _asked(g, monkeypatch, [])
    (run_dir / "plan.json").write_text(json.dumps(ONE), encoding="utf-8")  # meta still says needs_input
    g[3]("answer_plan_not_waiting", invoke(["answer", run_dir.name, "sqlite", "--repo", "demo"]))
    assert len(fb.requests) == 1


# ---------- exec / resume / approve / reject / answer --step ----------


def _exec(run_dir, *extra):
    return invoke(["exec", run_dir.name, "--repo", "demo", *extra])


def test_exec_done(g, monkeypatch):
    run_dir, _ = g[1]()
    fakes(monkeypatch, [edit({"src/s1.py": "a = 1\n"}), edit({"src/s2.py": "b = 2\n"})])
    g[3]("exec_done", _exec(run_dir))
    g[3]("exec_already_started", _exec(run_dir))


def test_exec_with_review_and_a_repair_round(g, tmp_path, monkeypatch):
    write_cfg(tmp_path, monkeypatch, g[0], review="per_step", max_repair_rounds=3)
    run_dir, _ = one_step(g[1])
    fakes(monkeypatch, [edit({"src/s1.py": "a = 1\n"}), review("reject", [issue("major", problem="off by one")]),
                        edit({"src/s1.py": "a = 2\n"}), review("approve", [issue("minor", problem="naming")])])  # fmt: skip
    g[3]("exec_review_repair", _exec(run_dir))


def test_exec_awaiting_approval_then_approve(g, monkeypatch):
    run_dir, _ = g[1](plan=good_plan(steps=[ENV_STEP]))
    fakes(monkeypatch, [edit({"src/s1.py": "a = 1\n", ".env": "K=1\n"})])
    g[3]("exec_awaiting_approval", _exec(run_dir))
    g[3]("approve_done", invoke(["approve", run_dir.name, "--repo", "demo"]))
    g[3]("approve_nothing_waiting", invoke(["approve", run_dir.name, "--repo", "demo"]))


def test_exec_awaiting_approval_then_reject(g, monkeypatch):
    run_dir, _ = g[1](plan=good_plan(steps=[ENV_STEP]))
    fakes(monkeypatch, [edit({"src/s1.py": "a = 1\n", ".env": "K=1\n"})])
    assert _exec(run_dir).exit_code == 2
    g[3]("reject", invoke(["reject", run_dir.name, "--step", "s1", "--repo", "demo"]))


def test_exec_repairs_exhausted_then_approve_retry(g, tmp_path, monkeypatch):
    write_cfg(tmp_path, monkeypatch, g[0], test_command=FILE_CMD)
    run_dir, _ = one_step(g[1])
    fakes(monkeypatch, [edit({"src/s1.py": "a = 1\n"})])
    g[3]("exec_repairs_exhausted", _exec(run_dir))
    g[3]("approve_retry", invoke(["approve", run_dir.name, "--step", "s1", "--retry", "--repo", "demo"]))


def test_exec_needs_input_then_answer_step(g, monkeypatch):
    run_dir, _ = one_step(g[1])
    fb = fakes(monkeypatch, [edit(rep=report(blocked=True, question="Which db?")), edit({"src/s1.py": "db = 1\n"})])
    g[3]("exec_needs_input", _exec(run_dir))
    g[3]("answer_step_done", invoke(["answer", run_dir.name, "use sqlite", "--step", "s1", "--repo", "demo"]))
    assert "use sqlite" in fb.requests[1].prompt


def test_answer_for_a_run_that_waits_for_nothing(g, monkeypatch):
    run_dir, _ = one_step(g[1])
    fb = fakes(monkeypatch, [])
    g[3]("answer_nothing_waiting", invoke(["answer", run_dir.name, "use sqlite", "--repo", "demo"]))
    assert fb.requests == []


def test_exec_failed(g, monkeypatch):
    run_dir, _ = one_step(g[1])
    fakes(monkeypatch, [fail()])
    g[3]("exec_failed", _exec(run_dir))


def test_exec_unknown_run(g, monkeypatch):
    fakes(monkeypatch, [])
    g[3]("exec_unknown_run", invoke(["exec", "nope", "--repo", "demo"]))


def test_exec_paused_then_resume(g, monkeypatch):
    run_dir, _ = one_step(g[1])
    fb = fakes(monkeypatch, [edit({"src/s1.py": "a = 1\n"})])
    cool_claude()
    g[3]("exec_paused", _exec(run_dir))
    g[3]("resume_refused_until", invoke(["resume", run_dir.name, "--repo", "demo"]))
    g[3]("resume_force_still_paused", invoke(["resume", run_dir.name, "--repo", "demo", "--force"]))
    assert fb.requests == []
    budget.clear_cooldowns()
    g[3]("resume_force_done", invoke(["resume", run_dir.name, "--repo", "demo", "--force"]))
    assert len(fb.requests) == 1


def test_exec_cancelled_by_ctrl_c_then_resume(g, monkeypatch):
    run_dir, _ = one_step(g[1])

    class Presses(FakeBuilder):
        def run(self, request):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)  # the user presses Ctrl-C once
            return cancelled_result()

    fb = Presses([])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    g[3]("exec_cancelled", _exec(run_dir))
    fakes(monkeypatch, [edit({"src/s1.py": "a = 1\n"})])
    g[3]("resume_after_cancel_done", invoke(["resume", run_dir.name, "--repo", "demo"]))
