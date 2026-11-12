"""orch command line: ask, usage, version, plan, runs, show, exec, run, resume, approve, reject, answer, merge, status, diff, clean."""

from __future__ import annotations

import json
import re
import signal
import sys
import textwrap
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.text import Text

from .adapters import AgentAdapter, AgentRequest, AntigravityAdapter, ClaudeCodeAdapter
from . import budget
from .budget import run_agent, summary, summary_by_model
from .config import ConfigError, apply_overrides, load_config, resolve_repo
from . import services, versions
from .executor import ExecError, check_hidden_tests
from .theme import current_theme, demon_label, demon_name, resolve_theme, set_theme
from .ui import make_view, render_doctor, render_plain, render_rich
from . import doctor as doctor_mod
from . import home as home_mod
from .view import build_run_view, duration, fmt_elapsed
from .plan import format_plan
from .planner import make_plan
from .services import EXIT_CODES, next_actions
from .runs import RunNotFound, create_run, find_run, is_stale, list_runs, load_meta, load_plan, run_tokens, save_outcome, write_meta

app = typer.Typer(help="Orchestrate Claude Code and Antigravity CLIs.")


class AgentName(str, Enum):
    claude = "claude"
    agy = "agy"


def get_adapter(name: AgentName) -> AgentAdapter:
    """Adapter registry. Tests monkeypatch this to inject a FakeAdapter."""
    return {AgentName.claude: ClaudeCodeAdapter, AgentName.agy: AntigravityAdapter}[name]()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    theme: Optional[str] = typer.Option(None, "--theme", help="Colour theme: dark or light (default: config ui.theme, ORCH_THEME, else dark)."),
    ascii_: bool = typer.Option(False, "--ascii", help="ASCII glyphs only (automatic when the terminal cannot show the star glyphs)."),
) -> None:
    """Choose the theme; with no subcommand, show the home screen."""
    cfg, cfg_error = None, None
    try:
        cfg = load_config()
    except ConfigError as e:
        cfg_error = str(e)
    try:
        set_theme(resolve_theme(theme, ascii_, cfg.ui.theme if cfg else None))
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)
    if ctx.invoked_subcommand is None:
        console = Console()
        for line in home_mod.render_home(home_mod.build_home(cfg, config_error=cfg_error), current_theme()):
            console.print(line)


