"""`orch doctor`: setup checks. Each check yields ok / warn / err plus a one-line fix.

Nothing here reads a credential file or prints a secret: environment values are masked and only the NAMES of
settings keys are shown. The login type comes from the official `claude auth status`, which makes no model call.
Every outside dependency (env, home dir, PATH lookup, subprocesses, registry) is injectable so tests never touch the machine.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from . import budget, versions, workspace
from .adapters.claude_code import SCRUBBED_ENV
from .adapters.process import child_env, run_process
from .config import Config, ConfigError, load_config, default_config_path
from .view import short_model

OK, WARN, ERR = "ok", "warn", "err"
LEVEL_SYM = {OK: "ok", WARN: "warn", ERR: "err"}  # theme.sym keys


@dataclass
class Check:
    name: str
    level: str
    detail: str = ""
    fix: str = ""


def mask(value: str) -> str:
    """Never show a secret: a short prefix and the length only."""
    if not value:
        return "(empty)"
    return f"{value[:3]}… ({len(value)} chars)" if len(value) > 8 else f"… ({len(value)} chars)"


def _windows_user_env(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def _claude_auth_status() -> dict | None:
    """`claude auth status --json`: a local status command, no model call. None when it cannot be read."""
    env = {k: v for k, v in child_env().items() if k not in SCRUBBED_ENV}
    out = run_process(["claude", "auth", "status", "--json"], cwd=Path.cwd(), env=env, timeout_s=20)
    if out.not_found or out.timed_out or out.returncode not in (0, 1):
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@dataclass
class Context:
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    home: Path = field(default_factory=Path.home)
    which: Callable[[str], str | None] = shutil.which
    version: Callable[[str], str] = versions.cli_version
    auth_status: Callable[[], dict | None] = _claude_auth_status
    user_env: Callable[[str], str | None] = _windows_user_env
    config_path: Path | None = None
    db_path: Path | None = None
    wt_root: Callable[[], Path] = workspace.worktree_root
    python: tuple[int, int] = field(default_factory=lambda: sys.version_info[:2])
    has_tzdata: Callable[[], bool] = lambda: importlib.util.find_spec("tzdata") is not None
    is_git_repo: Callable[[Path], bool] = lambda p: workspace.git(p, "rev-parse", "--git-dir", check=False).returncode == 0


def _writable(directory: Path) -> str | None:
    """None when a file can be created in `directory` (created if missing), else the reason."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".orch-doctor-", delete=True):
            pass
    except OSError as e:
        return str(e)
    return None


# ---------- individual checks ----------


def check_python(ctx: Context) -> Check:
    v = ".".join(map(str, ctx.python))
    if ctx.python >= (3, 12):
        return Check("python", OK, v)
    return Check("python", ERR, f"{v}, need 3.12 or newer", "install Python 3.12+ (uv python install 3.12)")


def check_tool(ctx: Context, name: str, level_missing: str, fix: str) -> Check:
    path = ctx.which(name)
    if not path:
        return Check(name, level_missing, "not found on PATH", fix)
    return Check(name, OK, path)


def check_cli(ctx: Context, name: str, level_missing: str, fix: str) -> Check:
    path = ctx.which(name)
    if not path:
        return Check(name, level_missing, "not found on PATH", fix)
    ver = ctx.version(name)
    if ver == "not found" or ver.startswith("error"):
        return Check(name, ERR, f"{path}: {ver}", f"reinstall {name} or fix its install; `{name} --version` must work")
    return Check(name, OK, f"{ver} ({path})")


def check_process_env(ctx: Context) -> list[Check]:
    bad = [(k, ctx.env.get(k)) for k in SCRUBBED_ENV if ctx.env.get(k)]
    if not bad:
        return [Check("process env", OK, "no ANTHROPIC_API_KEY / AUTH_TOKEN / BASE_URL")]
    return [Check(f"env {k}", ERR, f"set in this process: {mask(v or '')}",
                  "remove it: Remove-Item Env:" + k + " (orch scrubs it for claude anyway, but other tools do not)") for k, v in bad]  # fmt: skip


