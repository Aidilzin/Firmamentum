"""config.yaml loading (pydantic-validated), --model role=model overrides, repo lookup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

ROLE_NAMES = ("planner", "scout", "builder", "reviewer", "repair")
RiskFlag = Literal[
    "merge_to_main",
    "delete_outside_worktree",
    "touches_auth_payments_secrets_env",
    "db_migration_or_schema",
    "new_dependency",
    "tests_fail_after_3_repairs",
    "tests_removed",
    "plan_needs_input",
    "all_agents_rate_limited",
]


class ConfigError(ValueError):
    pass


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Literal["claude", "agy"]
    model: str | None = None  # None = the CLI's default model


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    test_command: str
    retest_command: str | None = None  # repair rounds only; never from a plan


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: dict[str, RoleConfig]
    fallback_model: dict[str, str] = {}
    approval_rules: list[RiskFlag]
    repos: dict[str, RepoConfig] = {}
    review: Literal["per_step", "off"] = "per_step"
    max_repair_rounds: int = 3
    base_dir: Path = Path(".")  # directory of config.yaml; relative repo paths resolve against it

    @model_validator(mode="after")
    def _check_roles(self) -> Config:
        missing = [r for r in ROLE_NAMES if r not in self.roles]
        unknown = [r for r in self.roles if r not in ROLE_NAMES]
        if missing or unknown:
            raise ValueError(f"roles: missing {missing}, unknown {unknown}")
        return self


class ResolvedRepo(BaseModel):
    name: str | None
    path: Path
    test_command: str | None  # None for an unconfigured path: the planner proposes one
    retest_command: str | None = None


def default_config_path() -> Path:
    env = os.environ.get("ORCH_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config.yaml"  # repo root (src layout)


def load_config(path: Path | None = None) -> Config:
    p = Path(path) if path else default_config_path()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read config {p}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: expected a mapping at the top level")
    try:
        cfg = Config.model_validate({**data, "base_dir": p.resolve().parent})
    except ValueError as e:
        raise ConfigError(f"invalid config {p}: {e}") from e
    return cfg


def apply_overrides(cfg: Config, overrides: list[str]) -> Config:
    """Apply `role=model` strings. Returns a new Config; the agent of a role is unchanged."""
    roles = {k: v.model_copy() for k, v in cfg.roles.items()}
    for item in overrides:
        role, sep, model = item.partition("=")
        role, model = role.strip(), model.strip()
        if not sep or not role or not model:
            raise ConfigError(f"bad --model value {item!r}; expected role=model, e.g. planner=sonnet")
        if role not in roles:
            raise ConfigError(f"unknown role {role!r} in --model; valid roles: {', '.join(ROLE_NAMES)}")
        roles[role].model = model
    return cfg.model_copy(update={"roles": roles})


def resolve_repo(cfg: Config, name_or_path: str | None) -> ResolvedRepo:
    """`None` means the current directory. A configured name wins over a path of the same text."""
    if name_or_path and name_or_path in cfg.repos:
        r = cfg.repos[name_or_path]
        return ResolvedRepo(name=name_or_path, path=(cfg.base_dir / r.path).resolve(), test_command=r.test_command, retest_command=r.retest_command)
    path = Path(name_or_path).resolve() if name_or_path else Path.cwd().resolve()
    if not path.is_dir():
        raise ConfigError(f"repo {name_or_path!r} is neither a configured name nor an existing directory")
    for name, r in cfg.repos.items():
        if (cfg.base_dir / r.path).resolve() == path:
            return ResolvedRepo(name=name, path=path, test_command=r.test_command, retest_command=r.retest_command)
    return ResolvedRepo(name=None, path=path, test_command=None)
