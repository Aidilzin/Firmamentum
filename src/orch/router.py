"""Role routing: ordered fallback lists, cooldowns after RATE_LIMITED, max one fallback hop per call.

Every planner/scout/builder/reviewer/repair call goes through `call_role`. The caller passes
`build_req(entry)` so the request (tool flags, inline role text, resume id) can differ per agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import budget
from .adapters.base import AgentAdapter, AgentRequest, AgentResult, ErrorKind
from .config import Config, Entry

MAX_COOLDOWN = timedelta(days=8)  # a parsed reset further away than this is not believed


# ---------- reset-time parsing ----------

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_REL_RE = re.compile(r"\b(?:in|after)\s+((?:\d+\s*[a-z]+[\s,]*(?:and\s+)?)+)", re.IGNORECASE)
_REL_PART_RE = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE)
_CLOCK_RE = re.compile(r"\bresets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
_UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _parse_iso(text: str) -> datetime | None:
    m = _ISO_RE.search(text)
    if not m:
        return None
    raw = m.group(0).replace(" ", "T")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # naive = UTC


def _parse_relative(text: str, now: datetime) -> datetime | None:
    m = _REL_RE.search(text)
    if not m:
        return None
    secs = sum(int(n) * _UNIT[u[0].lower()] for n, u in _REL_PART_RE.findall(m.group(1)))
    return now + timedelta(seconds=secs) if secs else None


def _parse_clock(text: str, now: datetime) -> datetime | None:
    m = _CLOCK_RE.search(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if not ampm and m.group(2) is None:
        return None  # "resets 5" is not a time
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    local_now = now.astimezone()  # the machine's timezone
    cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= local_now:
        cand += timedelta(days=1)
    return cand


def parse_reset_time(text: str, now: datetime, cooldown_minutes: int) -> tuple[datetime, bool]:
    """(until_utc, parsed). Falls back to now + cooldown_minutes when nothing believable is found."""
    for parse in (
        _parse_iso,
        lambda t: datetime.fromtimestamp(int(m.group(1)), timezone.utc) if (m := _EPOCH_RE.search(t)) else None,
        lambda t: _parse_relative(t, now),
        lambda t: _parse_clock(t, now),
    ):
        got = parse(text)
        if got is not None and now < got <= now + MAX_COOLDOWN:
            return got.astimezone(timezone.utc), True
    return now + timedelta(minutes=cooldown_minutes), False


# ---------- cooldown scope ----------

_CLAUDE_FAMILIES = ("opus", "sonnet", "haiku")


def cooldown_scope(agent: str, call_model: str | None, text: str) -> str:
    """'*' (every model of the agent) unless the message names exactly one model and it is the model that failed."""
    if not call_model:
        return "*"
    low = text.lower()
    if agent == "claude":
        named = {f for f in _CLAUDE_FAMILIES if re.search(rf"\b{f}\b", low)}
        return call_model if len(named) == 1 and next(iter(named)) in call_model.lower() else "*"
    return call_model if call_model.lower() in low else "*"


def record_rate_limit(
    entry: Entry, result: AgentResult, cfg: Config, now: datetime, db_path: Path | None = None
) -> datetime:
    text = "\n".join(t for t in (result.text, result.stderr_tail) if t)
    until, _ = parse_reset_time(text, now, cfg.cooldown_minutes)
    budget.record_cooldown(entry.agent, cooldown_scope(entry.agent, entry.model, text), until, text, now, db_path)
    return until


# ---------- picking ----------


def _free_at(entry: Entry, cooldowns: list[budget.Cooldown]) -> datetime | None:
    """None when the entry is usable now, else when its last covering cooldown ends."""
    ends = [c.until for c in cooldowns if c.agent == entry.agent and budget.model_matches(c.model, entry.model)]
    return max(ends) if ends else None


def pick(
    role: str, cfg: Config, now: datetime | None = None, db_path: Path | None = None
) -> tuple[Entry | None, datetime | None]:
    """(first entry not in cooldown, None) or (None, earliest until_utc over the role's entries)."""
    now = now or datetime.now(timezone.utc)
    cds = budget.active_cooldowns(now, db_path)
    waits = []
    for e in cfg.roles[role]:
        free = _free_at(e, cds)
        if free is None:
            return e, None
        waits.append(free)
    return None, min(waits)


def earliest_cooldown(role: str, cfg: Config, now: datetime, db_path: Path | None = None) -> datetime | None:
    """Earliest end among active cooldowns that cover any entry of the role (used when the hop limit stops a call)."""
    cds = budget.active_cooldowns(now, db_path)
    ends = [c.until for e in cfg.roles[role] for c in cds if c.agent == e.agent and budget.model_matches(c.model, e.model)]
    return min(ends) if ends else None


# ---------- the call ----------


@dataclass
class Routed:
    result: AgentResult | None  # None when nothing could be called (paused before any call)
    entry: Entry | None
    fallback: bool = False  # entry is not the role's first-choice entry
    paused: bool = False  # every usable entry is rate limited (or in cooldown)
    resume_after: datetime | None = None
    rate_limited: AgentResult | None = None  # the RATE_LIMITED result that caused a pause, if any


def call_role(
    role: str,
    build_req: Callable[[Entry], AgentRequest],
    get_adapter: Callable[[str], AgentAdapter],
    cfg: Config,
    *,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> Routed:
    def clock() -> datetime:
        return now or datetime.now(timezone.utc)

    entries = cfg.roles[role]
    entry, earliest = pick(role, cfg, clock(), db_path)
    if entry is None:
        return Routed(None, None, paused=True, resume_after=earliest)

    last_rl: AgentResult | None = None
    for hop in range(2):  # the first try, then at most ONE fallback hop
        req = build_req(entry).model_copy(update={"model": entry.model})
        result = budget.run_agent(get_adapter(entry.agent), req, db_path)
        if result.error_kind != ErrorKind.RATE_LIMITED:
            return Routed(result, entry, fallback=entry != entries[0])
        last_rl = result
        record_rate_limit(entry, result, cfg, clock(), db_path)
        if hop == 1:
            break
        entry, earliest = pick(role, cfg, clock(), db_path)
        if entry is None:
            break
    return Routed(None, None, paused=True, resume_after=earliest_cooldown(role, cfg, clock(), db_path), rate_limited=last_rl)
