"""Display only: renderers for RunView (rich inline and plain), the live view for exec/run/resume, the stop panel.

Nothing here changes what the executor does. The executor calls `log(msg)` and `on_update(meta)`; the view
builds a RunView (view.py) from the meta dict it is handed, and a renderer draws it.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .runs import run_tokens
from .theme import Theme, current_theme, demon_label
from .view import (  # noqa: F401  (PHASE_ROLE / fmt_elapsed re-exported for older imports)
    DONE, FAILED, NEEDS, PENDING, PHASE_LABEL, PHASE_ROLE, RUNNING, RunView, StopInfo, build_run_view, fmt_elapsed,
    fmt_tokens, stop_info,
)


def is_interactive() -> bool:
    return sys.stdout.isatty()


def step_rows(meta: dict, first_entries: dict[str, str], elapsed: dict[str, float | None]) -> list[dict]:
    """One dict per step with the old live-table columns. Pure; `first_entries` maps role -> label of its first entry."""
    rv = build_run_view(meta, first_entries, elapsed)
    return [{
        "id": s.id, "title": s.title, "status": s.status, "phase": s.phase or "-" if s.status == "running" else "-",
        "role": s.role or "-", "entry": s.entry or "-", "round": s.round, "tokens": s.tokens, "elapsed": fmt_elapsed(s.elapsed),
    } for s in rv.steps]  # fmt: skip


def footer_text(meta: dict, elapsed_total: float) -> str:
    total = run_tokens(meta)[1]
    return f"total {total:,} tokens | run: {meta.get('status', '?')} | {fmt_elapsed(elapsed_total)}"


def stop_lines(status: str, step_id: str | None, message: str, meta: dict, next_cmds: list[str]) -> list[str]:
    """What happened, why, and the exact next command. Used by both the panel and the plain block."""
    return stop_text(stop_info(status, step_id, message, meta, next_cmds))


def stop_text(info: StopInfo) -> list[str]:
    lines = [f"What happened: {info.what}", f"Why: {info.why}"]
    lines += [f"Next: {n}" for n in info.next_cmds] or ["Next: (nothing to do)"]
    return lines


# ---------- renderers ----------


def constellation(view: RunView, theme: Theme) -> Text:
    """`✦───✦───✧───·  step 3 of 4`: done, done, running, pending."""
    t = Text()
    sep = theme.sym("line") * 3
    for i, s in enumerate(view.steps):
        if i:
            t.append(sep, style=theme.sym_style("line"))
        if s.kind == DONE:
            t.append(theme.sym("star_done"), style=theme.sym_style("star_done"))
        elif s.kind == RUNNING:
            style = theme.demon_style(s.role, bold=True) if s.role else theme.style("text", bold=True)
            t.append(theme.sym("star_run"), style=style)
        elif s.kind == FAILED:
            t.append(theme.sym("err"), style=theme.sym_style("err"))
        elif s.kind == NEEDS:
            t.append(theme.sym("warn"), style=theme.sym_style("warn"))
        else:
            t.append(theme.sym("star_pending"), style=theme.sym_style("star_pending"))
    if view.steps:
        focus = view.steps[view.step_index - 1]
        t.append(f"  step {view.step_index} of {view.step_count}", style=theme.style("text"))
        t.append(f" · {focus.title}", style=theme.style("dim"))
    return t


def demon_lines(view: RunView, theme: Theme) -> list[Text]:
    """`glyph Name  what it did / is doing      model · tokens|time` per demon that has run or is running."""
    out = []
    for d in view.demons:
        right = " · ".join(x for x in (
            d.model, fmt_elapsed(d.elapsed) if d.running and d.elapsed is not None else fmt_tokens(d.tokens)) if x)  # fmt: skip
        t = Text()
        t.append(theme.demon_glyph(d.role) + " ", style=theme.demon_style(d.role, bold=True))
        t.append(f"{demon_label(d.role):<10}", style=theme.demon_style(d.role, bold=True))
        t.append(f"{d.text:<28}", style=theme.style("text"))
        t.append(right, style=theme.style("dim"))
        out.append(t)
    return out


def footer(view: RunView, theme: Theme) -> Text:
    return Text(
        f"total {view.tokens_total:,} tokens | run: {view.status} | {fmt_elapsed(view.elapsed_total)}",
        style=theme.style("dim"),
    )


def render_rich(view: RunView, theme: Theme, with_footer: bool = True) -> Group:
    parts: list = [constellation(view, theme)] if view.steps else []
    lines = demon_lines(view, theme)
    if lines:
        parts += ["", *lines] if parts else lines
    if with_footer:
        parts += ["", footer(view, theme)] if parts else [footer(view, theme)]
    return Group(*parts)


def render_plain(view: RunView, theme: Theme | None = None) -> list[str]:
    """The same words as the rich renderer, as plain lines (no colour, safe for logs)."""
    theme = theme or current_theme()
    lines = [constellation(view, theme).plain] if view.steps else []
    lines += [t.plain.rstrip() for t in demon_lines(view, theme)]
    return lines


def render_stop_panel(info: StopInfo, status: str, theme: Theme) -> Panel:
    return Panel("\n".join(stop_text(info)), title=f"Run {status}", border_style=theme.style(info.border))


def render_doctor(checks, theme: Theme) -> list[Text]:
    """One line per check: glyph, name, detail; a dim `fix:` line under every check that is not ok."""
    out = []
    width = max((len(c.name) for c in checks), default=0)
    for c in checks:
        key = {"ok": "ok", "warn": "warn", "err": "err"}[c.level]
        t = Text()
        t.append(theme.sym(key) + " ", style=theme.sym_style(key))
        t.append(f"{c.name:<{width}}  ", style=theme.style("text", bold=True))
        t.append(c.detail, style=theme.style("text"))
        out.append(t)
        if c.level != "ok" and c.fix:
            out.append(Text(f"    fix: {c.fix}", style=theme.style("dim")))
    return out


# ---------- log lines ----------

_LOG_RULES = [
    (re.compile(r": running builder$"), "builder", ": {} building"),
    (re.compile(r"\] reviewing$"), "reviewer", "] {} reviewing"),
    (re.compile(r"\] repair round "), "repair", "] {} round "),
]


def demonize(msg: str, seen: set[str]) -> str:
    """Executor log lines say 'running builder'; users read 'Maxwell (builder) building'. Role in brackets on first mention."""
    for rx, role, tpl in _LOG_RULES:
        if rx.search(msg):
            label = demon_label(role, first=role not in seen)
            seen.add(role)
            return rx.sub(tpl.format(label), msg)
    return msg


# ---------- views handed to the executor ----------


class PlainView:
    """Line-by-line output: log lines as they happen, nothing redrawn."""

    live = False

    def __init__(self, echo: Callable[[str], None], theme: Theme | None = None):
        self.echo = echo
        self.theme = theme or current_theme()
        self._seen: set[str] = set()

    def log(self, msg: str) -> None:
        self.echo(demonize(msg, self._seen))

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
    """rich live constellation; log lines are printed above it."""

    live = True

    def __init__(self, first_entries: dict[str, str], console: Console | None = None,
                 clock: Callable[[], float] = time.monotonic, theme: Theme | None = None):  # fmt: skip
        self.theme = theme or current_theme()  # not super().__init__: `echo` here is a method, not an attribute
        self._seen: set[str] = set()
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
        view = build_run_view(self.meta, self.first_entries, self._elapsed(), elapsed_total=self.clock() - self.t0)
        return render_rich(view, self.theme)

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
        info = stop_info(status, step_id, message, meta, next_cmds)
        self.console.print(render_stop_panel(info, status, self.theme))


def make_view(plain: bool, echo: Callable[[str], None], first_entries: dict[str, str], theme: Theme | None = None) -> PlainView:
    """Live view only on a terminal and when --plain is not given."""
    if plain or not is_interactive():
        return PlainView(echo, theme)
    return LiveView(first_entries, theme=theme)
