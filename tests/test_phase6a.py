"""Phase 6a: theme, demon registry, RunView, renderers, home screen, doctor. No real CLI is ever called."""

from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest
import yaml
from plan_factory import good_plan, step
from rich.console import Console
from test_executor import PASS_CMD, edit, env  # noqa: F401  (env is a fixture)
from typer.testing import CliRunner

from orch import budget, cli, doctor, home
from orch.adapters.base import AgentResult, ErrorKind
from orch.config import ROLE_NAMES, load_config
from orch.runs import load_meta
from orch.theme import DARK, DEMONS, LIGHT, Theme, can_encode, demon_label, resolve_theme
from orch.ui import (LiveView, PlainView, constellation, demonize, render_doctor, render_plain, render_rich,
                     render_stop_panel)  # fmt: skip
from orch.view import build_run_view, fmt_tokens

DARK_T = Theme("dark", color=True, ascii_=False)
ASCII_T = Theme("dark", color=False, ascii_=True)


class Stream:
    def __init__(self, encoding):
        self.encoding = encoding


# ---------- theme ----------


def test_palettes_have_exactly_the_specified_colours():
    assert (DARK.laplace, DARK.brillouin, DARK.maxwell, DARK.loschmidt, DARK.descartes) == (
        "#7AA2F7", "#7DCFFF", "#FF9E64", "#BB9AF7", "#F38BA8")  # fmt: skip
    assert (DARK.success, DARK.warning, DARK.error, DARK.text, DARK.dim, DARK.line) == (
        "#9ECE6A", "#E5C07B", "#FF6B6B", "#E6E9F5", "#8A90B0", "#3B4266")  # fmt: skip
    assert (LIGHT.laplace, LIGHT.brillouin, LIGHT.maxwell, LIGHT.loschmidt, LIGHT.descartes) == (
        "#2E5EAA", "#0E7C86", "#B85C00", "#7A4FC2", "#B4235A")  # fmt: skip
    assert (LIGHT.success, LIGHT.warning, LIGHT.error, LIGHT.text, LIGHT.dim, LIGHT.line) == (
        "#3F7D20", "#9A6B00", "#C62828", "#1F2340", "#5E6385", "#C9CCE0")  # fmt: skip


def test_demon_registry_covers_every_role():
    assert set(DEMONS) == set(ROLE_NAMES)
    assert {r: d.name for r, d in DEMONS.items()} == {
        "planner": "Laplace", "scout": "Brillouin", "builder": "Maxwell", "reviewer": "Loschmidt", "repair": "Descartes"}  # fmt: skip
    assert [DEMONS[r].glyph for r in ("planner", "scout", "builder", "reviewer", "repair")] == list("✦◎◆↺✚")
    assert [DEMONS[r].ascii for r in ("planner", "scout", "builder", "reviewer", "repair")] == list("*o#<+")
    assert demon_label("builder") == "Maxwell" and demon_label("builder", first=True) == "Maxwell (builder)"
    assert demon_label("unknown") == "unknown"


def test_theme_choice_order_and_validation():
    utf8 = Stream("utf-8")
    assert resolve_theme(env={}, stream=utf8).name == "dark"
    assert resolve_theme(cfg_theme="light", env={}, stream=utf8).name == "light"
    assert resolve_theme(cfg_theme="light", env={"ORCH_THEME": "dark"}, stream=utf8).name == "dark"  # env beats config
    assert resolve_theme("light", cfg_theme="dark", env={"ORCH_THEME": "dark"}, stream=utf8).name == "light"  # flag wins
    with pytest.raises(ValueError):
        resolve_theme("neon", env={}, stream=utf8)


def test_no_color_drops_colour_but_keeps_glyphs():
    t = resolve_theme(env={"NO_COLOR": "1"}, stream=Stream("utf-8"))
    assert not t.color and not t.ascii
    assert t.style("laplace") == "" and t.demon_style("builder") == "" and t.demon_glyph("builder") == "◆"
    assert t.sym("err") == "✗"
    assert resolve_theme(env={}, stream=Stream("utf-8")).demon_style("builder") == "#FF9E64"
    assert Theme("light").demon_style("builder") == "#B85C00"


