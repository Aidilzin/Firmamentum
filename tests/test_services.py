"""P5 (D4): the service layer returns data. Fakes only."""

import json
import re
from pathlib import Path

from orch import services
from orch.services import NextAction, next_actions


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