def check_user_env(ctx: Context) -> list[Check]:
    if sys.platform != "win32" and ctx.user_env is _windows_user_env:
        return []
    bad = [(k, ctx.user_env(k)) for k in SCRUBBED_ENV]
    bad = [(k, v) for k, v in bad if v]
    if not bad:
        return [Check("user env", OK, "no ANTHROPIC_* in the Windows user environment")]
    return [Check(f"user env {k}", ERR, f"set for your Windows user: {mask(v or '')}",
                  f"[Environment]::SetEnvironmentVariable('{k}', $null, 'User'), then open a new terminal") for k, v in bad]  # fmt: skip


def check_claude_settings(ctx: Context) -> list[Check]:
    p = ctx.home / ".claude" / "settings.json"
    name = "~/.claude/settings.json"
    if not p.is_file():
        return [Check(name, OK, "not present")]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [Check(name, WARN, f"cannot parse: {e}", "fix or delete the file, then run doctor again")]
    if not isinstance(data, dict):
        return [Check(name, WARN, "not a JSON object", "fix the file")]
    out = []
    env_block = data.get("env")
    keys = sorted(k for k in env_block if isinstance(k, str) and k.startswith("ANTHROPIC_")) if isinstance(env_block, dict) else []
    if keys:
        out.append(Check(f"{name} env", ERR, "sets " + ", ".join(keys) + " (values hidden); points Claude Code away from your Pro login",
                         "remove those keys from the \"env\" block, see README"))  # fmt: skip
    model = data.get("model")
    if isinstance(model, str) and ":" in model:
        out.append(Check(f"{name} model", ERR, f"model {model!r} points Claude Code at a local model, see README",
                         "remove the \"model\" key (or set a Claude model such as sonnet)"))  # fmt: skip
    return out or [Check(name, OK, "no ANTHROPIC_* env block, no local model")]


def check_login(ctx: Context) -> list[Check]:
    out = []
    if not ctx.which("claude"):
        out.append(Check("claude login", WARN, "skipped: claude not found", "install Claude Code first"))
    else:
        st = ctx.auth_status()
        if st is None:
            out.append(Check("claude login", WARN, "could not read `claude auth status`", "check manually: run claude, then /status"))
        elif not st.get("loggedIn"):
            out.append(Check("claude login", ERR, "not logged in", "run: claude auth login (choose your Claude Pro account)"))
        elif st.get("authMethod") != "claude.ai" or st.get("apiProvider", "firstParty") != "firstParty":
            out.append(Check("claude login", ERR, f"login type is {st.get('authMethod')}/{st.get('apiProvider')}, not a Pro login",
                             "run: claude auth logout, then claude auth login with your Claude Pro account"))  # fmt: skip
        else:
            plan = st.get("subscriptionType")
            out.append(Check("claude login", OK if plan in (None, "pro", "max") else WARN, f"claude.ai login" + (f", plan {plan}" if plan else ""),
                             "" if plan in (None, "pro", "max") else "expected a Pro login"))  # fmt: skip
    if ctx.which("agy"):  # agy --help lists no auth/status command, so there is nothing safe to call
        out.append(Check("agy login", WARN, "agy has no status command that avoids a model call",
                         "check manually: start agy and confirm it shows your Google AI Pro account"))  # fmt: skip
    return out


def _agy_only_roles(cfg: Config) -> list[str]:
    return [r for r, entries in cfg.roles.items() if entries and all(e.agent == "agy" for e in entries)]


def check_agy(ctx: Context, cfg: Config | None) -> Check:
    path = ctx.which("agy")
    fix = "install Antigravity CLI (agy) and make sure it is on PATH"
    only = _agy_only_roles(cfg) if cfg else []
    level_missing = ERR if only else WARN
    if not path:
        why = f"; role(s) {', '.join(only)} have only agy entries" if only else " (optional: scout and the reviewer's third fallback)"
        return Check("agy", level_missing, "not found on PATH" + why, fix)
    return check_cli(ctx, "agy", level_missing, fix)


def check_config(ctx: Context) -> tuple[Check, Config | None]:
    path = ctx.config_path or default_config_path()
    try:
        cfg = load_config(path)
    except ConfigError as e:
        return Check("config.yaml", ERR, str(e)[:300], f"fix {path}"), None
    return Check("config.yaml", OK, f"{path} ({len(cfg.repos)} repo(s))"), cfg


