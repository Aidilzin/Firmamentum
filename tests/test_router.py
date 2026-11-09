"""Phase 5a: router, cooldowns, fallbacks, resume_after, cooldowns/usage CLI. FakeAdapter-style fakes only."""

import json
import sqlite3
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from plan_factory import good_plan, result, step
from test_executor import PASS_CMD, FakeBuilder, edit, env, fail  # noqa: F401  (env is a fixture)
from test_phase4 import ON, review, write_cfg  # noqa: F401
from typer.testing import CliRunner

from orch import budget, cli
from orch.adapters.base import AgentRequest, ErrorKind, failure
from orch.adapters.fake import FakeAdapter
from orch.config import Entry, ResolvedRepo, load_config
from orch.planner import make_plan
from orch.router import call_role, cooldown_scope, parse_reset_time, pick
from orch.runs import load_meta

runner = CliRunner()
CFG = load_config()
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)  # 20:00 in Asia/Singapore


def rl(text="usage limit reached", agent="claude"):
    return failure(agent, ErrorKind.RATE_LIMITED, text)


def fakes_for(results_by_agent):
    made = {}

    def factory(name):
        return made.setdefault(name, FakeAdapter(list(results_by_agent.get(name, [])), agent_name=name))

    return factory, made


def build(prompt="p", tmp="."):
    return lambda entry: AgentRequest(prompt=prompt, cwd=tmp, model=entry.model)


# ---------- pick ----------


def test_pick_first_available_and_skips_cooling(tmp_path):
    entry, wait = pick("planner", CFG, NOW)
    assert entry.label == "claude:opus" and wait is None
    budget.record_cooldown("claude", "opus", NOW + timedelta(hours=1), "opus limit", NOW)
    entry, _ = pick("planner", CFG, NOW)
    assert entry.label == "claude:sonnet"
    assert pick("planner", CFG, NOW + timedelta(hours=2))[0].label == "claude:opus"  # expired


def test_pick_none_returns_earliest_until():
    budget.record_cooldown("claude", "*", NOW + timedelta(hours=3), "x", NOW)
    entry, wait = pick("planner", CFG, NOW)
    assert entry is None and wait == NOW + timedelta(hours=3)
    budget.record_cooldown("agy", "*", NOW + timedelta(hours=1), "y", NOW)
    assert pick("reviewer", CFG, NOW) == (None, NOW + timedelta(hours=1))  # earliest over the role's entries


# ---------- cooldown scope ----------


def test_claude_rate_limit_cools_all_claude_models():
    factory, _ = fakes_for({"claude": [rl("5-hour limit reached"), result(text="x")]})
    routed = call_role("builder", build(), factory, CFG, now=NOW)  # builder has only claude:sonnet
    assert routed.paused
    cds = budget.active_cooldowns(NOW)
    assert [(c.agent, c.model) for c in cds] == [("claude", "*")]
    assert pick("planner", CFG, NOW)[0] is None  # opus AND sonnet are cooling


def test_model_specific_message_cools_only_that_model():
    factory, made = fakes_for({"claude": [rl("Opus usage limit reached"), result(text="ok")]})
    routed = call_role("planner", build(), factory, CFG, now=NOW)
    assert [(c.agent, c.model) for c in budget.active_cooldowns(NOW)] == [("claude", "opus")]
    assert routed.entry.label == "claude:sonnet" and routed.fallback and not routed.paused
    assert [r.model for r in made["claude"].requests] == ["opus", "sonnet"]


def test_opus_call_whose_message_names_sonnet_cools_everything():
    factory, _ = fakes_for({"claude": [rl("Your sonnet quota is fine but you hit the limit")]})
    call_role("planner", build(), factory, CFG, now=NOW)
    assert [(c.agent, c.model) for c in budget.active_cooldowns(NOW)] == [("claude", "*")]


@pytest.mark.parametrize(
    "agent,model,text,expected",
    [
        ("claude", "opus", "opus limit", "opus"),
        ("claude", "claude-opus-4", "Opus-only limit reached", "claude-opus-4"),
        ("claude", "opus", "sonnet limit", "*"),
        ("claude", "opus", "opus and sonnet limit", "*"),
        ("claude", "opus", "limit reached", "*"),
        ("claude", None, "opus limit", "*"),
        ("agy", None, "quota exhausted for gemini-x", "*"),
        ("agy", "gemini-x", "quota exhausted for gemini-x", "gemini-x"),
        ("agy", "gemini-x", "quota exhausted", "*"),
    ],
)
def test_cooldown_scope(agent, model, text, expected):
    assert cooldown_scope(agent, model, text) == expected


