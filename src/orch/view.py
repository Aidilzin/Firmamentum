"""The view model: a pure data structure built from meta.json (a plain dict, or a frozen event snapshot). No rich, no printing, no executor imports.

Renderers (ui.py: rich inline and plain) consume a RunView only. A future full-screen front end would be a third
renderer reading the same structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .events import Meta
from .runs import run_tokens
from .theme import demon_label

PHASE_ROLE = {"build": "builder", "review": "reviewer", "repair": "repair", "need_repair": "repair"}
PHASE_LABEL = {"need_repair": "repair"}
VERBS = {  # role -> (while running, when done)
    "scout": ("scouting", "scouted the repo"),
    "planner": ("planning", "planned"),
    "builder": ("building", "built"),
    "reviewer": ("reviewing", "reviewed"),
    "repair": ("repairing", "repaired"),
}
ROLE_ORDER = ("scout", "planner", "builder", "reviewer", "repair")
# constellation cell kinds
DONE, RUNNING, PENDING, FAILED, NEEDS = "done", "running", "pending", "failed", "needs"
STOPPED_STEP_STATES = ("awaiting_approval", "needs_input", "failed", "rejected")


@dataclass
class StepView:
    id: str
    title: str
    status: str
    kind: str  # constellation cell kind
    role: str | None  # demon working on it right now (running steps only)
    phase: str | None
    round: int
    tokens: int
    elapsed: float | None  # seconds; None when unknown
    entry: str | None = None
    commit: str | None = None
    tests: str | None = None  # "pass" | "FAIL(1)" | None
    verdict: str | None = None
    error: str | None = None


@dataclass
class Activity:
    role: str
    step_id: str
    text: str  # "building s3: Slugify"


@dataclass
class DemonLine:
    role: str
    text: str  # "planned 4 steps", "building s3"
    model: str | None
    tokens: int
    elapsed: float | None
    running: bool = False


@dataclass
class StopInfo:
    kind: str  # "needs_you" | "failed" | "paused"
    what: str
    why: str
    next_cmds: list[str] = field(default_factory=list)

    @property
    def border(self) -> str:
        """Palette attribute for the panel border."""
        return "error" if self.kind == "failed" else "warning"


@dataclass
class RunView:
    run_id: str
    status: str
    models: dict[str, str]
    steps: list[StepView]
    current: Activity | None
    tokens_total: int
    tokens_by_role: dict[str, int]
    elapsed_total: float | None
    step_index: int  # 1-based position of the step in focus (first unfinished, else last); 0 when no steps
    step_count: int
    demons: list[DemonLine]
    stop: StopInfo | None = None


def parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def duration(started: str | None, ended: str | None) -> float | None:
    a, b = parse_utc(started), parse_utc(ended)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def fmt_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def short_model(entry: str | None) -> str | None:
    """'claude:opus' -> 'opus'; 'agy:default' -> 'agy'."""
    if not entry:
        return None
    agent, _, model = entry.partition(":")
    return model if model and model != "default" else agent


def step_kind(status: str) -> str:
    return {
        "done": DONE, "running": RUNNING, "failed": FAILED, "rejected": FAILED,
        "needs_input": NEEDS, "awaiting_approval": NEEDS,
    }.get(status, PENDING)  # fmt: skip


def _step_view(sid: str, st: dict, first_entries: dict[str, str], live_elapsed: dict[str, float | None]) -> StepView:
    status = st.get("status", "pending")
    phase = st.get("phase") if status == "running" else None
    role = PHASE_ROLE.get(phase or "") if status == "running" else None
    used = (st.get("roles_used") or {}).get(role or "") if role else None
    if used:
        entry = used["entry"] + (" (fallback)" if used.get("fallback") else "")
    else:
        entry = first_entries.get(role) if role else None
    elapsed = duration(st.get("started_at"), st.get("ended_at"))
    if elapsed is None:
        elapsed = live_elapsed.get(sid)
    t = st.get("tests")
    reviews = st.get("reviews") or []
    return StepView(
        id=sid, title=st.get("title", ""), status=status, kind=step_kind(status), role=role,
        phase=PHASE_LABEL.get(phase, phase) if phase else None, round=st.get("rounds_total", 0),
        tokens=st.get("tokens", 0), elapsed=elapsed, entry=entry, commit=st.get("commit"),
        tests=None if not t else ("pass" if t.get("passed") else f"FAIL({t.get('exit_code')})"),
        verdict=reviews[-1]["verdict"] if reviews else None, error=st.get("error"),
    )  # fmt: skip


def stop_info(status: str, step_id: str | None, message: str, meta: Meta, next_cmds: list[str]) -> StopInfo:
    """What happened, why, and the exact next command."""
    st = (meta.get("steps") or {}).get(step_id or "", {}) if step_id else {}
    what = f"run {status}" + (f" at step {step_id}" if step_id else "") + (f": {message}" if message else "")
    why = []
    if st.get("rule"):
        why.append(f"rule {st['rule']}")
    if st.get("stage"):
        stage = st["stage"]
        why.append(f"stage {demon_label(stage, first=True)}" if stage in VERBS else f"stage {stage}")
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
    kind = "failed" if status in ("failed", "rejected") else ("paused" if status in ("paused", "cancelled") else "needs_you")
    return StopInfo(kind, what, "; ".join(why) if why else "-", list(next_cmds))


def _demon_lines(meta: Meta, steps: list[StepView], models: dict[str, str], by_role: dict[str, int],
                 raw_steps: dict) -> list[DemonLine]:  # fmt: skip
    lines: list[DemonLine] = []
    n_steps = len(raw_steps)
    running = next((s for s in steps if s.status == "running"), None)
    for role in ROLE_ORDER:
        tokens = by_role.get(role, 0)
        used_in = [s.id for s in steps if role in (raw_steps[s.id].get("roles_tokens") or {})]
        is_running = running is not None and running.role == role
        if role in ("scout", "planner"):
            if role not in models and not tokens:
                continue
            if role == "planner":
                text = f"planned {n_steps} step{'' if n_steps == 1 else 's'}" if n_steps else ("needs your answer" if meta.get("status") == "needs_input" else "planned")
            else:
                text = VERBS[role][1]
            model, elapsed = models.get(role), None
        else:
            if not used_in and not is_running:
                continue
            if is_running:
                text = f"{VERBS[role][0]} {running.id}"  # type: ignore[union-attr]
                elapsed = running.elapsed  # type: ignore[union-attr]
            else:
                text = f"{VERBS[role][1]} {len(used_in)} step" + ("" if len(used_in) == 1 else "s")
                elapsed = None
            used = (raw_steps[used_in[-1]].get("roles_used") or {}).get(role) if used_in else None
            model = used["entry"] + (" (fallback)" if used.get("fallback") else "") if used else (
                running.entry if is_running and running else None)
        lines.append(DemonLine(role, text, short_model(model), tokens, elapsed, is_running))
    return lines


def build_run_view(
    meta: Meta,
    first_entries: dict[str, str] | None = None,
    elapsed: dict[str, float | None] | None = None,
    elapsed_total: float | None = None,
    next_cmds: list[str] | None = None,
    stop: tuple[str, str | None, str] | None = None,
) -> RunView:
    """Pure. `elapsed` maps step id -> live seconds (used only when meta has no started_at/ended_at for the step).

    `stop` is (status, step_id, message) from the executor; without it a stop is derived from meta['status']."""
    first_entries = first_entries or {}
    raw_steps = meta.get("steps") or {}
    steps = [_step_view(sid, st, first_entries, elapsed or {}) for sid, st in raw_steps.items()]
    by_role, total = run_tokens(meta)
    models = {r: m for r, m in (meta.get("models") or {}).items() if m}
    current = None
    running = next((s for s in steps if s.status == "running"), None)
    if running is not None and running.role:
        current = Activity(running.role, running.id, f"{VERBS[running.role][0]} {running.id}: {running.title}")
    focus = next((i for i, s in enumerate(steps) if s.kind != DONE), len(steps) - 1)
    status = meta.get("status", "?")
    if elapsed_total is None:
        elapsed_total = duration(meta.get("started_at"), meta.get("ended_at"))
    view = RunView(
        run_id=meta.get("run_id", ""), status=status, models=models, steps=steps, current=current,
        tokens_total=total, tokens_by_role=by_role, elapsed_total=elapsed_total,
        step_index=focus + 1 if steps else 0, step_count=len(steps),
        demons=_demon_lines(meta, steps, models, by_role, raw_steps),
    )  # fmt: skip
    if stop is None and status in ("failed", "needs_input", "awaiting_approval", "paused", "cancelled", "rejected"):
        bad = next((s.id for s in steps if s.status in STOPPED_STEP_STATES or s.error in ("rate limited", "cancelled")), None)
        stop = (status, bad, "")
    if stop is not None and stop[0] != "done":
        view.stop = stop_info(stop[0], stop[1], stop[2], meta, next_cmds or [])
    return view
