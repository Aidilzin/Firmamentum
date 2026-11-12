"""P5 (D4): the service layer returns data. Fakes only."""

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from plan_factory import good_plan, step
from test_executor import FakeBuilder, edit, env  # noqa: F401  (env is a fixture)

from orch import services
from orch.config import load_config
from orch.services import NextAction, next_actions


def CFG():
    return load_config().model_copy(update={"review": "off", "max_repair_rounds": 0})


def _run(tmp_path, meta):
    d = tmp_path / "r1"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"run_id": "r1", **meta}), encoding="utf-8")
    return d, meta


def test_services_never_prints_and_never_touches_typer():
    src = Path(services.__file__).read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    for banned in (r"\btyper\b", r"\bprint\(", r"\becho\(", r"sys\.stdout", r"sys\.stderr", r"\bConsole\("):
        assert not re.search(banned, code), banned


def test_next_actions_are_data_with_a_command_and_an_alternative(tmp_path):
    d, meta = _run(tmp_path, {"status": "awaiting_approval", "steps": {
        "s1": {"title": "T", "status": "awaiting_approval", "rule": "new_dependency"},
        "s2": {"title": "T", "status": "failed", "error": "boom"},
    }})  # fmt: skip
    approve, failed = next_actions(meta, d, "demo")
    assert (approve.kind, approve.run_id, approve.step, approve.reason, approve.repo) == ("approve", "r1", "s1", "new_dependency", "demo")
    assert approve.command == "orch approve r1 --step s1 --repo demo"
    assert approve.alt == NextAction("reject", "r1", "s1", repo="demo") and approve.alt.command == "orch reject r1 --step s1 --repo demo"
    assert failed.kind == "step_failed" and failed.command is None and failed.path == d / "steps" / "s2"
    assert str(failed) == f"(step s2 failed: boom; see {d / 'steps' / 's2'}; start a new run)"


def test_a_done_run_offers_diff_then_merge(tmp_path):
    d, meta = _run(tmp_path, {"status": "done", "steps": {"s1": {"title": "T", "status": "done", "commit": "abc"}}})
    (diff,) = next_actions(meta, d)
    assert diff.kind == "diff_run" and diff.alt.kind == "merge" and diff.alt.command == "orch merge r1"
    assert str(diff) == "orch diff r1   # review, then: orch merge r1"


def test_a_bare_resume_has_no_comment_and_a_missing_rule_prints_none(tmp_path):
    assert str(NextAction("resume", "r1", repo="demo")) == "orch resume r1 --repo demo"
    assert str(NextAction("resume", "r1", reason="stopped by you")) == "orch resume r1   # stopped by you"
    assert str(NextAction("answer_step", "r1", "s1")) == 'orch answer r1 --step s1 "<your answer>"   # None'
    assert str(NextAction("answer_plan", "r1", reason="the plan needs an answer")) == 'orch answer r1 "<your answer>"   # the plan needs an answer'


def test_exit_codes_cover_every_outcome():
    assert services.EXIT_CODES == {"done": 0, "failed": 1, "needs_input": 2, "awaiting_approval": 2, "paused": 3,
                                   "rejected": 1, "cancelled": 130}  # fmt: skip


# ---------- drive ----------


def test_drive_returns_the_outcome_and_turns_an_exec_error_into_a_service_error(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    fb = FakeBuilder([edit({"src/s1.py": "a = 1\n"})])
    logs = []
    ex = services.build_executor(rr, run_dir, CFG(), lambda name: fb, log=logs.append)
    out = services.drive(ex, "start")
    assert out.status == "done" and logs and len(fb.requests) == 1
    again = services.build_executor(rr, run_dir, CFG(), lambda name: fb, log=logs.append)
    with pytest.raises(services.ServiceError, match="already started") as e:
        services.drive(again, "start")
    assert e.value.code == 2
    with pytest.raises(ValueError):
        services.drive(again, "explode")


def test_build_executor_passes_the_callers_cancel_token(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    fb = FakeBuilder([])
    token = threading.Event()
    token.set()
    ex = services.build_executor(rr, run_dir, CFG(), lambda name: fb, log=lambda m: None, cancel=token)
    assert services.drive(ex, "start").status == "cancelled" and fb.requests == []


def test_resume_blocked_until_only_for_a_paused_run_in_the_future():
    now = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
    later = (now + timedelta(hours=1)).isoformat()
    assert services.resume_blocked_until({"status": "paused", "resume_after": later}, now=now) == now + timedelta(hours=1)
    assert services.resume_blocked_until({"status": "paused", "resume_after": later}, force=True, now=now) is None
    assert services.resume_blocked_until({"status": "failed", "resume_after": later}, now=now) is None
    assert services.resume_blocked_until({"status": "paused", "resume_after": (now - timedelta(minutes=1)).isoformat()}, now=now) is None
    assert services.resume_blocked_until({"status": "paused"}, now=now) is None