def check_repos(ctx: Context, cfg: Config) -> list[Check]:
    out = []
    for name, r in cfg.repos.items():
        path = (cfg.base_dir / r.path).resolve()
        label = f"repo {name}"
        if not path.is_dir():
            out.append(Check(label, ERR, f"{path} does not exist", f"fix repos.{name}.path in config.yaml"))
        elif not ctx.is_git_repo(path):
            out.append(Check(label, ERR, f"{path} is not a git repo", f"run: git init in {path}"))
        elif not (r.test_command or "").strip():
            out.append(Check(label, ERR, "no test_command", f"add repos.{name}.test_command in config.yaml"))
        else:
            out.append(Check(label, OK, f"{path}, tests: {r.test_command}"))
    return out


def check_paths(ctx: Context) -> list[Check]:
    out = []
    wt = ctx.wt_root()
    err = _writable(wt)
    out.append(Check("worktree root", OK, str(wt)) if err is None else Check("worktree root", ERR, f"{wt} not writable: {err}", "fix the folder permissions"))  # fmt: skip
    db = ctx.db_path or budget.default_db_path()
    err = _writable(db.parent)
    if err is None and db.exists() and not os.access(db, os.W_OK):
        err = "file is read-only"
    out.append(Check("usage DB", OK, str(db)) if err is None else Check("usage DB", ERR, f"{db} not writable: {err}", "fix the folder permissions"))  # fmt: skip
    return out


def check_tzdata(ctx: Context) -> Check:
    if ctx.has_tzdata():
        return Check("tzdata", OK, "importable")
    return Check("tzdata", WARN, "not importable; local times may be wrong on Windows", "run: uv sync")


def check_cooldowns(ctx: Context) -> list[Check]:
    try:
        items = budget.active_cooldowns(db_path=ctx.db_path)
    except Exception as e:  # unreadable DB is reported by the usage DB check as well
        return [Check("cooldowns", WARN, f"cannot read: {e}", "")]
    if not items:
        return [Check("cooldowns", OK, "none active")]
    return [Check(f"cooldown {c.agent}:{c.model}", WARN, f"until {c.until.astimezone():%Y-%m-%d %H:%M}",
                  "wait, or clear it: orch cooldowns --clear" + f" {c.agent}") for c in items]  # fmt: skip


def run_checks(ctx: Context | None = None) -> list[Check]:
    ctx = ctx or Context()
    cfg_check, cfg = check_config(ctx)
    checks = [
        check_python(ctx),
        check_tool(ctx, "uv", WARN, "install uv: https://docs.astral.sh/uv/"),
        check_tool(ctx, "git", ERR, "install Git for Windows and put it on PATH"),
        check_cli(ctx, "claude", ERR, "install Claude Code and make sure `claude` is on PATH"),
        check_agy(ctx, cfg),
        *check_process_env(ctx),
        *check_user_env(ctx),
        *check_claude_settings(ctx),
        *check_login(ctx),
        cfg_check,
        *(check_repos(ctx, cfg) if cfg else []),
        *check_paths(ctx),
        check_tzdata(ctx),
        *check_cooldowns(ctx),
    ]
    return checks


def live_checks(get_adapter: Callable[[str], object], run: Callable | None = None, only: tuple[str, ...] = ("claude", "agy")) -> list[Check]:
    """One tiny "Reply PONG" call per CLI (cheapest model) to prove the login works. Uses quota; --live only."""
    from .adapters import AgentRequest

    run = run or budget.run_agent
    out = []
    for name in only:
        model = "sonnet" if name == "claude" else None
        res = run(get_adapter(name), AgentRequest(prompt="Reply PONG", cwd=Path.cwd(), model=model))
        label = f"live {name}"
        if res.ok:
            out.append(Check(label, OK, f"replied ({short_model(f'{name}:{model or 'default'}')}, {res.total_tokens} tokens)"))
        else:
            out.append(Check(label, ERR, f"failed: {res.error_kind.value}", f"run `{name}` yourself to see the login or limit problem"))
    return out


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.level == ERR for c in checks) else 0