# ---------- reset-time parsing ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("limit hit. try again in 45 minutes", NOW + timedelta(minutes=45)),
        ("Please retry in 2 hours 30 minutes.", NOW + timedelta(hours=2, minutes=30)),
        ("retry after 90 seconds", NOW + timedelta(seconds=90)),
        ("resets 2026-11-10T17:00:00Z", datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)),
        ("resets at 2026-11-10T19:00:00+02:00", datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)),
        ("Claude AI usage limit reached|1794330000", datetime.fromtimestamp(1794330000, timezone.utc)),
    ],
)
def test_parse_reset_time_shapes(text, expected):
    got, parsed = parse_reset_time(text, NOW, 60)
    assert parsed and got == expected


def test_parse_clock_time_is_next_local_occurrence():
    got, parsed = parse_reset_time("Your limit resets at 5am", NOW, 60)
    local = got.astimezone()
    assert parsed and (local.hour, local.minute) == (5, 0) and NOW < got <= NOW + timedelta(days=1)
    got, parsed = parse_reset_time("resets at 3:30 PM", NOW, 60)
    assert parsed and (got.astimezone().hour, got.astimezone().minute) == (15, 30)


SGT = timezone(timedelta(hours=8))


def _have_zone(name):
    try:
        ZoneInfo(name)
        return True
    except Exception:  # no tzdata on Windows unless the tzdata package is installed
        return False


@pytest.mark.skipif(not _have_zone("Asia/Singapore"), reason="no tz database (tzdata) available")
@pytest.mark.parametrize(
    "text,expected",
    [
        # Claude Code /usage: no "at", optional date, optional tz name in brackets. NOW = 20:00 SGT on Nov 10.
        ("Resets 4:59am (Asia/Singapore)", datetime(2026, 11, 11, 4, 59, tzinfo=SGT)),
        ("Resets 9pm (Asia/Singapore)", datetime(2026, 11, 10, 21, 0, tzinfo=SGT)),
        ("Resets Nov 12, 7:59pm (Asia/Singapore)", datetime(2026, 11, 12, 19, 59, tzinfo=SGT)),
        ("Resets November 12, 7:59pm (Asia/Singapore)", datetime(2026, 11, 12, 19, 59, tzinfo=SGT)),
        ("Session limit reached. resets at 5am (Asia/Singapore)", datetime(2026, 11, 11, 5, 0, tzinfo=SGT)),
    ],
)
def test_parse_usage_screen_formats_with_named_zone(text, expected):
    got, parsed = parse_reset_time(text, NOW, 60)
    assert parsed and got == expected


def test_parse_usage_screen_time_only_shapes_any_zone():
    # an unresolvable zone name falls back to the machine's zone, so these hold on any machine
    for text, hm in (("Resets 4:59am (Nowhere/Land)", (4, 59)), ("Resets 9pm (Nowhere/Land)", (21, 0)), ("Resets 4:59am", (4, 59))):
        got, parsed = parse_reset_time(text, NOW, 60)
        assert parsed and (got.astimezone().hour, got.astimezone().minute) == hm, text
        assert NOW < got <= NOW + timedelta(days=1)


def test_parse_usage_screen_date_shapes_any_zone():
    for text in ("Resets Nov 12, 7:59pm (Nowhere/Land)", "Resets November 12, 7:59pm", "resets Nov 12 7:59pm (Nowhere/Land)"):
        got, parsed = parse_reset_time(text, NOW, 60)
        local = got.astimezone()
        assert parsed and (local.month, local.day, local.hour, local.minute) == (11, 12, 19, 59), text


def test_parse_usage_screen_bad_dates_fall_back():
    for text in ("Resets Foo 12, 7:59pm", "Resets Nov 40, 7:59pm", "Resets Dec 30, 7:59pm (Nowhere/Land)"):
        assert parse_reset_time(text, NOW, 60) == (NOW + timedelta(minutes=60), False), text



@pytest.mark.parametrize(
    "text",
    ["rate limited", "", "resets 5 times", "resets at 2020-01-01T00:00:00Z", "try again in 400 days", "resets at 25pm"],
)
def test_parse_reset_time_falls_back_to_cooldown_minutes(text):
    got, parsed = parse_reset_time(text, NOW, 60)
    assert not parsed and got == NOW + timedelta(minutes=60)


