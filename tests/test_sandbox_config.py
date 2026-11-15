"""Sandbox settings in config.yaml (docs/sandbox-design.md). Pure config tests: no Docker, no git."""

import pytest
import yaml

from orch.config import ConfigError, default_config_path, load_config, resolve_repo

CONFIG_FILE = default_config_path().parent / "config.yaml"


def _valid():
    return yaml.safe_load(open(CONFIG_FILE, encoding="utf-8"))


def _load(tmp_path, data=None, text=None):
    p = tmp_path / "config.yaml"
    p.write_text(text if text is not None else yaml.safe_dump(data), encoding="utf-8")
    return load_config(p)


def test_unconfigured_repo_resolves_to_docker(tmp_path):
    """The default must never change silently: a path that is not in config.yaml is sandboxed."""
    cfg = load_config(CONFIG_FILE)
    target = resolve_repo(cfg, str(tmp_path))
    assert target.name is None
    assert target.sandbox == "docker"
    assert target.image == "orch-sbx:py312"


def test_a_configured_repo_without_a_sandbox_key_is_docker(tmp_path):
    d = _valid()
    d["repos"] = {"demo": {"path": str(tmp_path), "test_command": "pytest -q"}}
    cfg = _load(tmp_path, d)
    assert cfg.repos["demo"].sandbox == "docker"
    assert resolve_repo(cfg, "demo").sandbox == "docker"


def test_top_level_defaults():
    cfg = load_config(CONFIG_FILE)
    assert (cfg.sandbox.image, cfg.sandbox.memory, cfg.sandbox.cpus) == ("orch-sbx:py312", "4g", 4)


def test_placeholders_on_the_real_repos_until_phase_2():
    """Sandbox Phase 2 removes `sandbox: "off"` from config.yaml and flips this test to `== "docker"`."""
    cfg = load_config(CONFIG_FILE)
    assert cfg.repos["orch"].sandbox == "off"
    assert cfg.repos["practice"].sandbox == "off"


@pytest.mark.parametrize("written", ["off", '"off"', "'off'"])
def test_off_in_every_yaml_spelling(tmp_path, written):
    """A bare `off` is the boolean False in YAML 1.1; it must still mean the string "off"."""
    text = (
        "roles: {planner: [claude:sonnet], scout: [agy:default], builder: [claude:sonnet],"
        " reviewer: [claude:sonnet], repair: [claude:sonnet]}\n"
        "approval_rules: [merge_to_main]\n"
        f"repos:\n  demo: {{path: '.', test_command: 'pytest -q', sandbox: {written}}}\n"
    )
    cfg = _load(tmp_path, text=text)
    assert cfg.repos["demo"].sandbox == "off"
    assert resolve_repo(cfg, "demo").sandbox == "off"


@pytest.mark.parametrize("bad", ["on", True, "no", "none", "podman", ""])
def test_bad_sandbox_values_are_rejected(tmp_path, bad):
    d = _valid()
    d["repos"]["orch"]["sandbox"] = bad
    with pytest.raises(ConfigError, match="sandbox"):
        _load(tmp_path, d)


def test_repo_image_overrides_the_top_level_image(tmp_path):
    d = _valid()
    d["sandbox"] = {"image": "orch-sbx:custom", "memory": "2g", "cpus": 2}
    d["repos"]["practice"]["image"] = "orch-sbx:php83"
    cfg = _load(tmp_path, d)
    assert resolve_repo(cfg, "orch").image == "orch-sbx:custom"
    assert resolve_repo(cfg, "practice").image == "orch-sbx:php83"
    assert resolve_repo(cfg, str(tmp_path)).image == "orch-sbx:custom"  # unconfigured path gets the default too
    assert (cfg.sandbox.memory, cfg.sandbox.cpus) == ("2g", 2)


@pytest.mark.parametrize("image", ["--privileged", "-v", "a b", "x;rm", "", "a\nb"])
def test_image_names_that_could_be_read_as_docker_flags_are_rejected(tmp_path, image):
    d = _valid()
    d["sandbox"] = {"image": image}
    with pytest.raises(ConfigError):
        _load(tmp_path, d)
    d = _valid()
    d["repos"]["orch"]["image"] = image
    with pytest.raises(ConfigError):
        _load(tmp_path, d)


@pytest.mark.parametrize("memory", ["4", "4gb", "0g", "-1g", "g", "4 g"])
def test_bad_memory_is_rejected(tmp_path, memory):
    d = _valid()
    d["sandbox"] = {"memory": memory}
    with pytest.raises(ConfigError, match="memory"):
        _load(tmp_path, d)


@pytest.mark.parametrize("cpus", [0, -1, "many"])
def test_bad_cpus_are_rejected(tmp_path, cpus):
    d = _valid()
    d["sandbox"] = {"cpus": cpus}
    with pytest.raises(ConfigError):
        _load(tmp_path, d)


def test_unknown_sandbox_keys_are_rejected(tmp_path):
    d = _valid()
    d["sandbox"] = {"network": "host"}
    with pytest.raises(ConfigError):
        _load(tmp_path, d)