@app.command()
def ask(
    agent: AgentName = typer.Option(..., "--agent", help="Which CLI to use."),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Prompt text."),
    file: Optional[Path] = typer.Option(None, "--file", help="Read the prompt from a file."),
    model: Optional[str] = typer.Option(None, "--model", help="Passed to the CLI's --model."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Working directory for the CLI."),
    json_out: bool = typer.Option(False, "--json", help="Print the AgentResult as JSON."),
) -> None:
    if (prompt is None) == (file is None):
        typer.echo("Error: give exactly one of --prompt or --file.", err=True)
        raise typer.Exit(2)
    text = prompt if prompt is not None else file.read_text(encoding="utf-8")  # type: ignore[union-attr]
    request = AgentRequest(prompt=text, cwd=cwd.resolve(), model=model)
    result = run_agent(get_adapter(agent), request)
    if json_out:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(result.text)
        typer.echo(
            f"[{result.agent} | {result.model or 'default'} | {result.duration_ms / 1000:.1f}s"
            f" | {result.total_tokens} tokens]"
        )
    if not result.ok:
        typer.echo(f"Error: {result.error_kind.value}", err=True)
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print orch, claude and agy versions."""
    typer.echo(f"orch {versions.orch_version()}")
    for name in ("claude", "agy"):
        typer.echo(f"{name}: {versions.cli_version(name)}")


@app.command()
def usage(
    days: int = typer.Option(7, "--days", help="Look back this many days."),
    window: Optional[str] = typer.Option(None, "--window", help="Look back this long instead, e.g. 5h, 24h, 7d, 30m."),
    json_out: bool = typer.Option(False, "--json", help="Print the summary as JSON."),
) -> None:
    """Print call, token and failure totals per agent and model (--json for JSON output)."""
    now = datetime.now(timezone.utc)
    if window is not None:
        try:
            delta = parse_window(window)
        except ValueError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(2)
        label = window
    else:
        delta, label = timedelta(days=days), f"{days} day(s)"
    since = now - delta
    rows = summary(since)
    by_model = summary_by_model(since)
    cancelled = budget.unknown_token_calls(since)
    if json_out:
        payload = {
            "days": days,
            "window": window,
            "since": since.isoformat(timespec="seconds"),
            "agents": [asdict(r) for r in rows],
            "models": [asdict(r) for r in by_model],
            "cancelled_calls": cancelled,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"Usage, last {label}")
    typer.echo(f"{'agent':<8}{'model':<22}{'calls':>8}{'tokens':>12}{'failures':>10}")
    if not by_model:
        typer.echo("(no calls logged)")
    for r in by_model:
        typer.echo(f"{r.agent:<8}{r.model:<22}{r.calls:>8}{r.tokens:>12}{r.failures:>10}")
    if cancelled:
        typer.echo(f"{cancelled} call(s) cancelled, tokens unknown (counted as 0)")


def parse_window(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", text.lower())
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"bad --window {text!r}; use a number and m, h or d, e.g. 5h, 24h, 7d")
    n, unit = int(m.group(1)), m.group(2)
    key = {"m": "minutes", "h": "hours", "d": "days"}[unit]
    return timedelta(**{key: n})


def _parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


@app.command()
def cooldowns(
    agent: Optional[AgentName] = typer.Argument(None, help="With --clear: only this agent."),
    clear: bool = typer.Option(False, "--clear", help="Remove cooldowns (all, or only AGENT's)."),
) -> None:
    """List active cooldowns; --clear [agent] removes them (when you know the limit has reset)."""
    if clear:
        n = budget.clear_cooldowns(agent.value if agent else None)
        typer.echo(f"Cleared {n} cooldown(s)" + (f" for {agent.value}." if agent else "."))
        return
    items = budget.active_cooldowns()
    if not items:
        typer.echo("(no active cooldowns)")
    for c in items:
        typer.echo(f"{c.agent}:{c.model if c.model != '*' else '*':<10} until {_local(c.until)}  {c.reason[:80]!r}")


def _planned_models(outcome, cfg, roles: list[str]) -> dict[str, str]:
    """Entry that actually served each role (falls back to the first configured entry when no call was made)."""
    used = {r["role"]: r["entry"] for r in outcome.routes}
    return {r: used.get(r) or cfg.roles[r][0].label for r in roles}


def _planning_stopped(outcome) -> None:
    """Print why planning stopped; exit 3 when it was a rate-limit pause, else 1."""
    typer.echo(f"Error: {outcome.error}", err=True)
    raise typer.Exit(3 if outcome.paused else 1)


def _repo_or_exit(cfg, repo_arg):
    try:
        return resolve_repo(cfg, repo_arg)
    except ConfigError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)


@app.command()
def plan(
    request: str = typer.Argument(..., help="What you want done."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
    scout: bool = typer.Option(False, "--scout", help="Run the scout role first (costs extra quota)."),
    model: Optional[list[str]] = typer.Option(None, "--model", help="role=model, repeatable."),
) -> None:
    """Ask the Planner for a plan and store it under .orch/runs/."""
    try:
        cfg = apply_overrides(load_config(), model or [])
    except ConfigError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)
    target = _repo_or_exit(cfg, repo)
    run_dir = create_run(target.path, request)
    outcome = make_plan(request, target, cfg, lambda name: get_adapter(AgentName(name)), use_scout=scout)
    models = _planned_models(outcome, cfg, (["scout"] if scout else []) + ["planner"])
    meta = save_outcome(run_dir, outcome, models)
    typer.echo(f"Run: {run_dir.name}")
    if outcome.plan is None:
        typer.echo(f"Details: {run_dir / 'planner_raw.json'}  [{outcome.total_tokens} tokens]", err=True)
        _planning_stopped(outcome)
    typer.echo(format_plan(outcome.plan))
    typer.echo(f"\n[{meta['status']} | {outcome.total_tokens} tokens | {len(outcome.results)} call(s)]")
    if outcome.plan.needs_input:
        for n in next_commands(run_dir, target.name):
            typer.echo(f"Next: {n}")
        raise typer.Exit(2)
    typer.echo(f"Next: {_cmd(run_dir.name, target.name, f'exec {run_dir.name}')}")


@app.command()
def runs(repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path.")) -> None:
    """List stored runs, newest first."""
    target = _repo_or_exit(load_config(), repo)
    items = list_runs(target.path)
    if not items:
        typer.echo("(no runs)")
    for r in items:
        typer.echo(f"{r.run_id:<60} {r.shown_status:<12} {r.created[:19].replace('T', ' ')}")


@app.command()
def show(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path."),
) -> None:
    """Print a stored plan."""
    target = _repo_or_exit(load_config(), repo)
    try:
        d = find_run(target.path, run_id)
    except RunNotFound as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    stored = load_plan(d)
    typer.echo(f"Run: {d.name}")
    if stored is None:
        typer.echo("(no plan stored: the run failed or is incomplete)")
        raise typer.Exit(1)
    typer.echo(format_plan(stored))


def _find_or_exit(target, run_id: str) -> Path:
    try:
        return find_run(target.path, run_id)
    except RunNotFound as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


def _or_exit(fn, *args, **kw):
    """Call a service; print an expected refusal and exit with its code."""
    try:
        return fn(*args, **kw)
    except services.ServiceError as e:
        typer.echo(f"Error: {e.message}", err=True)
        raise typer.Exit(e.code)


def _load_cfg(model: list[str] | None):
    try:
        return apply_overrides(load_config(), model or [])
    except ConfigError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)


def _cmd(run_id: str, repo_name: str | None, text: str) -> str:
    return f"orch {text}" + (f" --repo {repo_name}" if repo_name else "")


def next_commands(run_dir: Path, repo_name: str | None = None) -> list[str]:
    """The exact command(s) the human should run next, one line per stopped step."""
    return [str(a) for a in next_actions(load_meta(run_dir), run_dir, repo_name)]


def _status_table(run_dir: Path, repo_name: str | None = None) -> str:
    meta = load_meta(run_dir)
    total_time = duration(meta.get("started_at"), meta.get("ended_at"))
    shown = f"{meta.get('status')} (stale)" if is_stale(run_dir, meta) else meta.get("status")
    lines = [f"Run: {run_dir.name}  status: {shown}" + (f"  time: {fmt_elapsed(total_time)}" if total_time is not None else "")]
    if meta.get("models"):
        routed = {r["role"]: r for r in meta.get("routing") or []}
        lines.append("Planning models: " + ", ".join(
            f"{demon_name(r)}={m}" + (" (fallback)" if routed.get(r, {}).get("fallback") else "") for r, m in meta["models"].items()
        ))
    ex = meta.get("exec")
    if ex:
        lines.append(f"Branch: {ex['branch']}  Worktree: {ex['worktree']}")
    if meta.get("resume_after") and meta.get("status") == "paused":
        after = _parse_utc(meta["resume_after"])
        if after:
            lines.append(f"Paused until {_local(after)}")
    if meta.get("merged_sha"):
        lines.append(f"Merged into {meta.get('merged_into')} as {meta['merged_sha'][:8]}")
    lines.append(f"{'step':<6}{'status':<19}{'tokens':>9}  {'tests':<12}{'rounds':<8}{'review':<9}{'commit':<10}{'time':<8}title")
    for sid, s in (meta.get("steps") or {}).items():
        t = s.get("tests")
        tests = "-" if not t else ("pass" if t["passed"] else f"FAIL({t['exit_code']})")
        reviews = s.get("reviews") or []
        verdict = reviews[-1]["verdict"] if reviews else "-"
        rounds = str(s.get("rounds_total", 0))
        lines.append(
            f"{sid:<6}{s['status']:<19}{s.get('tokens', 0):>9}  {tests:<12}{rounds:<8}{verdict:<9}"
            f"{(s.get('commit') or '-')[:8]:<10}{fmt_elapsed(duration(s.get('started_at'), s.get('ended_at'))):<8}{s['title']}"
        )
        if s.get("roles_tokens"):
            lines.append("      tokens by role: " + ", ".join(f"{demon_name(r)}={n}" for r, n in s["roles_tokens"].items()))
        if s.get("roles_used"):
            lines.append("      models used: " + ", ".join(
                f"{demon_name(r)}={u['entry']}" + (" (fallback)" if u.get("fallback") else "") for r, u in s["roles_used"].items()
            ))  # fmt: skip
        for r in s.get("rounds") or []:
            lines.append(f"      round {r['n']}: {'+'.join(r['reasons'])}; {r['session']}; review={r.get('verdict') or '-'}"
                         + (f"; {r['outcome']}" if r.get("outcome") not in (None, "ok") else ""))  # fmt: skip
        for rv in reviews:
            minor = sum(1 for i in rv["issues"] if i["severity"] == "minor")
            lines.append(f"      review (round {rv['round']}): {rv['verdict']}; {len(rv['issues']) - minor} blocking, {minor} minor")
            for i in rv["issues"]:
                lines.append(textwrap.fill(f"[{i['severity']}] {i['file']}:{i.get('line') or '-'} {i['problem']}", width=100,
                                           initial_indent=" " * 8, subsequent_indent=" " * 12))
        for w in s.get("warnings") or []:
            lines.append(f"      warning: {w}")
        if s.get("question"):
            lines.append(f"      question: {s['question']}")
        if s.get("rule"):
            lines.append(f"      needs approval: {s['rule']} {s.get('flagged') or s.get('summary') or ''}")
        if s.get("error"):
            lines.append(f"      error: {s['error']}")
    if not meta.get("steps"):
        lines.append("(not started)")
    by_role, total = run_tokens(meta)
    roles = ", ".join(f"{demon_name(r)}={n:,}" for r, n in by_role.items())
    lines.append(f"Run tokens: {total:,} total" + (f" ({roles})" if roles else ""))
    nxt = next_commands(run_dir, repo_name)
    if nxt:
        lines.append("Needs from you:")
        lines += [f"  {n}" for n in nxt]
    return "\n".join(lines)


def _emit_status(run_dir: Path, repo_name: str | None = None, header: bool = True) -> None:
    """Constellation + demon lines (coloured on a terminal, plain otherwise), then the detailed step table."""
    view = build_run_view(load_meta(run_dir)) if header else None
    if view is not None and (view.steps or view.demons):  # a run that has not started has nothing to draw
        theme = current_theme()
        if sys.stdout.isatty():
            Console().print(render_rich(view, theme, with_footer=False))
        else:
            for line in render_plain(view, theme):
                typer.echo(line)
        typer.echo("")
    typer.echo(_status_table(run_dir, repo_name))


PLAIN_OPT = typer.Option(False, "--plain", help="Line-by-line output, no live table (automatic when not a terminal).")
HIDDEN_OPT = typer.Option(
    None, "--hidden-tests",
    help="PRACTICE USE ONLY. Directory of test files copied into the worktree's tests/ after the builder "
         "finishes a step and before the checks, so the builder never sees them (forces a real repair round). "
         "Must be outside the repo and any worktree; refused if a file name already exists in tests/.",
)  # fmt: skip


def _result_text(outcome) -> str:
    """`done` alone when the whole run finished; otherwise status, the step (if any) and the message."""
    if outcome.status == "done" and not outcome.step_id:
        return "done"
    return outcome.status + (f" ({outcome.step_id})" if outcome.step_id else "") + (f": {outcome.message}" if outcome.message else "")


def _install_sigint(ex):
    """Ctrl-C during a run: 1st press cancels (the agent is killed, the step saved), 2nd stops now (waits only for a
    commit or a save in progress), 3rd exits at once. The default handler is NOT put back while stopping.
    Returns the previous handler, or None when not on the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return None
    presses = 0

    def handler(signum, frame) -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            sys.stderr.write("\nstopping the run (Ctrl-C again: stop now)\n")
            ex.cancel()
        elif presses == 2:
            ex.stop_now()
        else:
            raise SystemExit(130)

    return signal.signal(signal.SIGINT, handler)


def _restore_sigint(previous) -> None:
    if previous is not None:
        signal.signal(signal.SIGINT, previous)


def _drive(target, run_dir: Path, cfg, action: str = "start", plain: bool = False, hidden_tests: Path | None = None, **kw) -> None:
    view = make_view(plain, typer.echo, {r: es[0].label for r, es in cfg.roles.items()}, current_theme())
    ex = services.build_executor(target, run_dir, cfg, lambda name: get_adapter(AgentName(name)), log=view.log,
                                 on_event=view.on_event if view.live else None, hidden_tests=hidden_tests)  # fmt: skip
    previous = _install_sigint(ex)
    try:
        with view:
            outcome = services.drive(ex, action, **kw)
    except services.ServiceError as e:
        typer.echo(f"Error: {e.message}", err=True)
        raise typer.Exit(e.code)
    finally:
        _restore_sigint(previous)
    typer.echo("")
    _emit_status(run_dir, target.name, header=not view.live)
    typer.echo(f"\nResult: {_result_text(outcome)}")
    nxt = next_commands(run_dir, target.name)
    for n in nxt:
        typer.echo(f"Next: {n}")
    view.show_stop(outcome.status, outcome.step_id, outcome.message, load_meta(run_dir), nxt)
    raise typer.Exit(EXIT_CODES.get(outcome.status, 1))


@app.command("exec")
def exec_cmd(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
    model: Optional[list[str]] = typer.Option(None, "--model", help="role=model, repeatable."),
    plain: bool = PLAIN_OPT,
    hidden_tests: Optional[Path] = HIDDEN_OPT,
) -> None:
    """Execute a stored plan in a new git worktree (no merge)."""
    cfg = _load_cfg(model)
    target = _repo_or_exit(cfg, repo)
    _drive(target, _find_or_exit(target, run_id), cfg, "start", plain=plain, hidden_tests=hidden_tests)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    force: bool = typer.Option(False, "--force", help="Resume even if resume_after is still in the future."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
    model: Optional[list[str]] = typer.Option(None, "--model", help="role=model, repeatable."),
    plain: bool = PLAIN_OPT,
) -> None:
    """Continue a started run from its first unfinished step."""
    cfg = _load_cfg(model)
    target = _repo_or_exit(cfg, repo)
    run_dir = _find_or_exit(target, run_id)
    after = services.resume_blocked_until(load_meta(run_dir), force)
    if after is not None:
        typer.echo(f"Run is paused until {_local(after)} (usage limit). Use --force to try earlier.", err=True)
        raise typer.Exit(3)
    _drive(target, run_dir, cfg, "resume", plain=plain)


@app.command()
def run(
    request: str = typer.Argument(..., help="What you want done."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
    scout: bool = typer.Option(False, "--scout", help="Run the scout role first (costs extra quota)."),
    model: Optional[list[str]] = typer.Option(None, "--model", help="role=model, repeatable."),
    plain: bool = PLAIN_OPT,
    hidden_tests: Optional[Path] = HIDDEN_OPT,
) -> None:
    """Plan, then execute, in one go."""
    cfg = _load_cfg(model)
    target = _repo_or_exit(cfg, repo)
    if not target.test_command:
        typer.echo("Error: this repo has no test_command in config.yaml; refusing to run.", err=True)
        raise typer.Exit(2)
    if hidden_tests is not None:  # refuse before any quota is spent
        try:
            check_hidden_tests(hidden_tests, target.path)
        except ExecError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(2)
    run_dir = create_run(target.path, request)
    outcome = make_plan(request, target, cfg, lambda name: get_adapter(AgentName(name)), use_scout=scout)
    roles = (["scout"] if scout else []) + ["planner"]
    models = _planned_models(outcome, cfg, roles)
    save_outcome(run_dir, outcome, models)
    typer.echo(f"Run: {run_dir.name}")
    if outcome.plan is None:
        _planning_stopped(outcome)
    typer.echo(format_plan(outcome.plan))
    if outcome.plan.needs_input:
        for n in next_commands(run_dir, target.name):
            typer.echo(f"Next: {n}")
        raise typer.Exit(2)
    _drive(target, run_dir, cfg, "start", plain=plain, hidden_tests=hidden_tests)


@app.command()
def approve(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    step: Optional[str] = typer.Option(None, "--step", help="Step id, e.g. s2 (optional if only one waits)."),
    retry: bool = typer.Option(False, "--retry", help="After repair rounds ran out: grant another set of rounds."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
    model: Optional[list[str]] = typer.Option(None, "--model", help="role=model, repeatable."),
    plain: bool = PLAIN_OPT,
) -> None:
    """Approve a step that is awaiting approval, then continue the run."""
    cfg = _load_cfg(model)
    target = _repo_or_exit(cfg, repo)
    _drive(target, _find_or_exit(target, run_id), cfg, "approve", plain=plain, step_id=step, retry=retry)


@app.command()
def reject(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    step: str = typer.Option(..., "--step", help="Step id, e.g. s2."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
) -> None:
    """Reject a step and stop the run."""
    cfg = _load_cfg(None)
    target = _repo_or_exit(cfg, repo)
    _drive(target, _find_or_exit(target, run_id), cfg, "reject", step_id=step)


def _answer_plan(target, cfg, run_dir: Path, text: str) -> None:
    stored = load_plan(run_dir)
    meta = load_meta(run_dir)
    if stored is None or not stored.needs_input:
        typer.echo("Error: this run's plan is not waiting for an answer.", err=True)
        raise typer.Exit(2)
    request = (run_dir / "request.md").read_text(encoding="utf-8").strip()
    clarification = f"\n\n## Clarification\nQuestion: {stored.question}\nAnswer: {text}"
    raw = run_dir / "planner_raw.json"
    if raw.exists():
        n = 1
        while (run_dir / f"planner_raw.{n}.json").exists():
            n += 1
        raw.rename(run_dir / f"planner_raw.{n}.json")
    outcome = make_plan(request + clarification, target, cfg, lambda name: get_adapter(AgentName(name)))
    models = meta.get("models") or _planned_models(outcome, cfg, ["planner"])
    new_meta = save_outcome(run_dir, outcome, models)
    new_meta["total_tokens"] += int(meta.get("total_tokens", 0))
    old_roles = meta.get("plan_role_tokens") or {"planner": int(meta.get("total_tokens", 0))}
    new_meta["plan_role_tokens"] = {
        r: new_meta["plan_role_tokens"].get(r, 0) + old_roles.get(r, 0) for r in {*new_meta["plan_role_tokens"], *old_roles}
    }
    write_meta(run_dir, new_meta)
    (run_dir / "request.md").write_text(request + clarification + "\n", encoding="utf-8")
    typer.echo(f"Run: {run_dir.name}")
    if outcome.plan is None:
        typer.echo(f"Error: {outcome.error}", err=True)
        raise typer.Exit(1)
    typer.echo(format_plan(outcome.plan))
    typer.echo(f"\n[{new_meta['status']} | {outcome.total_tokens} tokens | {len(outcome.results)} call(s)]")
    if outcome.plan.needs_input:
        for n in next_commands(run_dir, target.name):
            typer.echo(f"Next: {n}")
        raise typer.Exit(2)
    typer.echo(f"Next: {_cmd(run_dir.name, target.name, f'exec {run_dir.name}')}")


@app.command()
def answer(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    text: str = typer.Argument(..., help="Your answer."),
    step: Optional[str] = typer.Option(None, "--step", help="Step id (optional if only one step waits)."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
    model: Optional[list[str]] = typer.Option(None, "--model", help="role=model, repeatable."),
    plain: bool = PLAIN_OPT,
) -> None:
    """Answer a question: re-plan (plan needs input) or re-run the step (step needs input)."""
    cfg = _load_cfg(model)
    target = _repo_or_exit(cfg, repo)
    run_dir = _find_or_exit(target, run_id)
    meta = load_meta(run_dir)
    if not meta.get("steps") and step is None and meta.get("status") == "needs_input":
        _answer_plan(target, cfg, run_dir, text)
        return
    _drive(target, run_dir, cfg, "answer", plain=plain, step_id=step, text=text)


@app.command()
def merge(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    allow_main: bool = typer.Option(False, "--allow-main", help="Allow merging into main/master."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path. Default: cwd."),
) -> None:
    """Merge the run's branch into the main repo's current branch (--no-ff), then run the tests. Never pushes."""
    target = _repo_or_exit(load_config(), repo)
    r = _or_exit(services.merge, target, _find_or_exit(target, run_id), allow_main=allow_main)
    typer.echo(f"Merged {r.branch} into {r.target} as {r.sha[:8]} (not pushed).")
    if r.tests_passed is None:
        typer.echo(f"Tests after merge: could not run ({r.tests_detail})")
        raise typer.Exit(1)
    typer.echo(f"Tests after merge: {'PASS' if r.tests_passed else 'FAIL'} ({r.tests_detail})")
    if not r.tests_passed:
        typer.echo(f"The merge commit is kept. To undo it: git reset --hard {r.sha}^ (on {r.target}).")
        raise typer.Exit(1)


@app.command()
def status(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path."),
) -> None:
    """Step table: status, tokens, test result, commit."""
    target = _repo_or_exit(load_config(), repo)
    _emit_status(_find_or_exit(target, run_id), target.name)


@app.command()
def doctor(
    live: bool = typer.Option(False, "--live", help="Also make one tiny 'Reply PONG' call per CLI (uses a little plan quota)."),
) -> None:
    """Check the setup: tools, logins, leftover env vars, config, repos, paths. Exit 1 if anything is wrong."""
    theme = current_theme()
    console = Console()
    checks = doctor_mod.run_checks(doctor_mod.Context())
    if live:
        typer.echo("--live makes one tiny call to claude (sonnet) and one to agy; each costs ~13k-20k tokens of plan quota.")
        if typer.confirm("Make the calls?", default=False):
            checks += doctor_mod.live_checks(lambda name: get_adapter(AgentName(name)))
        else:
            typer.echo("Skipped the live calls.")
    for line in render_doctor(checks, theme):
        console.print(line)
    bad = sum(c.level == doctor_mod.ERR for c in checks)
    warn = sum(c.level == doctor_mod.WARN for c in checks)
    console.print("")
    console.print(Text(f"{len(checks)} checks: {bad} failed, {warn} to look at.",
                       style=theme.style("error" if bad else ("warning" if warn else "success"))))  # fmt: skip
    raise typer.Exit(doctor_mod.exit_code(checks))


@app.command()
def diff(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    step: Optional[str] = typer.Option(None, "--step", help="Only this step, e.g. s1."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path."),
) -> None:
    """Show the diff of a run (base..branch) or of one step."""
    target = _repo_or_exit(load_config(), repo)
    for part in _or_exit(services.diff_run, target, _find_or_exit(target, run_id), step):
        typer.echo((part.header or "") + part.text)


@app.command()
def clean(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path."),
) -> None:
    """Remove the run's worktree. The branch is kept."""
    target = _repo_or_exit(load_config(), repo)
    r = _or_exit(services.clean_run, target, _find_or_exit(target, run_id))
    if not r.executed:
        typer.echo("Nothing to clean: the run was never executed.")
    elif r.removed:
        typer.echo(f"Removed worktree {r.worktree}; branch {r.branch} kept.")
    else:
        typer.echo("No worktree to remove.")


if __name__ == "__main__":
    app()
