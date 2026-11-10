"""Display only: a rich live table for exec/run/resume, a plain line-by-line fallback, and the stop panel.

Nothing here changes what the executor does. The executor calls `log(msg)` and `on_update(meta)`; the view
reads the meta dict it is handed.
"""

from __future__ import annotations

import sys
import time
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

PHASE_ROLE = {"build": "builder", "review": "reviewer", "repair": "repair", "need_repair": "repair"}
PHASE_LABEL = {"need_repair": "repair"}


def is_interactive() -> bool:
    return sys.stdout.isatty()


def fmt_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def step_rows(meta: dict, first_entries: dict[str, str], elapsed: dict[str, float | None]) -> list[dict]:
    """One dict per step with the live-table columns. Pure; `first_entries` maps role -> label of its first entry."""
    rows = []
    for sid, st in (meta.get("steps") or {}).items():
        phase = st.get("phase")
        role = PHASE_ROLE.get(phase or "") if st.get("status") == "running" else None
        used = (st.get("roles_used") or {}).get(role or "") if role else None
        if used:
            entry = used["entry"] + (" (fallback)" if used.get("fallback") else "")
        elif role:
            entry = first_entries.get(role, "-")
        else:
            entry = "-"
        rows.append({
            "id": sid,
            "title": st.get("title", ""),
            "status": st.get("status", ""),
            "phase": PHASE_LABEL.get(phase, phase) or "-" if st.get("status") == "running" else "-",
            "role": role or "-",
            "entry": entry,
            "round": st.get("rounds_total", 0),
            "tokens": st.get("tokens", 0),
            "elapsed": fmt_elapsed(elapsed.get(sid)),
        })  # fmt: skip
    return rows


def footer_text(meta: dict, elapsed_total: float) -> str:
    total = meta.get("exec_tokens", 0)
    return f"total {total:,} tokens | run: {meta.get('status', '?')} | {fmt_elapsed(elapsed_total)}"


def stop_lines(status: str, step_id: str | None, message: str, meta: dict, next_cmds: list[str]) -> list[str]:
    """What happened, why, and the exact next command. Used by both the panel and the plain block."""
    st = (meta.get("steps") or {}).get(step_id or "", {}) if step_id else {}
    lines = [f"What happened: run {status}" + (f" at step {step_id}" if step_id else "") + (f": {message}" if message else "")]
    why = []
    if st.get("rule"):
        why.append(f"rule {st['rule']}")
    if st.get("stage"):
        why.append(f"stage {st['stage']}")
    if st.get("flagged"):
        why.append("files " + ", ".join(st["flagged"]))
    if st.get("question"):
        why.append(f"question: {st['question']}")
    if st.get("error"):
        why.append(f"error: {st['error']}")
    if meta.get("resume_after") and status == "paused":
        why.append(f"usage limit, resume after {meta['resume_after']}")
    if st.get("summary"):
        why.append(st["summary"])
    lines.append("Why: " + ("; ".join(why) if why else "-"))
    lines += [f"Next: {n}" for n in next_cmds] or ["Next: (nothing to do)"]
    return lines


class PlainView:
    """Today's output: log lines as they happen, nothing redrawn."""

    live = False

    def __init__(self, echo: Callable[[str], None]):
        self.echo = echo

    def log(self, msg: str) -> None:
        self.echo(msg)

    def on_update(self, meta: dict) -> None:
        pass

    def __enter__(self) -> PlainView:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def show_stop(self, status: str, step_id: str | None, message: str, meta: dict, next_cmds: list[str]) -> None:
        if status == "done":
            return
        self.echo("")
        self.echo("--- STOPPED ---")
        for line in stop_lines(status, step_id, message, meta, next_cmds):
            self.echo(line)


class LiveView(PlainView):
    """rich live table; log lines are printed above it."""

    live = True

    def __init__(self, first_entries: dict[str, str], console: Console | None = None, clock: Callable[[], float] = time.monotonic):
        self.console = console or Console()
        self.first_entries = first_entries
        self.clock = clock
        self.t0 = clock()
        self.meta: dict = {}
        self._times: dict[str, list[float | None]] = {}
        self._live = Live(self._render(), console=self.console, refresh_per_second=4, transient=False)

    def echo(self, msg: str) -> None:  # type: ignore[override]
        self.console.print(Text(msg))

    def _elapsed(self) -> dict[str, float | None]:
        now = self.clock()
        return {sid: ((t[1] if t[1] is not None else now) - t[0]) for sid, t in self._times.items()}  # type: ignore[operator]

    def _render(self) -> Group:
        table = Table(expand=False)
        for col in ("id", "title", "status", "phase", "role", "entry", "round", "tokens", "elapsed"):
            table.add_column(col, justify="right" if col in ("round", "tokens", "elapsed") else "left")
        for r in step_rows(self.meta, self.first_entries, self._elapsed()):
            table.add_row(r["id"], r["title"], r["status"], r["phase"], r["role"], r["entry"], str(r["round"]),
                          f"{r['tokens']:,}", r["elapsed"])  # fmt: skip
        return Group(table, Text(footer_text(self.meta, self.clock() - self.t0)))

    def on_update(self, meta: dict) -> None:
        self.meta = meta
        now = self.clock()
        for sid, st in (meta.get("steps") or {}).items():
            t = self._times.get(sid)
            if st.get("status") == "running" and t is None:
                self._times[sid] = [now, None]
            elif st.get("status") != "running" and t is not None and t[1] is None:
                t[1] = now
        self._live.update(self._render())

    def __enter__(self) -> LiveView:
        self._live.start()
        return self

    def __exit__(self, *exc) -> None:
        self._live.update(self._render())
        self._live.stop()

    def show_stop(self, status: str, step_id: str | None, message: str, meta: dict, next_cmds: list[str]) -> None:
        if status == "done":
            return
        body = "\n".join(stop_lines(status, step_id, message, meta, next_cmds))
        self.console.print(Panel(body, title=f"Run {status}", border_style="red" if status == "failed" else "yellow"))


def make_view(plain: bool, echo: Callable[[str], None], first_entries: dict[str, str]) -> PlainView:
    """Live view only on a terminal and when --plain is not given."""
    if plain or not is_interactive():
        return PlainView(echo)
    return LiveView(first_entries)
