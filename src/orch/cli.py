"""orch command line: ask, usage, version, plan, runs, show, exec, run, resume, approve, reject, answer, merge, status, diff, clean."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from .adapters import AgentAdapter, AgentRequest, AntigravityAdapter, ClaudeCodeAdapter
from . import budget
from .budget import run_agent, summary, summary_by_model
from .config import ConfigError, apply_overrides, load_config, resolve_repo
from . import versions, workspace
from .executor import ExecError, ExecOutcome, Executor, check_hidden_tests
from .ui import make_view
from .merge import MergeError, merge_run
from .plan import format_plan
from .planner import make_plan
from .runs import RunNotFound, create_run, find_run, list_runs, load_meta, load_plan, run_tokens, save_outcome, write_meta

app = typer.Typer(help="Orchestrate Claude Code and Antigravity CLIs.", no_args_is_help=True)


class AgentName(str, Enum):
    claude = "claude"
    agy = "agy"


def get_adapter(name: AgentName) -> AgentAdapter:
    """Adapter registry. Tests monkeypatch this to inject a FakeAdapter."""
    return {AgentName.claude: ClaudeCodeAdapter, AgentName.agy: AntigravityAdapter}[name]()


@app.callback()
def _main() -> None:
    """Keeps `ask` and `usage` as explicit subcommands."""


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
    if json_out:
        payload = {
            "days": days,
            "window": window,
            "since": since.isoformat(timespec="seconds"),
            "agents": [asdict(r) for r in rows],
            "models": [asdict(r) for r in by_model],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"Usage, last {label}")
    typer.echo(f"{'agent':<8}{'model':<22}{'calls':>8}{'tokens':>12}{'failures':>10}")
    if not by_model:
        typer.echo("(no calls logged)")
    for r in by_model:
        typer.echo(f"{r.agent:<8}{r.model:<22}{r.calls:>8}{r.tokens:>12}{r.failures:>10}")


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
        typer.echo(f"{r.run_id:<60} {r.status:<12} {r.created[:19].replace('T', ' ')}")


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


def _load_cfg(model: list[str] | None):
    try:
        return apply_overrides(load_config(), model or [])
    except ConfigError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)


EXIT_CODES = {"done": 0, "failed": 1, "needs_input": 2, "awaiting_approval": 2, "paused": 3, "rejected": 1}


def _cmd(run_id: str, repo_name: str | None, text: str) -> str:
    return f"orch {text}" + (f" --repo {repo_name}" if repo_name else "")


def next_commands(run_dir: Path, repo_name: str | None = None) -> list[str]:
    """The exact command(s) the human should run next, one line per stopped step."""
    meta = load_meta(run_dir)
    rid = run_dir.name
    out: list[str] = []
    if meta.get("status") == "needs_input" and not meta.get("steps"):
        return [f'{_cmd(rid, repo_name, f"answer {rid} " + chr(34) + "<your answer>" + chr(34))}   # the plan needs an answer']
    for sid, st in (meta.get("steps") or {}).items():
        status, stage = st["status"], st.get("stage")
        if status == "awaiting_approval" and stage == "repairs_exhausted":
            out.append(f"{_cmd(rid, repo_name, f'approve {rid} --step {sid} --retry')}   # another set of repair rounds"
                       f"  (or: {_cmd(rid, repo_name, f'reject {rid} --step {sid}')})")  # fmt: skip
        elif status == "awaiting_approval":
            out.append(f"{_cmd(rid, repo_name, f'approve {rid} --step {sid}')}   # {st.get('rule')}"
                       f"  (or: {_cmd(rid, repo_name, f'reject {rid} --step {sid}')})")  # fmt: skip
        elif status == "needs_input":
            ans = f'answer {rid} --step {sid} "<your answer>"'
            out.append(f"{_cmd(rid, repo_name, ans)}   # {st.get('question')}")
        elif status == "failed":
            out.append(f"(step {sid} failed: {st.get('error')}; see {run_dir / 'steps' / sid}; start a new run)")
        elif status == "pending" and st.get("error") == "rate limited":
            out.append(f"{_cmd(rid, repo_name, f'resume {rid}')}   # after the usage limit resets")
    ex = meta.get("exec") or {}
    if ex.get("worktree") and Path(ex["worktree"]).is_dir():
        for sid, st in (meta.get("steps") or {}).items():
            if st["status"] in STOPPED_STATUSES and not st.get("commit"):
                out.append(f"{_cmd(rid, repo_name, f'diff {rid} --step {sid}')}   # the uncommitted change of this step")
    if meta.get("status") == "paused" and not out:
        out.append(_cmd(rid, repo_name, f"resume {rid}"))
    if meta.get("status") == "done" and not meta.get("merged_sha"):
        out.append(f"{_cmd(rid, repo_name, f'diff {rid}')}   # review, then: {_cmd(rid, repo_name, f'merge {rid}')}")
    return out


def _status_table(run_dir: Path, repo_name: str | None = None) -> str:
    meta = load_meta(run_dir)
    lines = [f"Run: {run_dir.name}  status: {meta.get('status')}"]
    if meta.get("models"):
        routed = {r["role"]: r for r in meta.get("routing") or []}
        lines.append("Planning models: " + ", ".join(
            f"{r}={m}" + (" (fallback)" if routed.get(r, {}).get("fallback") else "") for r, m in meta["models"].items()
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
    lines.append(f"{'step':<6}{'status':<19}{'tokens':>9}  {'tests':<12}{'rounds':<8}{'review':<9}{'commit':<10}title")
    for sid, s in (meta.get("steps") or {}).items():
        t = s.get("tests")
        tests = "-" if not t else ("pass" if t["passed"] else f"FAIL({t['exit_code']})")
        reviews = s.get("reviews") or []
        verdict = reviews[-1]["verdict"] if reviews else "-"
        rounds = str(s.get("rounds_total", 0))
        lines.append(
            f"{sid:<6}{s['status']:<19}{s.get('tokens', 0):>9}  {tests:<12}{rounds:<8}{verdict:<9}"
            f"{(s.get('commit') or '-')[:8]:<10}{s['title']}"
        )
        if s.get("roles_tokens"):
            lines.append("      tokens by role: " + ", ".join(f"{r}={n}" for r, n in s["roles_tokens"].items()))
        if s.get("roles_used"):
            lines.append("      models used: " + ", ".join(
                f"{r}={u['entry']}" + (" (fallback)" if u.get("fallback") else "") for r, u in s["roles_used"].items()
            ))  # fmt: skip
        for r in s.get("rounds") or []:
            lines.append(f"      round {r['n']}: {'+'.join(r['reasons'])}; {r['session']}; review={r.get('verdict') or '-'}"
                         + (f"; {r['outcome']}" if r.get("outcome") not in (None, "ok") else ""))  # fmt: skip
        for rv in reviews:
            minor = sum(1 for i in rv["issues"] if i["severity"] == "minor")
            lines.append(f"      review (round {rv['round']}): {rv['verdict']}; {len(rv['issues']) - minor} blocking, {minor} minor")
            for i in rv["issues"]:
                lines.append(f"        [{i['severity']}] {i['file']}:{i.get('line') or '-'} {i['problem']}")
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
    lines.append(f"Run tokens: {total:,} total (" + ", ".join(f"{r}={n:,}" for r, n in by_role.items()) + ")")
    nxt = next_commands(run_dir, repo_name)
    if nxt:
        lines.append("Needs from you:")
        lines += [f"  {n}" for n in nxt]
    return "\n".join(lines)


PLAIN_OPT = typer.Option(False, "--plain", help="Line-by-line output, no live table (automatic when not a terminal).")
HIDDEN_OPT = typer.Option(
    None, "--hidden-tests",
    help="PRACTICE USE ONLY. Directory of test files copied into the worktree's tests/ after the builder "
         "finishes a step and before the checks, so the builder never sees them (forces a real repair round). "
         "Must be outside the repo and any worktree; refused if a file name already exists in tests/.",
)  # fmt: skip


def _drive(target, run_dir: Path, cfg, action: str = "start", plain: bool = False, hidden_tests: Path | None = None, **kw) -> None:
    view = make_view(plain, typer.echo, {r: es[0].label for r, es in cfg.roles.items()})
    ex = Executor(target, run_dir, cfg, lambda name: get_adapter(AgentName(name)), log=view.log,
                  on_update=view.on_update, hidden_tests=hidden_tests)  # fmt: skip
    try:
        with view:
            outcome: ExecOutcome = getattr(ex, action)(**kw)
    except (ExecError, workspace.WorkspaceError, RunNotFound) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo("")
    typer.echo(_status_table(run_dir, target.name))
    typer.echo(f"\nResult: {outcome.status}" + (f" ({outcome.step_id}): {outcome.message}" if outcome.message else ""))
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
    meta = load_meta(run_dir)
    after = _parse_utc(meta.get("resume_after")) if meta.get("status") == "paused" else None
    if after is not None and after > datetime.now(timezone.utc) and not force:
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
    run_dir = _find_or_exit(target, run_id)
    try:
        r = merge_run(target, run_dir, allow_main=allow_main)
    except (MergeError, workspace.WorkspaceError, RunNotFound) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
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
    typer.echo(_status_table(_find_or_exit(target, run_id), target.name))


STOPPED_STATUSES = ("awaiting_approval", "needs_input", "failed")


def _uncommitted_header(sid: str, st: dict) -> str:
    return f"# step {sid} ({st.get('status')}): UNCOMMITTED changes in the worktree (git diff HEAD plus new files)\n"


@app.command()
def diff(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    step: Optional[str] = typer.Option(None, "--step", help="Only this step, e.g. s1."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path."),
) -> None:
    """Show the diff of a run (base..branch) or of one step."""
    target = _repo_or_exit(load_config(), repo)
    meta = load_meta(_find_or_exit(target, run_id))
    ex = meta.get("exec")
    if not ex:
        typer.echo("Error: this run was never executed.", err=True)
        raise typer.Exit(1)
    try:
        if step:
            s = (meta.get("steps") or {}).get(step)
            if s is None:
                typer.echo(f"Error: no step {step!r}", err=True)
                raise typer.Exit(1)
            if s.get("commit"):
                typer.echo(workspace.diff(target.path, f"{s['commit']}^", s["commit"]))
            elif Path(ex["worktree"]).is_dir():
                typer.echo(_uncommitted_header(step, s) + workspace.diff_uncommitted(Path(ex["worktree"])))
            else:
                typer.echo(f"(step {step} has no commit)")
        else:
            typer.echo(workspace.diff(target.path, ex["base_sha"], ex["branch"]))
            wt = Path(ex["worktree"])
            for sid, s in (meta.get("steps") or {}).items():  # stopped steps have changes that are not committed yet
                if s.get("status") in STOPPED_STATUSES and not s.get("commit") and wt.is_dir():
                    typer.echo(_uncommitted_header(sid, s) + workspace.diff_uncommitted(wt))
    except workspace.WorkspaceError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def clean(
    run_id: str = typer.Argument(..., help="Run id or a unique prefix."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Configured repo name or a path."),
) -> None:
    """Remove the run's worktree. The branch is kept."""
    target = _repo_or_exit(load_config(), repo)
    d = _find_or_exit(target, run_id)
    ex = load_meta(d).get("exec")
    if not ex:
        typer.echo("Nothing to clean: the run was never executed.")
        return
    try:
        removed = workspace.remove(target.path, ex["repo_name"], d.name, Path(ex["worktree"]))
    except workspace.WorkspaceError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Removed worktree {ex['worktree']}; branch {ex['branch']} kept." if removed else "No worktree to remove.")


if __name__ == "__main__":
    app()