def test_ascii_fallback_by_flag_and_by_encoding():
    assert can_encode(Stream("utf-8")) and not can_encode(Stream("cp1252")) and not can_encode(Stream("ascii"))
    assert resolve_theme(cli_ascii=True, env={}, stream=Stream("utf-8")).ascii
    t = resolve_theme(env={}, stream=Stream("cp1252"))
    assert t.ascii and t.demon_glyph("planner") == "*" and t.sym("ok") == "[ok]" and t.sym("warn") == "[!]" and t.sym("err") == "[x]"


# ---------- RunView and renderers ----------


def S(status, **kw):
    return {"title": "t", "status": status, "tokens": 0, **kw}


def meta_of(status, steps=None, **kw):
    return {"run_id": "r1", "status": status, "steps": steps, "models": {"planner": "claude:opus"},
            "plan_role_tokens": {"planner": 44000}, **kw}  # fmt: skip


DONE_META = meta_of("done", {
    "s1": S("done", tokens=900, roles_tokens={"builder": 900}, roles_used={"builder": {"entry": "claude:sonnet"}},
            started_at="2026-11-11T10:00:00+00:00", ended_at="2026-11-11T10:01:30+00:00"),
    "s2": S("done", tokens=500, roles_tokens={"builder": 300, "reviewer": 200},
            roles_used={"builder": {"entry": "claude:sonnet"}, "reviewer": {"entry": "claude:opus"}}),
    "s3": S("done"),
})  # fmt: skip
RUNNING_META = meta_of("executing", role_tokens={"builder": 910}, exec_tokens=910, steps={
    "s1": S("done", tokens=900, roles_tokens={"builder": 900}, roles_used={"builder": {"entry": "claude:sonnet"}}),
    "s2": S("running", title="Docs", phase="build", roles_tokens={"builder": 10}, roles_used={"builder": {"entry": "claude:sonnet"}}),
    "s3": S("pending"),
})  # fmt: skip
APPROVAL_META = meta_of("awaiting_approval", {
    "s1": S("done"), "s2": S("awaiting_approval", rule="new_dependency", stage="pre_builder"), "s3": S("pending")})  # fmt: skip
PAUSED_META = meta_of("paused", {
    "s1": S("done"), "s2": S("pending", error="rate limited"), "s3": S("pending")}, resume_after="2026-11-11T20:00:00+00:00")  # fmt: skip
FAILED_META = meta_of("failed", {"s1": S("done"), "s2": S("failed", error="tests could not run", stage="tests"), "s3": S("pending")})  # fmt: skip


@pytest.mark.parametrize("meta,expected", [
    (DONE_META, "✦───✦───✦"),
    (RUNNING_META, "✦───✧───·"),
    (APPROVAL_META, "✦───!───·"),
    (PAUSED_META, "✦───·───·"),
    (FAILED_META, "✦───✗───·"),
])  # fmt: skip
def test_constellation_string_per_state(meta, expected):
    assert constellation(build_run_view(meta), DARK_T).plain.startswith(expected)


def test_constellation_ascii_and_step_counter():
    assert constellation(build_run_view(RUNNING_META), ASCII_T).plain.startswith("*---@---.")
    assert "step 2 of 3" in constellation(build_run_view(RUNNING_META), DARK_T).plain
    assert "step 3 of 3" in constellation(build_run_view(DONE_META), DARK_T).plain


def test_running_star_uses_the_active_demons_colour():
    t = constellation(build_run_view(RUNNING_META), Theme("dark"))
    star = [s for s in t.spans if t.plain[s.start:s.end] == "✧"][0]
    assert "#FF9E64" in str(star.style)  # Maxwell
    repair = {"s1": S("running", phase="need_repair")}
    t2 = constellation(build_run_view(meta_of("executing", repair)), Theme("dark"))
    assert any("#F38BA8" in str(s.style) for s in t2.spans)  # Descartes


