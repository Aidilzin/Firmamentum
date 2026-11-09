import pytest
import yaml

from orch.config import ConfigError, apply_overrides, default_config_path, load_config, resolve_repo


def test_repo_config_loads():
    cfg = load_config(default_config_path().parent / "config.yaml")
    assert [e.label for e in cfg.roles["planner"]] == ["claude:opus", "claude:sonnet"]
    assert [e.label for e in cfg.roles["scout"]] == ["agy:default"] and cfg.roles["scout"][0].model is None
    assert [e.label for e in cfg.roles["reviewer"]] == ["claude:opus", "claude:sonnet", "agy:default"]
    assert cfg.cooldown_minutes == 60
    assert len(cfg.approval_rules) == 9
    assert cfg.repos["orch"].test_command == "uv run pytest -q"


def test_overrides_are_applied_and_repeatable():
    cfg = load_config()
    new = apply_overrides(cfg, ["planner=sonnet", "scout=gemini-x"])
    assert [e.label for e in new.roles["planner"]] == ["claude:sonnet", "claude:opus"]  # sonnet first, duplicate dropped
    assert [e.label for e in new.roles["scout"]] == ["agy:gemini-x", "agy:default"]
    r = apply_overrides(cfg, ["reviewer=haiku"])
    assert [e.label for e in r.roles["reviewer"]] == ["claude:haiku", "claude:opus", "claude:sonnet", "agy:default"]
    assert cfg.roles["planner"][0].model == "opus"  # original untouched


@pytest.mark.parametrize("bad", ["planner", "planner=", "=sonnet", "nobody=sonnet"])
def test_bad_overrides(bad):
    with pytest.raises(ConfigError):
        apply_overrides(load_config(), [bad])


def _write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _valid():
    return yaml.safe_load(open(default_config_path().parent / "config.yaml", encoding="utf-8"))


def test_missing_role_rejected(tmp_path):
    d = _valid()
    del d["roles"]["repair"]
    with pytest.raises(ConfigError, match="repair"):
        load_config(_write(tmp_path, d))


def test_unknown_agent_and_rule_rejected(tmp_path):
    d = _valid()
    d["roles"]["planner"] = ["gpt:x"]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, d))
    d = _valid()
    d["approval_rules"].append("do_anything")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, d))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_resolve_repo_by_name_and_path(tmp_path):
    d = _valid()
    d["repos"] = {"demo": {"path": "proj", "test_command": "pytest"}}
    (tmp_path / "proj").mkdir()
    cfg = load_config(_write(tmp_path, d))
    by_name = resolve_repo(cfg, "demo")
    assert by_name.path == (tmp_path / "proj").resolve() and by_name.test_command == "pytest"
    by_path = resolve_repo(cfg, str(tmp_path / "proj"))
    assert by_path.name == "demo" and by_path.test_command == "pytest"
    other = tmp_path / "other"
    other.mkdir()
    assert resolve_repo(cfg, str(other)).test_command is None
    with pytest.raises(ConfigError):
        resolve_repo(cfg, str(tmp_path / "missing"))


def test_old_single_entry_form_still_loads(tmp_path):
    d = _valid()
    d["roles"] = {
        "planner": {"agent": "claude", "model": "opus"},
        "scout": {"agent": "agy", "model": None},
        "builder": {"agent": "claude", "model": "sonnet"},
        "reviewer": {"agent": "claude", "model": "opus"},
        "repair": {"agent": "claude", "model": "sonnet"},
    }
    d["fallback_model"] = {"claude": "sonnet"}
    cfg = load_config(_write(tmp_path, d))
    assert [e.label for e in cfg.roles["planner"]] == ["claude:opus"]
    assert [e.label for e in cfg.roles["scout"]] == ["agy:default"]


@pytest.mark.parametrize("role", ["builder", "repair"])
def test_agy_rejected_for_builder_and_repair(tmp_path, role):
    d = _valid()
    d["roles"][role] = ["claude:sonnet", "agy:default"]
    with pytest.raises(ConfigError, match=role):
        load_config(_write(tmp_path, d))
    d["roles"][role] = {"agent": "agy", "model": None}
    with pytest.raises(ConfigError, match=role):
        load_config(_write(tmp_path, d))


def test_empty_role_list_rejected(tmp_path):
    d = _valid()
    d["roles"]["scout"] = []
    with pytest.raises(ConfigError, match="scout"):
        load_config(_write(tmp_path, d))
