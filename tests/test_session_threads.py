"""P6.5: what a worker thread may hold from a Session, and a reload() during a run. Fakes only, every wait has a timeout."""

import sqlite3
import threading

import pytest
import yaml
from plan_factory import good_plan, step
from test_executor import PASS_CMD, FakeBuilder, edit, env  # noqa: F401  (env is a fixture)

from orch import services
from orch.config import default_config_path
from orch.session import RunContext, Session

WAIT_S = 30


@pytest.fixture
def world(env, tmp_path):
    repo, setup, _ = env
    data = yaml.safe_load(open(default_config_path().parent / "config.yaml", encoding="utf-8"))
    data.update(review="off", max_repair_rounds=0, repos={"demo": {"path": str(repo), "test_command": PASS_CMD}})
    cfgfile = tmp_path / "session-config.yaml"
    cfgfile.write_text(yaml.safe_dump(data), encoding="utf-8")
    session = Session(config_path=cfgfile, env={}, db_path=tmp_path / "worker.db")
    run_dir, _ = setup(plan=good_plan(steps=[step("s1")]))

    def change_config(**changes):
        cur = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
        cur.update(changes)
        cfgfile.write_text(yaml.safe_dump(cur), encoding="utf-8")

    return session, run_dir, change_config


def _worker(ctx: RunContext, run_dir, adapter, out: dict):
    """The body of a worker thread: it sees only the RunContext, never the Session."""

    def work():
        try:
            ex = services.build_executor(ctx.repo, run_dir, ctx.config, lambda n: adapter, log=lambda m: None, db_path=ctx.db_path)
            out["ex"] = ex
            out["outcome"] = services.drive(ex, "start")
        except BaseException as e:  # reported to the test thread
            out["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def _calls(path) -> int:
    return sqlite3.connect(path).execute("SELECT COUNT(*) FROM calls").fetchone()[0]


def test_a_reload_and_new_overrides_during_a_run_do_not_reach_the_executor(world):
    session, run_dir, change_config = world
    started, release = threading.Event(), threading.Event()
    do_edit = edit({"src/s1.py": "a = 1\n"})

    def blocked(req):
        started.set()
        assert release.wait(WAIT_S), "the test never released the worker"
        return do_edit(req)

    fb = FakeBuilder([blocked])
    ctx = session.snapshot("demo")
    before = ctx.config.model_copy(deep=True)
    out: dict = {}
    t = _worker(ctx, run_dir, fb, out)
    try:
        assert started.wait(WAIT_S), "the worker never reached the agent call"
        # the main thread changes everything it may change, while the run is inside an agent call
        change_config(review="per_step", cooldown_minutes=5, max_repair_rounds=3)
        session.reload()
        session.set_overrides(["builder=opus"])
        assert session.config.cooldown_minutes == 5 and session.config.review == "per_step"
        assert session.config.roles["builder"][0].model == "opus"
        assert out["ex"].cfg == before and out["ex"].cfg is ctx.config  # same object, still the old values
    finally:
        release.set()
        t.join(WAIT_S)
    assert not t.is_alive() and "error" not in out, out.get("error")
    assert out["outcome"].status == "done"
    assert out["ex"].cfg == before
    assert len(fb.requests) == 1 and fb.requests[0].model != "opus"  # no reviewer call, the old builder entry
    assert session.snapshot("demo").config.review == "per_step"  # the next run does see the reload


def test_mutating_a_snapshot_does_not_touch_the_session(world):
    session, _, _ = world
    ctx = session.snapshot("demo")
    ctx.config.cooldown_minutes = 999
    ctx.config.roles["builder"].clear()
    assert session.config.cooldown_minutes != 999 and session.config.roles["builder"]
    with pytest.raises(Exception):
        ctx.config = None  # type: ignore[misc]  (RunContext is frozen)


def test_a_worker_given_only_a_run_context_finishes_a_fake_run(world):
    session, run_dir, _ = world
    ctx = session.snapshot("demo")
    out: dict = {}
    fb = FakeBuilder([edit({"src/s1.py": "a = 1\n"})])
    t = _worker(ctx, run_dir, fb, out)
    t.join(WAIT_S)
    assert not t.is_alive() and "error" not in out, out.get("error")
    assert out["outcome"].status == "done"
    assert _calls(session.db_path) == 1  # logged to the path the context carried