def test_runview_fields_running():
    v = build_run_view(RUNNING_META, {"builder": "claude:sonnet"}, {"s2": 41.0}, next_cmds=[])
    assert (v.run_id, v.status, v.step_index, v.step_count) == ("r1", "executing", 2, 3)
    assert v.current.role == "builder" and v.current.step_id == "s2" and "building s2" in v.current.text
    s2 = v.steps[1]
    assert (s2.kind, s2.role, s2.phase, s2.elapsed, s2.entry) == ("running", "builder", "build", 41.0, "claude:sonnet")
    assert v.tokens_total == 44000 + 910 and v.stop is None
    assert [d.role for d in v.demons] == ["planner", "builder"]
    assert v.demons[1].running and v.demons[1].text == "building s2" and v.demons[1].model == "sonnet"


def test_runview_done_uses_stored_timestamps_and_old_metas_show_none():
    v = build_run_view(DONE_META)
    assert v.steps[0].elapsed == 90.0 and v.steps[1].elapsed is None  # no timestamps: unknown, not a guess
    assert v.stop is None and v.current is None
    assert [d.role for d in v.demons] == ["planner", "builder", "reviewer"]
    assert v.demons[0].text == "planned 3 steps" and v.demons[1].text == "built 2 steps"
    started = dict(DONE_META, started_at="2026-11-11T10:00:00+00:00", ended_at="2026-11-11T10:05:10+00:00")
    assert build_run_view(started).elapsed_total == 310.0 and build_run_view(DONE_META).elapsed_total is None


def test_runview_stop_info_for_needs_you_paused_and_failed():
    v = build_run_view(APPROVAL_META, next_cmds=["orch approve r1 --step s2"])
    assert v.stop.kind == "needs_you" and v.stop.border == "warning" and "new_dependency" in v.stop.why
    assert "Laplace" not in v.stop.why and v.stop.next_cmds == ["orch approve r1 --step s2"]
    p = build_run_view(PAUSED_META)
    assert p.stop.kind == "paused" and "resume after 2026-11-11T20:00:00" in p.stop.why
    f = build_run_view(FAILED_META)
    assert f.stop.kind == "failed" and f.stop.border == "error"
    assert "Maxwell" not in f.stop.why and "tests could not run" in f.stop.why
    stage = build_run_view(meta_of("failed", {"s1": S("failed", stage="builder", error="x")}))
    assert "Maxwell (builder)" in stage.stop.why  # demon name, role in brackets


def test_plain_output_names_demons_and_has_no_colour_codes():
    lines = render_plain(build_run_view(RUNNING_META), ASCII_T)
    text = "\n".join(lines)
    assert "Laplace" in text and "Maxwell" in text and "building s2" in text and "\x1b" not in text
    assert lines[0].startswith("*---@---.")
    fb = build_run_view(meta_of("executing", {"s1": S("running", phase="build", roles_tokens={"builder": 5},
                                                      roles_used={"builder": {"entry": "claude:sonnet", "fallback": True}})}))
    assert "sonnet (fallback)" in "\n".join(render_plain(fb, DARK_T))


def test_log_lines_use_demon_names_with_role_on_first_mention():
    seen = set()
    assert demonize("[s1] Do s1: running builder", seen) == "[s1] Do s1: Maxwell (builder) building"
    assert demonize("[s2] Do s2: running builder", seen) == "[s2] Do s2: Maxwell building"
    assert demonize("[s1] reviewing", seen) == "[s1] Loschmidt (reviewer) reviewing"
    assert demonize("[s1] repair round 1/3: tests", seen) == "[s1] Descartes (repair) round 1/3: tests"
    assert demonize("[s1] done abc12345", seen) == "[s1] done abc12345"


def test_plain_view_logs_demon_names():
    out = []
    v = PlainView(out.append, ASCII_T)
    v.log("[s1] Do s1: running builder")
    v.show_stop("failed", "s1", "boom", {"steps": {"s1": {"stage": "builder", "error": "x"}}}, [])
    assert out[0] == "[s1] Do s1: Maxwell (builder) building"
    assert "Maxwell (builder)" in "\n".join(out) and "--- STOPPED ---" in out


