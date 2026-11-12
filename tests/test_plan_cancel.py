"""P5: Ctrl-C while the planner runs gives the cancelled outcome (exit 130, no traceback). Fakes only."""

import signal
import threading

import pytest
from golden_helpers import check, invoke
from plan_factory import good_plan, step
from test_cancel import cancelled_result
from test_executor import FakeBuilder, env  # noqa: F401  (env is a fixture)
from test_phase4 import write_cfg

from orch import cli, services
from orch.adapters.base import AgentResult, ErrorKind
from orch.config import ResolvedRepo, load_config
from orch.planner import make_plan
from orch.runs import load_meta, load_plan
from orch.services import NextAction

ASK = good_plan(needs_input=True, question="Which database?", steps=[])
ONE = good_plan(steps=[step("s1")])


def planner(structured, tokens=70):
    return lambda req: AgentResult(agent="claude", ok=True, error_kind=ErrorKind.OK, structured=structured,
                                   session_id="p", total_tokens=tokens)  # fmt: skip


def scout_ok(req):
    return AgentResult(agent="agy", ok=True, error_kind=ErrorKind.OK, text="summary", total_tokens=30)


def press():
    """The user presses Ctrl-C: call whatever handler is installed right now."""
    signal.getsignal(signal.SIGINT)(signal.SIGINT, None)


def press_once(req):
    press()
    assert req.cancel is not None and req.cancel.is_set()  # the token reached the request
    return cancelled_result()


def press_twice(req):
    press()
    press()  # raises KeyboardInterrupt here
    raise AssertionError("not reached")


@pytest.fixture
def g(env, tmp_path, monkeypatch):
    repo, _, _ = env
    write_cfg(tmp_path, monkeypatch, repo)

    def fakes(actions):
        fb = FakeBuilder(actions)
        monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
        return fb

    return repo, fakes, lambda name, result: check(name, result, tmp_path)


def only_run(repo):
    (d,) = (repo / ".orch" / "runs").iterdir()
    return d


# ---------- the planner ----------


def _rr(repo):
    return ResolvedRepo(name="demo", path=repo, test_command="pytest -q")


def test_make_plan_stops_at_a_cancelled_call_without_a_retry(env):
    repo, _, _ = env
    token = threading.Event()

    def cancel_now(req):
        token.set()
        return cancelled_result()

    fb = FakeBuilder([cancel_now])
    out = make_plan("x", _rr(repo), load_config(), lambda name: fb, cancel=token)
    assert out.cancelled and out.plan is None and not out.paused and out.error == "planning cancelled by you"
    assert len(fb.requests) == 1 and len(out.attempts) == 1


def test_make_plan_with_a_set_token_calls_nothing(env):
    repo, _, _ = env
    token = threading.Event()
    token.set()
    fb = FakeBuilder([])
    assert make_plan("x", _rr(repo), load_config(), lambda name: fb, cancel=token).cancelled and fb.requests == []
    assert make_plan("x", _rr(repo), load_config(), lambda name: fb, use_scout=True, cancel=token).cancelled and fb.requests == []


def test_make_plan_cancelled_during_the_retry_does_not_call_again(env):
    repo, _, _ = env
    token = threading.Event()

    def cancel_now(req):
        token.set()
        return cancelled_result()

    fb = FakeBuilder([planner(good_plan(steps=[])), cancel_now])  # an invalid plan, then Ctrl-C in the retry
    out = make_plan("x", _rr(repo), load_config(), lambda name: fb, cancel=token)
    assert out.cancelled and len(fb.requests) == 2 and out.total_tokens == 70


def test_make_plan_cancelled_during_the_scout_never_calls_the_planner(env):
    repo, _, _ = env
    token = threading.Event()

    def cancel_now(req):
        token.set()
        return cancelled_result()

    fb = FakeBuilder([cancel_now])
    out = make_plan("x", _rr(repo), load_config(), lambda name: fb, use_scout=True, cancel=token)
    assert out.cancelled and len(fb.requests) == 1 and out.attempts == []


# ---------- the services ----------


