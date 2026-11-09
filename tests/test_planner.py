"""Planner flow with FakeAdapter only."""

import json

from plan_factory import good_plan, result, step

from orch.adapters.fake import FakeAdapter
from orch.config import ResolvedRepo, apply_overrides, load_config
from orch.planner import extract_json, make_plan, planner_instructions

CFG = load_config()


def repo(tmp_path, tc="uv run pytest -q"):
    return ResolvedRepo(name="demo", path=tmp_path, test_command=tc)


def run(tmp_path, results, scout=False, cfg=CFG):
    fakes = {}

    def factory(name):
        return fakes.setdefault(name, FakeAdapter(list(results[name]) if isinstance(results, dict) else list(results),
                                                  agent_name=name))

    return make_plan("add a thing", repo(tmp_path), cfg, factory, use_scout=scout), fakes


def test_first_try_good(tmp_path):
    out, fakes = run(tmp_path, [result(good_plan())])
    assert out.plan and out.error is None and len(out.attempts) == 1
    req = fakes["claude"].requests[0]
    assert req.model == "opus" and req.cwd == tmp_path
    assert req.json_schema["title"] == "Plan"
    assert req.extra_args[req.extra_args.index("--tools") + 1] == "Read,Grep,Glob"
    assert req.extra_args[req.extra_args.index("--permission-prompts") + 1] == "none"
    # role instructions go by flag for claude, not into the prompt
    i = req.extra_args.index("--append-system-prompt")
    assert req.extra_args[i + 1] == planner_instructions()
    assert "Planner (Laplace)" not in req.prompt
    assert "add a thing" in req.prompt and "uv run pytest -q" in req.prompt and "new_dependency" in req.prompt


def test_retry_bad_then_good_resumes_session(tmp_path):
    bad = good_plan(steps=[step("s1", deps=["s9"])])
    out, fakes = run(tmp_path, [result(bad, session_id="abc"), result(good_plan())])
    assert out.plan and len(out.attempts) == 2
    assert out.attempts[0].validation_errors and "unknown step 's9'" in out.attempts[0].validation_errors[0]
    first, second = fakes["claude"].requests
    assert second.resume_id == "abc"
    assert "unknown step 's9'" in second.prompt
    assert second.extra_args == first.extra_args  # still read-only on the retry


def test_retry_without_session_includes_previous_output(tmp_path):
    bad = good_plan(steps=[])
    out, fakes = run(tmp_path, [result(bad, session_id=None), result(good_plan())])
    assert out.plan
    second = fakes["claude"].requests[1]
    assert second.resume_id is None
    assert "Previous attempt" in second.prompt and "at least one step" in second.prompt
    assert "add a thing" in second.prompt


def test_retry_falls_back_when_resume_call_fails(tmp_path):
    bad = good_plan(steps=[])
    out, fakes = run(tmp_path, [result(bad, session_id="abc"), result(text="boom", ok=False), result(good_plan())])
    assert out.plan
    reqs = fakes["claude"].requests
    assert len(reqs) == 3 and reqs[1].resume_id == "abc" and reqs[2].resume_id is None


def test_gives_up_after_one_retry(tmp_path):
    bad = good_plan(steps=[])
    out, fakes = run(tmp_path, [result(bad), result(bad)])
    assert out.plan is None and "invalid plan twice" in out.error
    assert len(fakes["claude"].requests) == 2
    assert out.total_tokens == 200


def test_agent_failure_is_not_retried(tmp_path):
    out, fakes = run(tmp_path, [result(text="rate limited", ok=False)])
    assert out.plan is None and "planner failed" in out.error
    assert len(fakes["claude"].requests) == 1


def test_text_json_in_fence_is_accepted_when_no_structured(tmp_path):
    out, _ = run(tmp_path, [result(text="```json\n" + json.dumps(good_plan()) + "\n```")])
    assert out.plan


def test_non_json_text_triggers_retry(tmp_path):
    out, _ = run(tmp_path, [result(text="Sure! Here is a plan."), result(good_plan())])
    assert out.plan and "not valid JSON" in out.attempts[0].validation_errors[0]


def test_needs_input_plan_is_returned(tmp_path):
    p = good_plan(needs_input=True, question="Which db?", steps=[])
    out, _ = run(tmp_path, [result(p)])
    assert out.plan.needs_input and out.plan.question == "Which db?"


def test_scout_runs_first_on_agy_and_feeds_planner(tmp_path):
    out, fakes = run(
        tmp_path,
        {"agy": [result(text="Repo is a python CLI.", agent="agy", tokens=50)], "claude": [result(good_plan())]},
        scout=True,
    )
    assert out.plan and out.scout.text == "Repo is a python CLI."
    scout_req = fakes["agy"].requests[0]
    assert scout_req.extra_args == ["--mode", "plan"] and scout_req.json_schema is None
    assert "Repo is a python CLI." in fakes["claude"].requests[0].prompt
    assert out.total_tokens == 150


def test_scout_failure_stops_before_planner(tmp_path):
    out, fakes = run(tmp_path, {"agy": [result(text="x", ok=False, agent="agy")], "claude": [result(good_plan())]}, scout=True)
    assert out.plan is None and "scout failed" in out.error
    assert "claude" not in fakes


def test_planner_on_agy_gets_instructions_inline_and_plan_mode(tmp_path):
    cfg = CFG.model_copy(update={"roles": {**CFG.roles, "planner": CFG.roles["planner"].model_copy(update={"agent": "agy", "model": None})}})
    out, fakes = run(tmp_path, {"agy": [result(good_plan(), agent="agy")]}, cfg=cfg)
    req = fakes["agy"].requests[0]
    assert out.plan and req.extra_args == ["--mode", "plan"]
    assert "Planner (Laplace)" in req.prompt


def test_model_override_reaches_the_request(tmp_path):
    cfg = apply_overrides(CFG, ["planner=sonnet"])
    _, fakes = run(tmp_path, [result(good_plan())], cfg=cfg)
    assert fakes["claude"].requests[0].model == "sonnet"


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('text before {"a": 1} after') == {"a": 1}