def test_rich_renderer_and_panel_borders():
    buf = StringIO()
    console = Console(file=buf, width=100, force_terminal=False)
    console.print(render_rich(build_run_view(RUNNING_META, {"builder": "claude:sonnet"}, {"s2": 41.0}, elapsed_total=75), DARK_T))
    out = buf.getvalue()
    assert "step 2 of 3" in out and "Laplace" in out and "planned 3 steps" in out and "opus · 44k" in out
    assert "Maxwell" in out and "building s2" in out and "sonnet · 0:41" in out and "total 44,910 tokens" in out
    needs = build_run_view(APPROVAL_META, next_cmds=["orch approve r1"]).stop
    failed = build_run_view(FAILED_META).stop
    assert render_stop_panel(needs, "awaiting_approval", DARK_T).border_style == "#E5C07B"
    assert render_stop_panel(failed, "failed", DARK_T).border_style == "#FF6B6B"
    assert render_stop_panel(failed, "failed", Theme("light")).border_style == "#C62828"


def test_live_view_draws_constellation_and_demon_lines():
    buf = StringIO()
    t = iter(range(0, 1000, 5))
    view = LiveView({"builder": "claude:sonnet"}, Console(file=buf, width=120, force_terminal=False), clock=lambda: next(t), theme=ASCII_T)
    with view:
        view.log("[s2] Docs: running builder")
        view.on_update(RUNNING_META)
    text = buf.getvalue()
    assert "Maxwell (builder) building" in text and "step 2 of 3" in text and "Docs" in text and "total 44,910 tokens" in text


def test_fmt_tokens():
    assert [fmt_tokens(n) for n in (0, 999, 1500, 44000, 2_500_000)] == ["0", "999", "1.5k", "44k", "2.5M"]


# ---------- timestamps recorded by the executor ----------