def test_cooldown_uses_config_minutes_and_keeps_raw_message():
    cfg = CFG.model_copy(update={"cooldown_minutes": 17})
    factory, _ = fakes_for({"claude": [rl("limit!")]})
    call_role("builder", build(), factory, cfg, now=NOW)
    (c,) = budget.active_cooldowns(NOW)
    assert c.until == NOW + timedelta(minutes=17) and c.reason == "limit!"


# ---------- the hop ----------


def test_planner_hops_opus_to_sonnet_and_records_route(tmp_path):
    factory, made = fakes_for({"claude": [rl("Opus limit reached"), result(good_plan())]})
    repo = ResolvedRepo(name="demo", path=tmp_path, test_command="uv run pytest -q")
    out = make_plan("add a thing", repo, CFG, factory)
    assert out.plan and not out.paused
    assert out.routes == [{"role": "planner", "entry": "claude:sonnet", "fallback": True}]
    assert [r.model for r in made["claude"].requests] == ["opus", "sonnet"]
    assert made["claude"].requests[0].prompt == made["claude"].requests[1].prompt


def test_only_one_hop_per_call_even_if_a_third_entry_is_free():
    factory, made = fakes_for({"claude": [rl("Opus limit reached"), rl("Sonnet limit reached")], "agy": [result(text="x", agent="agy")]})
    routed = call_role("reviewer", build(), factory, CFG, now=NOW)
    assert routed.paused and routed.result is None
    assert "agy" not in made  # agy:default was never tried
    assert len(made["claude"].requests) == 2
    assert routed.resume_after is not None and routed.resume_after > NOW


def test_planner_all_entries_cooling_is_paused_with_resume_after_and_no_call(tmp_path):
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    budget.record_cooldown("claude", "*", until, "5h limit")
    factory, made = fakes_for({"claude": [result(good_plan())]})
    repo = ResolvedRepo(name="demo", path=tmp_path, test_command="uv run pytest -q")
    out = make_plan("x", repo, CFG, factory)
    assert out.plan is None and out.paused and out.resume_after == until.replace(microsecond=0)
    assert "claude" not in made


# ---------- executor integration ----------


