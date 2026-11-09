import pytest
import yaml

from orch.config import ConfigError, apply_overrides, default_config_path, load_config, resolve_repo


def test_repo_config_loads():
    cfg = load_config(default_config_path().parent / "config.yaml")
    assert cfg.roles["planner"].agent == "claude" and cfg.roles["planner"].model == "opus"
    assert cfg.roles["scout"].agent == "agy" and cfg.roles["scout"].model is None
    assert cfg.fallback_model == {"claude": "sonnet"}
    assert len(cfg.approval_rules) == 9
    assert cfg.repos["orch"].test_command == "uv run pytest -q"


def test_overrides_are_applied_and_repeatable():
    cfg = load_config()
    new = apply_overrides(cfg, ["planner=sonnet", "scout=gemini-x"])
    assert new.roles["planner"].model == "sonnet"
    assert new.roles["scout"].model == "gemini-x"
    assert cfg.roles["planner"].model == "opus"  # original untouched


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
    d["roles"]["planner"]["agent"] = "gpt"
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