def test_executor_records_started_and_ended_at(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=PASS_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    assert ex.start().status == "done"
    meta = load_meta(run_dir)
    s1 = meta["steps"]["s1"]
    assert s1["started_at"] <= s1["ended_at"] and meta["started_at"] <= meta["ended_at"]
    assert build_run_view(meta).steps[0].elapsed is not None and build_run_view(meta).elapsed_total is not None


# ---------- home screen ----------


def _cfg_file(tmp_path, repos=None, name="config.yaml", **extra):
    data = {
        "roles": {"planner": ["claude:opus", "claude:sonnet"], "scout": ["agy:default"], "builder": ["claude:sonnet"],
                  "reviewer": ["claude:opus", "agy:default"], "repair": ["claude:sonnet"]},
        "approval_rules": ["merge_to_main"],
        "repos": repos if repos is not None else {"demo": {"path": "repo", "test_command": "pytest -q"}},
        **extra,
    }  # fmt: skip
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _log(db, agent, model, tokens, when):
    conn = budget._connect(db)
    with conn:
        conn.execute(
            "INSERT INTO calls (ts, agent, model, ok, error_kind, duration_ms, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, cwd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (when.isoformat(timespec="seconds"), agent, model, 1, "none", 10, 0, 0, 0, 0, tokens, None, "."))
    conn.close()


def _texts(lines):
    return "\n".join(t.plain for t in lines)


def test_home_empty_usage_db(tmp_path):
    cfg = load_config(_cfg_file(tmp_path))
    d = home.build_home(cfg, db_path=tmp_path / "u.db", now=datetime(2026, 11, 11, tzinfo=timezone.utc))
    text = _texts(home.render_home(d, DARK_T))
    assert "✦ orch · Firmamentum v2" in text and "demo" in text
    assert "5h 0 · 24h 0" in text and "cooldowns" in text and "none" in text and "no runs yet" in text
    assert "Laplace" in text and "opus" in text and "Maxwell" in text and "sonnet" in text and "orch doctor" in text
    assert len(home.render_home(d, DARK_T)) <= 20


def test_home_populated_usage_db_cooldowns_and_latest_run(tmp_path):
    repo = tmp_path / "repo"
    runs = repo / ".orch" / "runs"
    for rid, status in (("20261110T100000Z-old", "done"), ("20261111T090000Z-new", "paused")):
        (runs / rid).mkdir(parents=True)
        (runs / rid / "meta.json").write_text(f'{{"status": "{status}", "created": "x"}}', encoding="utf-8")
    cfg = load_config(_cfg_file(tmp_path))
    db = tmp_path / "u.db"
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    _log(db, "claude", "opus", 30000, now - timedelta(hours=1))
    _log(db, "claude", "sonnet", 12000, now - timedelta(hours=10))
    _log(db, "claude", "sonnet", 99000, now - timedelta(hours=30))
    budget.record_cooldown("claude", "opus", now + timedelta(hours=2), "limit", now=now, db_path=db)
    d = home.build_home(cfg, db_path=db, now=now)
    assert (d.tokens_5h, d.tokens_24h) == (30000, 42000)
    text = _texts(home.render_home(d, DARK_T))
    assert "5h 30k · 24h 42k" in text and "claude:opus until" in text
    assert "20261111T090000Z-new" in text and "paused" in text and "20261110T100000Z-old" not in text
    assert "--repo demo" in text


def test_home_screen_via_cli_and_help_stays_standard(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_CONFIG", str(_cfg_file(tmp_path)))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("HOME", str(tmp_path))
    r = CliRunner().invoke(cli.app, [])
    assert r.exit_code == 0 and "orch · Firmamentum v2" in r.output and "orch doctor" in r.output
    h = CliRunner().invoke(cli.app, ["--help"])
    assert h.exit_code == 0 and "Usage:" in h.output and "--theme" in h.output and "doctor" in h.output
    bad = CliRunner().invoke(cli.app, ["--theme", "neon"])
    assert bad.exit_code == 2


# ---------- doctor ----------

GOOD_AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}


def ctx_for(tmp_path, **over):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    base = dict(
        env={}, home=tmp_path / "home", which=lambda n: f"C:/bin/{n}.exe", version=lambda n: f"{n} 1.2.3",
        auth_status=lambda: GOOD_AUTH, user_env=lambda k: None, config_path=over.get("config_path") or _cfg_file(tmp_path),
        db_path=tmp_path / "db" / "usage.db", wt_root=lambda: tmp_path / "wt", python=(3, 12),
        has_tzdata=lambda: True, is_git_repo=lambda p: True,
    )  # fmt: skip
    base.update(over)
    return doctor.Context(**base)


def by_name(checks):
    return {c.name: c for c in checks}


def levels(checks, level):
    return [c.name for c in checks if c.level == level]


def test_doctor_all_good_but_manual_agy_login(tmp_path):
    checks = doctor.run_checks(ctx_for(tmp_path))
    assert levels(checks, doctor.ERR) == []
    assert levels(checks, doctor.WARN) == ["agy login"]  # no status command that avoids a model call
    assert doctor.exit_code(checks) == 0
    assert by_name(checks)["claude login"].detail.startswith("claude.ai login")


def test_doctor_ollama_settings_json(tmp_path):
    s = tmp_path / "home" / ".claude"
    s.mkdir(parents=True)
    (s / "settings.json").write_text(
        '{"env": {"ANTHROPIC_BASE_URL": "http://localhost:11434", "ANTHROPIC_AUTH_TOKEN": "ollama-secret-xyz"}, "model": "qwen3:8b"}',
        encoding="utf-8")  # fmt: skip
    checks = doctor.run_checks(ctx_for(tmp_path))
    c = by_name(checks)
    assert c["~/.claude/settings.json env"].level == doctor.ERR and "ANTHROPIC_AUTH_TOKEN" in c["~/.claude/settings.json env"].detail
    assert c["~/.claude/settings.json model"].level == doctor.ERR
    assert "points Claude Code at a local model, see README" in c["~/.claude/settings.json model"].detail
    assert doctor.exit_code(checks) == 1
    rendered = _texts(render_doctor(checks, DARK_T))
    assert "ollama-secret-xyz" not in rendered and "localhost:11434" not in rendered  # values are never printed


def test_doctor_api_key_in_process_env_is_masked(tmp_path):
    secret = "sk-ant-api03-SUPERSECRETVALUE"
    checks = doctor.run_checks(ctx_for(tmp_path, env={"ANTHROPIC_API_KEY": secret}))
    c = by_name(checks)["env ANTHROPIC_API_KEY"]
    assert c.level == doctor.ERR and "Remove-Item Env:ANTHROPIC_API_KEY" in c.fix
    rendered = _texts(render_doctor(checks, DARK_T))
    assert secret not in rendered and "SUPERSECRET" not in rendered and f"({len(secret)} chars)" in rendered
    assert doctor.exit_code(checks) == 1
    assert doctor.mask("abc") == "… (3 chars)" and "SECRET" not in doctor.mask("sk-SECRET-VALUE-1")


def test_doctor_user_environment_variable(tmp_path):
    checks = doctor.run_checks(ctx_for(tmp_path, user_env=lambda k: "http://x" if k == "ANTHROPIC_BASE_URL" else None))
    c = by_name(checks)["user env ANTHROPIC_BASE_URL"]
    assert c.level == doctor.ERR and "SetEnvironmentVariable" in c.fix and "http://x" not in c.detail


def test_doctor_agy_missing_is_a_warning_unless_a_role_needs_only_agy(tmp_path):
    no_agy = lambda n: None if n == "agy" else f"C:/bin/{n}.exe"  # noqa: E731
    cfg_with_fallback = _cfg_file(tmp_path, name="fallback.yaml")
    data = yaml.safe_load(cfg_with_fallback.read_text())
    data["roles"]["scout"] = ["agy:default", "claude:sonnet"]
    cfg_with_fallback.write_text(yaml.safe_dump(data), encoding="utf-8")
    c = by_name(doctor.run_checks(ctx_for(tmp_path, which=no_agy, config_path=cfg_with_fallback)))
    assert c["agy"].level == doctor.WARN
    only = by_name(doctor.run_checks(ctx_for(tmp_path, which=no_agy, config_path=_cfg_file(tmp_path))))  # scout: [agy:default]
    assert only["agy"].level == doctor.ERR and "scout" in only["agy"].detail


def test_doctor_missing_claude_git_python_uv(tmp_path):
    none = lambda n: None  # noqa: E731
    c = by_name(doctor.run_checks(ctx_for(tmp_path, which=none, python=(3, 11))))
    assert c["claude"].level == doctor.ERR and c["git"].level == doctor.ERR and c["python"].level == doctor.ERR
    assert c["uv"].level == doctor.WARN and c["claude login"].level == doctor.WARN


def test_doctor_login_types(tmp_path):
    def login(status):
        return by_name(doctor.run_checks(ctx_for(tmp_path, auth_status=lambda: status)))["claude login"]

    assert login({"loggedIn": False}).level == doctor.ERR
    assert login({"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"}).level == doctor.ERR
    assert login(None).level == doctor.WARN and "/status" in login(None).fix


def test_doctor_repo_problems(tmp_path):
    cfg = _cfg_file(tmp_path, repos={
        "notest": {"path": "repo", "test_command": ""}, "gone": {"path": "nowhere", "test_command": "pytest"},
        "nogit": {"path": "repo2", "test_command": "pytest"}, "fine": {"path": "repo", "test_command": "pytest"}})  # fmt: skip
    (tmp_path / "repo2").mkdir()
    c = by_name(doctor.run_checks(ctx_for(tmp_path, config_path=cfg, is_git_repo=lambda p: p.name != "repo2")))
    assert c["repo notest"].level == doctor.ERR and "test_command" in c["repo notest"].detail
    assert c["repo gone"].level == doctor.ERR and "does not exist" in c["repo gone"].detail
    assert c["repo nogit"].level == doctor.ERR and "not a git repo" in c["repo nogit"].detail
    assert c["repo fine"].level == doctor.OK


def test_doctor_bad_config_paths_tzdata_and_cooldowns(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("roles: 3\n", encoding="utf-8")
    c = by_name(doctor.run_checks(ctx_for(tmp_path, config_path=bad)))
    assert c["config.yaml"].level == doctor.ERR and not [n for n in c if n.startswith("repo ")]
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    c = by_name(doctor.run_checks(ctx_for(tmp_path, wt_root=lambda: blocker / "wt", db_path=blocker / "usage.db", has_tzdata=lambda: False)))
    assert c["worktree root"].level == doctor.ERR and c["usage DB"].level == doctor.ERR and c["tzdata"].level == doctor.WARN
    db = tmp_path / "cd.db"
    budget.record_cooldown("claude", "opus", datetime.now(timezone.utc) + timedelta(hours=1), "limit", db_path=db)
    c = by_name(doctor.run_checks(ctx_for(tmp_path, db_path=db)))
    assert c["cooldown claude:opus"].level == doctor.WARN and "orch cooldowns --clear claude" in c["cooldown claude:opus"].fix


def test_doctor_render_shows_fix_lines_and_ascii(tmp_path):
    checks = [doctor.Check("git", doctor.ERR, "not found", "install git"), doctor.Check("uv", doctor.OK, "x"),
              doctor.Check("agy", doctor.WARN, "w", "do this")]  # fmt: skip
    text = _texts(render_doctor(checks, ASCII_T))
    assert "[x] git" in text and "[ok] uv" in text and "[!] agy" in text and "fix: install git" in text and "fix: do this" in text
    assert "fix: " not in _texts(render_doctor(checks[1:2], DARK_T))


def test_doctor_live_makes_one_cheap_call_per_cli(tmp_path):
    calls = []

    def run(adapter, request):
        calls.append((adapter, request.model, request.prompt))
        ok = adapter != "agy-adapter"
        return AgentResult(agent="claude", ok=ok, error_kind=ErrorKind.OK if ok else ErrorKind.AGENT_ERROR, text="PONG", total_tokens=15000)

    checks = doctor.live_checks(lambda name: f"{name}-adapter", run)
    assert calls == [("claude-adapter", "sonnet", "Reply PONG"), ("agy-adapter", None, "Reply PONG")]
    assert [(c.name, c.level) for c in checks] == [("live claude", doctor.OK), ("live agy", doctor.ERR)]


def test_doctor_cli_exit_code_and_live_needs_confirmation(tmp_path, monkeypatch):
    ctx = ctx_for(tmp_path, env={"ANTHROPIC_API_KEY": "sk-secret-secret-1"})
    good = ctx_for(tmp_path)
    monkeypatch.setattr(cli.doctor_mod, "Context", lambda: ctx)
    monkeypatch.setattr(cli.doctor_mod, "live_checks", lambda *a, **k: pytest.fail("live call without confirmation"))
    r = CliRunner().invoke(cli.app, ["doctor", "--live"], input="n\n")
    assert r.exit_code == 1 and "Skipped the live calls" in r.output and "sk-secret-secret-1" not in r.output
    monkeypatch.setattr(cli.doctor_mod, "Context", lambda: good)
    ok = CliRunner().invoke(cli.app, ["--ascii", "doctor"])
    assert ok.exit_code == 0 and "[ok] python" in ok.output and "0 failed" in ok.output


def test_status_command_shows_constellation_and_demon_names(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    cfg = _cfg_file(tmp_path, repos={"demo": {"path": str(repo), "test_command": PASS_CMD}})
    monkeypatch.setenv("ORCH_CONFIG", str(cfg))
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=PASS_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    ex.start()
    r = CliRunner().invoke(cli.app, ["status", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 0, r.output
    assert "step 1 of 1" in r.output and "Maxwell" in r.output and "Run tokens:" in r.output and "time" in r.output
    assert "Planning models:" not in r.output or "Laplace=" in r.output