def test_plan_run_cancelled_is_stored_as_failed_and_offers_to_plan_again(env):
    repo, _, _ = env
    token = threading.Event()
    token.set()
    r = services.plan_run('make "slugify"\nkeep digits', _rr(repo), load_config(), lambda name: FakeBuilder([]), cancel=token)
    assert (r.state, r.exit_code) == ("cancelled", 130)
    meta = load_meta(r.run_dir)
    assert meta["status"] == "failed" and meta["error"] == "planning cancelled by you"
    (again,) = r.next
    assert again.kind == "plan_again" and again.text == 'make "slugify"\nkeep digits'
    assert str(again) == 'orch plan "make \\"slugify\\" keep digits" --repo demo   # planning was cancelled; this starts a new run'
    assert services.next_actions(meta, r.run_dir, "demo") == []  # status and runs offer nothing for it


def test_plan_again_without_a_repo_name_has_no_repo_flag():
    a = NextAction("plan_again", "r1", reason="why", text="do it")
    assert a.command == 'orch plan "do it"' and str(a) == 'orch plan "do it"   # why'


def test_a_raw_interrupt_inside_the_planner_still_stores_the_run(env):
    repo, _, _ = env

    def boom(req):
        raise KeyboardInterrupt

    token = threading.Event()
    r = services.plan_run("x", _rr(repo), load_config(), lambda name: FakeBuilder([boom]), cancel=token)
    assert r.state == "cancelled" and token.is_set() and load_meta(r.run_dir)["status"] == "failed"


# ---------- the command line ----------


def test_plan_first_ctrl_c_cancels_and_exits_130(g):
    repo, fakes, golden = g
    before = signal.getsignal(signal.SIGINT)
    fb = fakes([press_once])
    r = invoke(["plan", "make slugify keep digits", "--repo", "demo"])
    golden("plan_cancelled", r)
    assert r.exit_code == 130 and len(fb.requests) == 1
    assert signal.getsignal(signal.SIGINT) is before  # restored
    meta = load_meta(only_run(repo))
    assert meta["status"] == "failed" and meta["error"] == "planning cancelled by you"


def test_plan_second_ctrl_c_stops_now_without_a_traceback(g):
    repo, fakes, golden = g
    fakes([press_twice])
    r = invoke(["plan", "make slugify keep digits", "--repo", "demo"])
    assert r.exit_code == 130 and "Traceback" not in r.output and "planning cancelled by you" in r.output
    assert 'Next: orch plan "make slugify keep digits" --repo demo' in r.output
    assert load_meta(only_run(repo))["status"] == "failed"  # the run folder still has a meta.json


def test_plan_ctrl_c_during_the_scout_never_calls_the_planner(g):
    repo, fakes, golden = g
    fb = fakes([press_once])
    r = invoke(["plan", "x", "--repo", "demo", "--scout"])
    assert r.exit_code == 130 and len(fb.requests) == 1 and load_meta(only_run(repo))["status"] == "failed"


def test_run_ctrl_c_while_planning_never_builds_an_executor(g, monkeypatch):
    repo, fakes, golden = g
    fb = fakes([press_once])
    monkeypatch.setattr(services, "build_executor", lambda *a, **k: pytest.fail("the executor must not be built"))
    r = invoke(["run", "make slugify keep digits", "--repo", "demo"])
    golden("run_planning_cancelled", r)
    assert r.exit_code == 130 and len(fb.requests) == 1 and "steps" not in load_meta(only_run(repo))


def test_run_after_a_finished_plan_still_cancels_the_build_with_ctrl_c(g):
    repo, fakes, golden = g
    fakes([planner(ONE), press_once])  # Ctrl-C arrives in the builder: the executor's own handling
    r = invoke(["run", "do it", "--repo", "demo"])
    assert r.exit_code == 130 and "Result: cancelled (s1)" in r.output
    assert load_meta(only_run(repo))["status"] == "cancelled"


def test_answer_ctrl_c_while_replanning_keeps_needs_input(g):
    repo, fakes, golden = g
    fb = fakes([planner(ASK), press_once, planner(ONE)])
    assert invoke(["plan", "add storage", "--repo", "demo"]).exit_code == 2
    run_dir = only_run(repo)
    r = invoke(["answer", run_dir.name, "sqlite", "--repo", "demo"])
    golden("answer_plan_cancelled", r)
    assert r.exit_code == 130 and load_meta(run_dir)["status"] == "needs_input" and load_plan(run_dir).needs_input
    assert "## Clarification" not in (run_dir / "request.md").read_text(encoding="utf-8")
    assert invoke(["answer", run_dir.name, "sqlite", "--repo", "demo"]).exit_code == 0  # and it can be answered again
    assert len(fb.requests) == 3
