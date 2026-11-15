"""Session: config with reload, overrides, theme, repo and snapshots (audit D7)."""

import os

import pytest
import yaml

from orch.config import ConfigError, default_config_path
from orch.session import RunContext, Session


@pytest.fixture
def cfgfile(tmp_path):
    data = yaml.safe_load(open(default_config_path().parent / "config.yaml", encoding="utf-8"))
    (tmp_path / "proj").mkdir()
    data["repos"] = {"demo": {"path": "proj", "test_command": "pytest -q"}}
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    return f


def _rewrite(f, **changes):
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data.update(changes)
    f.write_text(yaml.safe_dump(data), encoding="utf-8")


def _session(cfgfile, **kw):
    return Session(config_path=cfgfile, env={}, **kw)


def test_overrides_apply_to_config(cfgfile):
    s = _session(cfgfile)
    base = s.config.roles["planner"][0]
    s.set_overrides(["planner=sonnet"])
    assert s.config.roles["planner"][0].model == "sonnet"
    s.set_overrides([])
    assert s.config.roles["planner"][0] == base


def test_bad_override_is_stored_then_raised_on_read(cfgfile):
    s = _session(cfgfile)
    s.set_overrides(["nonsense"])  # stored only
    with pytest.raises(ConfigError, match="bad --model"):
        s.config


def test_config_error_comes_before_a_bad_override(tmp_path):
    s = _session(tmp_path / "missing.yaml")
    s.set_overrides(["nonsense"])
    assert s.config_or_none is None and s.config_error
    with pytest.raises(ConfigError, match="cannot read config"):
        s.config


def test_failed_reload_keeps_the_old_config(cfgfile):
    s = _session(cfgfile)
    old = s.config
    cfgfile.write_text("not: [valid", encoding="utf-8")
    s.reload()
    assert s.config is old and s.config_error and "invalid YAML" in s.config_error


def test_reload_builds_a_new_object_and_leaves_the_old_one_alone(cfgfile):
    s = _session(cfgfile)
    old = s.config
    before = old.model_copy(deep=True)
    _rewrite(cfgfile, cooldown_minutes=7)
    s.reload()
    assert s.config is not old and s.config.cooldown_minutes == 7 and s.config_error is None
    assert old == before and old.cooldown_minutes != 7


def test_reload_clears_a_previous_error(cfgfile):
    s = _session(cfgfile)
    good = cfgfile.read_text(encoding="utf-8")
    cfgfile.write_text("not: [valid", encoding="utf-8")
    s.reload()
    assert s.config_error
    cfgfile.write_text(good, encoding="utf-8")
    s.reload()
    assert s.config_error is None


def test_theme_precedence_and_reload(cfgfile):
    s = _session(cfgfile)
    assert s.theme.name == "dark"
    _rewrite(cfgfile, ui={"theme": "light"})
    s.reload()
    assert s.theme.name == "light"  # follows config when nothing explicit was chosen
    s.set_theme("dark")
    _rewrite(cfgfile, ui={"theme": "light"})
    s.reload()
    assert s.theme.name == "dark"  # an explicit choice survives a reload


def test_env_theme_beats_config(cfgfile):
    _rewrite(cfgfile, ui={"theme": "light"})
    s = Session(config_path=cfgfile, env={"ORCH_THEME": "dark"})
    assert s.theme.name == "dark"


def test_unknown_theme_raises_value_error(cfgfile):
    s = _session(cfgfile, theme="purple")
    with pytest.raises(ValueError, match="unknown theme"):
        s.theme


def test_ascii_flag(cfgfile):
    assert _session(cfgfile, ascii_=True).theme.ascii is True


def test_repo_uses_the_stored_cwd_not_the_process_cwd(cfgfile, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    s = _session(cfgfile, cwd=proj)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    r = s.repo()
    assert r.path == proj.resolve() and r.name == "demo"


def test_repo_argument_and_session_repo(cfgfile, tmp_path):
    s = _session(cfgfile, cwd=tmp_path)
    assert s.repo("demo").name == "demo"
    s.set_repo("demo")
    assert s.repo().name == "demo"
    assert s.repo(str(tmp_path)).name is None  # an explicit argument beats the session repo
    with pytest.raises(ConfigError):
        s.repo("no-such-dir")


def test_repo_is_resolved_on_each_call(cfgfile):
    s = _session(cfgfile)
    assert s.repo("demo").test_command == "pytest -q"
    _rewrite(cfgfile, repos={"demo": {"path": "proj", "test_command": "pytest -x"}})
    s.reload()
    assert s.repo("demo").test_command == "pytest -x"


def test_snapshot_is_isolated_from_the_session(cfgfile):
    s = _session(cfgfile)
    s.set_overrides(["planner=sonnet"])
    ctx = s.snapshot("demo")
    assert isinstance(ctx, RunContext) and ctx.db_path == s.db_path and ctx.repo.name == "demo"
    assert ctx.config == s.config and ctx.config is not s.config
    ctx.config.cooldown_minutes = 999
    ctx.config.roles["planner"].append(ctx.config.roles["planner"][0])
    assert s.config.cooldown_minutes != 999 and len(s.config.roles["planner"]) != len(ctx.config.roles["planner"])


def test_snapshot_does_not_follow_a_later_reload_or_override(cfgfile):
    s = _session(cfgfile)
    ctx = s.snapshot("demo")
    before = ctx.config.model_copy(deep=True)
    _rewrite(cfgfile, cooldown_minutes=5)
    s.reload()
    s.set_overrides(["planner=sonnet"])
    assert ctx.config == before


def test_db_path_defaults_to_budget_default_db_path(cfgfile, tmp_path):
    assert _session(cfgfile).db_path == tmp_path / "usage-test.db"  # conftest patches budget.default_db_path
    assert _session(cfgfile, db_path=tmp_path / "x.db").db_path == tmp_path / "x.db"


def test_default_config_path_follows_orch_config(cfgfile, monkeypatch):
    monkeypatch.setenv("ORCH_CONFIG", str(cfgfile))
    assert Session().config_path == cfgfile
    assert os.environ["ORCH_CONFIG"] == str(cfgfile)
