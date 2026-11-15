"""P6.2: an explicit usage-DB path reaches the router from planning and from the executor. Fakes only."""

import sqlite3

from plan_factory import good_plan, step
from test_executor import FakeBuilder, edit, env  # noqa: F401  (env is a fixture)
from test_plan_cancel import ONE, planner

from orch import budget, services
from orch.config import ResolvedRepo, load_config


def _cfg():
    return load_config().model_copy(update={"review": "off", "max_repair_rounds": 0})


def _calls(path) -> int:
    return sqlite3.connect(path).execute("SELECT COUNT(*) FROM calls").fetchone()[0]


def test_plan_run_logs_to_the_given_db_and_not_the_default(env, tmp_path):
    repo, _, _ = env
    mine = tmp_path / "mine" / "usage.db"
    fb = FakeBuilder([planner(ONE)])
    rr = ResolvedRepo(name="demo", path=repo, test_command="pytest -q")
    r = services.plan_run("x", rr, _cfg(), lambda name: fb, db_path=mine)
    assert r.outcome.plan is not None
    assert _calls(mine) == 1
    assert not budget.default_db_path().exists()


def test_answer_plan_and_scout_log_to_the_given_db(env, tmp_path):
    repo, _, _ = env
    mine = tmp_path / "usage-mine.db"
    rr = ResolvedRepo(name="demo", path=repo, test_command="pytest -q")
    from test_plan_cancel import ASK, scout_ok

    fb = FakeBuilder([scout_ok, planner(ASK)])
    r = services.plan_run("x", rr, _cfg(), lambda name: fb, scout=True, db_path=mine)
    assert _calls(mine) == 2  # scout + planner
    fb2 = FakeBuilder([planner(ONE)])
    services.answer_plan(rr, _cfg(), r.run_dir, "postgres", lambda name: fb2, db_path=mine)
    assert _calls(mine) == 3
    assert not budget.default_db_path().exists()


def test_executor_logs_to_the_given_db(env, tmp_path):
    repo, setup, _ = env
    mine = tmp_path / "usage-exec.db"
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    fb = FakeBuilder([edit({"src/s1.py": "a = 1\n"})])
    ex = services.build_executor(rr, run_dir, _cfg(), lambda name: fb, log=lambda m: None, db_path=mine)
    assert services.drive(ex, "start").status == "done"
    assert _calls(mine) >= 1
    assert not budget.default_db_path().exists()
