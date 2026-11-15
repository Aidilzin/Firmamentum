"""config.yaml loading (pydantic-validated), --model role=model overrides, repo lookup."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class Entry(BaseModel):
    """One routing choice: an agent plus a model (None = the CLI's default model)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent: Literal["claude", "agy"]
    model: str | None = None

    @property
    def label(self) -> str:
        return f"{self.agent}:{self.model or 'default'}"


RoleConfig = Entry  # old name


def parse_entry(value: object) -> Entry:
    """`claude:opus`, `agy:default`, bare `claude`, or the old {agent, model} mapping."""
    if isinstance(value, Entry):
        return value
    if isinstance(value, str):
        agent, _, model = value.strip().partition(":")
        model = model.strip()
        return Entry(agent=agent.strip(), model=None if model in ("", "default") else model)  # type: ignore[arg-type]
    if isinstance(value, dict):
        return Entry(**value)
    raise ValueError(f"bad role entry {value!r}; expected 'agent:model'")


DEFAULT_SANDBOX_IMAGE = "orch-sbx:py312"
SandboxKind = Literal["docker", "off"]
_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")  # cannot start with `-`, so it is never read as a docker flag


def _image(v: str) -> str:
    if not _IMAGE_RE.fullmatch(v):
        raise ValueError(f"image {v!r}: expected a docker image name such as orch-sbx:py312")
    return v


class SandboxConfig(BaseModel):
    """Top-level sandbox defaults (docs/sandbox-design.md). `memory` and `cpus` are limits for one test container."""

    model_config = ConfigDict(extra="forbid")
    image: str = DEFAULT_SANDBOX_IMAGE
    memory: str = "4g"
    cpus: int = Field(4, ge=1)

    @field_validator("image")
    @classmethod
    def _image_name(cls, v: str) -> str:
        return _image(v)

    @field_validator("memory")
    @classmethod
    def _memory(cls, v: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*[kmg]", v):
            raise ValueError(f"memory {v!r}: expected a number and k, m or g, e.g. 4g")
        return v


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    test_command: str
    retest_command: str | None = None  # repair rounds only; never from a plan
    env_passthrough: list[str] = []  # extra env var names workers and test runs may see (default: allow-list only)
    # `docker`: agent-written tests run in a container. `off` must be written out on purpose; it runs them on this machine.
    sandbox: SandboxKind = "docker"
    image: str | None = None  # a different sandbox image for this repo (default: the top-level sandbox.image)

    @field_validator("sandbox", mode="before")
    @classmethod
    def _sandbox_off(cls, v: object) -> object:
        # YAML 1.1 reads a bare `off` as the boolean False. Accept that as "off"; `on`/`yes`/true is not a sandbox kind.
        return "off" if v is False else v

    @field_validator("image")
    @classmethod
    def _image_name(cls, v: str | None) -> str | None:
        return None if v is None else _image(v)


class UiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["dark", "light"] = "dark"


class PostChecksConfig(BaseModel):
    """Changed-file patterns that stop a step for approval (see checks.py). Globs on repo-relative paths: a pattern
    without `/` matches the file name at any depth, one with `/` the whole path (`**` crosses folders)."""

    model_config = ConfigDict(extra="forbid")
    protected: list[str] = [".env*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", ".npmrc"]
    protected_words: list[str] = ["secret", "credential", "token", "password", "auth", "payment"]  # whole words in the path
    executes_later: list[str] = [
        ".claude/**", "CLAUDE.md", "AGENTS.md", ".mcp.json", ".github/workflows/**", ".githooks/**", ".husky/**",
        "Makefile", "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", ".gitattributes", ".gitmodules",
    ]  # fmt: skip
    dependency: list[str] = [
        "pyproject.toml", "uv.lock", "requirements*.txt", "package.json", "package-lock.json", "pnpm-lock.yaml",
        "yarn.lock", "composer.json", "composer.lock",
    ]  # fmt: skip
    migration: list[str] = ["**/migrations/**", "database/migrations/**", "alembic/**"]
    self_hosting: list[str] = ["config.yaml", "src/orch/**"]  # only checked when the target repo is orch itself


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ui: UiConfig = UiConfig()
    post_checks: PostChecksConfig = PostChecksConfig()
    sandbox: SandboxConfig = SandboxConfig()
    roles: dict[str, list[Entry]]
    fallback_model: dict[str, str] = {}  # superseded by role lists; accepted and ignored
    cooldown_minutes: int = 60
    approval_rules: list[RiskFlag]
    repos: dict[str, RepoConfig] = {}
    review: Literal["per_step", "off"] = "per_step"
    max_repair_rounds: int = 3
    # agy's read-only mode is only partly verified (docs/probe-results.md), so agy reviews only when this is true.
    allow_agy_reviewer: bool = False
    base_dir: Path = Path(".")  # directory of config.yaml; relative repo paths resolve against it

    @field_validator("roles", mode="before")
    @classmethod
    def _parse_roles(cls, v: object) -> object:
        if not isinstance(v, dict):
            return v
        out = {}
        for role, items in v.items():
            items = items if isinstance(items, list) else [items]  # old single-entry form
            out[role] = [parse_entry(i) for i in items]
        return out

    @model_validator(mode="after")
    def _check_roles(self) -> Config:
        missing = [r for r in ROLE_NAMES if r not in self.roles]
        unknown = [r for r in self.roles if r not in ROLE_NAMES]
        if missing or unknown:
            raise ValueError(f"roles: missing {missing}, unknown {unknown}")
        for role, entries in self.roles.items():
            if not entries:
                raise ValueError(f"roles.{role}: needs at least one entry")
        if not self.allow_agy_reviewer and all(e.agent == "agy" for e in self.roles["reviewer"]):
            raise ValueError("roles.reviewer: only agy entries, but allow_agy_reviewer is false; add a claude entry or opt in")
        for role in ("builder", "repair"):
            bad = [e.label for e in self.roles[role] if e.agent != "claude"]
            if bad:
                raise ValueError(f"roles.{role}: only claude entries allowed (agy has no tool allow-list), got {bad}")
        return self


class ResolvedRepo(BaseModel):
    name: str | None
    path: Path
    test_command: str | None  # None for an unconfigured path: the planner proposes one
    retest_command: str | None = None
    env_passthrough: list[str] = []
    sandbox: SandboxKind = "docker"  # an unconfigured path is sandboxed too: there is no silent opt-out
    image: str = DEFAULT_SANDBOX_IMAGE


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
    """Apply `role=model` strings: that model goes first for the role's first agent. Returns a new Config."""
    roles = {k: list(v) for k, v in cfg.roles.items()}
    for item in overrides:
        role, sep, model = item.partition("=")
        role, model = role.strip(), model.strip()
        if not sep or not role or not model:
            raise ConfigError(f"bad --model value {item!r}; expected role=model, e.g. planner=sonnet")
        if role not in roles:
            raise ConfigError(f"unknown role {role!r} in --model; valid roles: {', '.join(ROLE_NAMES)}")
        first = Entry(agent=roles[role][0].agent, model=model)
        roles[role] = [first] + [e for e in roles[role] if e != first]
    return cfg.model_copy(update={"roles": roles})


def resolve_repo(cfg: Config, name_or_path: str | None, cwd: Path | None = None) -> ResolvedRepo:
    """`None` means the current directory (`cwd` when given, else the process cwd); a relative path resolves against it too.
    A configured name wins over a path of the same text."""
    def configured(name: str, path: Path) -> ResolvedRepo:
        r = cfg.repos[name]
        return ResolvedRepo(name=name, path=path, test_command=r.test_command, retest_command=r.retest_command,
                            env_passthrough=r.env_passthrough, sandbox=r.sandbox, image=r.image or cfg.sandbox.image)  # fmt: skip

    if name_or_path and name_or_path in cfg.repos:
        return configured(name_or_path, (cfg.base_dir / cfg.repos[name_or_path].path).resolve())
    base = Path(cwd) if cwd is not None else Path.cwd()
    path = (base / name_or_path).resolve() if name_or_path else base.resolve()  # an absolute name_or_path wins over base
    if not path.is_dir():
        raise ConfigError(f"repo {name_or_path!r} is neither a configured name nor an existing directory")
    for name, r in cfg.repos.items():
        if (cfg.base_dir / r.path).resolve() == path:
            return configured(name, path)
    return ResolvedRepo(name=None, path=path, test_command=None, image=cfg.sandbox.image)