def test_reviewer_falls_back_to_agy_in_plan_mode(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    # opus rate limited with a generic message -> claude:* cools -> hop to agy:default, which approves
    ex, fb = executor(run_dir, rr, [edit({"a.py": "1\n"}), fail(ErrorKind.RATE_LIMITED), review("approve")], **ON)
    assert ex.start().status == "done"
    review_req = fb.requests[2]
    assert review_req.extra_args[:2] == ["--mode", "plan"] and review_req.model is None
    assert "Reviewer" in review_req.prompt  # agy gets the role text inline
    meta = load_meta(run_dir)
    assert meta["steps"]["s1"]["roles_used"]["reviewer"] == {"entry": "agy:default", "fallback": True}
    assert meta["steps"]["s1"]["roles_used"]["builder"] == {"entry": "claude:sonnet", "fallback": False}
    assert {"step": "s1", "role": "reviewer", "entry": "agy:default", "fallback": True} in meta["calls_log"]


def test_all_entries_cooling_pauses_run_with_resume_after_and_makes_no_call(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    budget.record_cooldown("claude", "*", until, "5h limit")
    ex, fb = executor(run_dir, rr, [])
    out = ex.start()
    assert out.status == "paused" and fb.requests == []
    meta = load_meta(run_dir)
    assert meta["status"] == "paused" and meta["steps"]["s1"]["status"] == "pending"
    assert datetime.fromisoformat(meta["resume_after"]) == until.replace(microsecond=0)


def _paused_run(env, tmp_path, monkeypatch, until):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)
    run_dir, _ = setup(plan=good_plan(steps=[step("s1")]))
    budget.record_cooldown("claude", "*", until, "5h limit")
    fb = FakeBuilder([edit({"a.py": "1\n"})])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    r = runner.invoke(cli.app, ["exec", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 3 and "Result: paused" in r.output and "Paused until" in r.output
    return run_dir, fb


def test_resume_before_resume_after_is_refused_without_force(env, tmp_path, monkeypatch):
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    run_dir, fb = _paused_run(env, tmp_path, monkeypatch, until)
    r = runner.invoke(cli.app, ["resume", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 3 and "paused until" in r.output.lower()
    assert until.astimezone().strftime("%H:%M") in r.output  # local time
    assert fb.requests == []
    # --force goes ahead, but the router still honours the cooldown: nothing is called
    r = runner.invoke(cli.app, ["resume", run_dir.name, "--repo", "demo", "--force"])
    assert r.exit_code == 3 and "Result: paused" in r.output and fb.requests == []
    # once the cooldown is cleared, plain --force (or a past resume_after) really resumes
    budget.clear_cooldowns()
    r = runner.invoke(cli.app, ["resume", run_dir.name, "--repo", "demo", "--force"])
    assert r.exit_code == 0 and len(fb.requests) == 1


def test_resume_after_in_the_past_resumes_normally(env, tmp_path, monkeypatch):
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    run_dir, fb = _paused_run(env, tmp_path, monkeypatch, until)
    meta = load_meta(run_dir)
    meta["resume_after"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    budget.clear_cooldowns()
    r = runner.invoke(cli.app, ["resume", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 0 and "models used: builder=claude:sonnet" in r.output


# ---------- cooldowns CLI ----------


def test_cooldowns_list_and_clear():
    assert "no active cooldowns" in runner.invoke(cli.app, ["cooldowns"]).output
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    budget.record_cooldown("claude", "*", until, "claude limit")
    budget.record_cooldown("agy", "*", until, "agy limit")
    out = runner.invoke(cli.app, ["cooldowns"]).output
    assert "claude:*" in out and "agy:*" in out and "claude limit" in out
    r = runner.invoke(cli.app, ["cooldowns", "--clear", "agy"])
    assert r.exit_code == 0 and "Cleared 1" in r.output
    assert [c.agent for c in budget.active_cooldowns()] == ["claude"]
    r = runner.invoke(cli.app, ["cooldowns", "--clear"])
    assert "Cleared 1" in r.output and budget.active_cooldowns() == []


def test_expired_cooldowns_are_not_listed():
    budget.record_cooldown("claude", "*", datetime.now(timezone.utc) - timedelta(minutes=1), "old")
    assert budget.active_cooldowns() == []


# ---------- usage --window ----------


def _insert(db, hours_ago, agent, model, tokens):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO calls (ts, agent, model, ok, error_kind, duration_ms, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, cwd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, agent, model, 1, "OK", 1, 0, 0, 0, 0, tokens, None, "."),
    )
    conn.commit()
    conn.close()


def test_usage_window_filters_by_agent_and_model(tmp_path):
    db = budget.default_db_path()
    budget._connect(None).close()  # create tables
    _insert(db, 1, "claude", "opus", 100)
    _insert(db, 2, "claude", "sonnet", 10)
    _insert(db, 2, "agy", None, 5)
    _insert(db, 30, "claude", "opus", 1000)  # outside 5h and 24h
    _insert(db, 24 * 6, "claude", "opus", 7)  # inside 7d only

    def models(*args):
        r = runner.invoke(cli.app, ["usage", "--json", *args])
        assert r.exit_code == 0, r.output
        return {(m["agent"], m["model"]): (m["calls"], m["tokens"]) for m in json.loads(r.output)["models"]}

    assert models("--window", "5h") == {("claude", "opus"): (1, 100), ("claude", "sonnet"): (1, 10), ("agy", "default"): (1, 5)}
    assert models("--window", "90m") == {("claude", "opus"): (1, 100)}
    assert models("--window", "24h")[("claude", "opus")] == (1, 100)
    assert models("--window", "7d")[("claude", "opus")] == (3, 1107)
    # --days still works and the old "agents" key is unchanged
    data = json.loads(runner.invoke(cli.app, ["usage", "--days", "1", "--json"]).output)
    assert data["days"] == 1 and {a["agent"]: a["calls"] for a in data["agents"]} == {"claude": 2, "agy": 1}
    text = runner.invoke(cli.app, ["usage", "--window", "5h"]).output
    assert "last 5h" in text and "opus" in text and "sonnet" in text


@pytest.mark.parametrize("bad", ["5", "h", "0h", "5x", "-1h"])
def test_usage_window_rejects_bad_values(bad):
    assert runner.invoke(cli.app, ["usage", "--window", bad]).exit_code == 2
