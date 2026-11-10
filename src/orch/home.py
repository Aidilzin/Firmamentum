"""The `orch` home screen: wordmark, repos, which model each demon tries first, recent token use, cooldowns, latest run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.text import Text

from . import budget
from .config import ROLE_NAMES, Config
from .runs import RunInfo, list_runs
from .theme import Theme, demon_label
from .view import fmt_tokens, short_model

WORDMARK = "orch · Firmamentum v2"


@dataclass
class HomeData:
    repos: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)  # role -> first entry label
    tokens_5h: int = 0
    tokens_24h: int = 0
    cooldowns: list[str] = field(default_factory=list)
    latest: tuple[str | None, RunInfo] | None = None  # (repo name, run)
    example_repo: str | None = None
    config_error: str | None = None


def _total(since: datetime, db_path: Path | None) -> int:
    return sum(r.tokens for r in budget.summary(since, db_path))


def build_home(cfg: Config | None, db_path: Path | None = None, now: datetime | None = None, config_error: str | None = None) -> HomeData:
    now = now or datetime.now(timezone.utc)
    d = HomeData(config_error=config_error)
    try:
        d.tokens_5h = _total(now - timedelta(hours=5), db_path)
        d.tokens_24h = _total(now - timedelta(hours=24), db_path)
        d.cooldowns = [
            f"{c.agent}:{c.model} until {c.until.astimezone():%H:%M}" for c in budget.active_cooldowns(now, db_path)
        ]
    except Exception:  # an unreadable usage DB must not hide the rest of the screen
        pass
    if cfg is None:
        return d
    d.repos = list(cfg.repos)
    d.example_repo = d.repos[0] if d.repos else None
    d.models = {r: cfg.roles[r][0].label for r in ROLE_NAMES if cfg.roles.get(r)}
    best: tuple[str | None, RunInfo] | None = None
    for name, r in cfg.repos.items():
        for run in list_runs((cfg.base_dir / r.path).resolve())[:1]:  # newest first
            if best is None or run.run_id > best[1].run_id:  # ids start with a UTC timestamp
                best = (name, run)
    d.latest = best
    return d


def render_home(d: HomeData, theme: Theme) -> list[Text]:
    dim, text = theme.style("dim"), theme.style("text")
    out: list[Text] = []

    def row(label: str, value: str, style: str = text) -> None:
        t = Text()
        t.append(f"{label:<10}", style=dim)
        t.append(value, style=style)
        out.append(t)

    mark = Text()
    mark.append(theme.demon_glyph("planner") + " ", style=theme.demon_style("planner", bold=True))
    mark.append(WORDMARK, style=theme.demon_style("planner", bold=True))
    out += [mark, Text("")]
    if d.config_error:
        row("config", d.config_error[:100], theme.style("error"))
    else:
        row("repos", ", ".join(d.repos) or "(none configured)")
    for role, label in d.models.items():
        t = Text()
        t.append(f"{'':<10}", style=dim)
        t.append(theme.demon_glyph(role) + " ", style=theme.demon_style(role, bold=True))
        t.append(f"{demon_label(role):<10}", style=theme.demon_style(role, bold=True))
        t.append(f"{short_model(label)} ", style=text)
        t.append(f"({role})", style=dim)
        out.append(t)
    row("tokens", f"5h {fmt_tokens(d.tokens_5h)} · 24h {fmt_tokens(d.tokens_24h)}")
    if d.cooldowns:
        row("cooldowns", "; ".join(d.cooldowns), theme.style("warning"))
    else:
        row("cooldowns", "none")
    if d.latest:
        name, run = d.latest
        row("latest", f"{run.run_id[:46]}  {run.status}" + (f"  [{name}]" if name else ""))
    else:
        row("latest", "no runs yet")
    repo = d.example_repo or "<repo>"
    out += [Text(""), Text("try", style=dim)]
    for cmd in (
        f'orch plan "add a --verbose flag" --repo {repo}',
        f'orch run "fix the failing test" --repo {repo}',
        f"orch status <run-id> --repo {repo}",
        "orch usage --window 5h",
        "orch doctor",
    ):
        out.append(Text("  " + cmd, style=text))
    return out
