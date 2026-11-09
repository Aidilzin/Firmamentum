"""orch command line: `orch ask` and `orch usage`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from .adapters import AgentAdapter, AgentRequest, AntigravityAdapter, ClaudeCodeAdapter
from .budget import run_agent, summary

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
def usage(days: int = typer.Option(7, "--days", help="Look back this many days.")) -> None:
    """Print per-agent call, token and failure totals."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = summary(since)
    typer.echo(f"Usage, last {days} day(s)")
    typer.echo(f"{'agent':<8}{'calls':>8}{'tokens':>12}{'failures':>10}")
    if not rows:
        typer.echo("(no calls logged)")
    for r in rows:
        typer.echo(f"{r.agent:<8}{r.calls:>8}{r.tokens:>12}{r.failures:>10}")


if __name__ == "__main__":
    app()
