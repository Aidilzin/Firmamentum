"""Usage log. One global SQLite file, because plan quota is shared across every project."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adapters.base import AgentAdapter, AgentRequest, AgentResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT,
    ok INTEGER NOT NULL,
    error_kind TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    cost_usd REAL,
    cwd TEXT
)
"""

COOLDOWN_SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldowns (
    agent TEXT NOT NULL,
    model TEXT NOT NULL,          -- '*' = every model of the agent
    until_utc TEXT NOT NULL,
    reason TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (agent, model)
)
"""


def default_db_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "orch" / "usage.db"
    return Path.home() / ".local" / "share" / "orch" / "usage.db"


def _connect(db_path: Path | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    conn.execute(COOLDOWN_SCHEMA)
    return conn


def log_call(result: AgentResult, request: AgentRequest, db_path: Path | None = None) -> None:
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO calls (ts, agent, model, ok, error_kind, duration_ms, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, cwd)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    result.agent,
                    result.model,
                    int(result.ok),
                    result.error_kind.value,
                    result.duration_ms,
                    result.input_tokens,
                    result.output_tokens,
                    result.cache_read_tokens,
                    result.cache_write_tokens,
                    result.total_tokens,
                    result.cost_usd,
                    str(request.cwd),
                ),
            )
    finally:
        conn.close()


def run_agent(adapter: AgentAdapter, request: AgentRequest, db_path: Path | None = None) -> AgentResult:
    """The single path every adapter call goes through; logs one row per call."""
    result = adapter.run(request)
    log_call(result, request, db_path)
    return result


@dataclass
class AgentSummary:
    agent: str
    calls: int
    tokens: int
    failures: int


def summary(since: datetime, db_path: Path | None = None) -> list[AgentSummary]:
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT agent, COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(1-ok),0)"
            " FROM calls WHERE ts >= ? GROUP BY agent ORDER BY agent",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchall()
    finally:
        conn.close()
    return [AgentSummary(agent=a, calls=c, tokens=t, failures=f) for a, c, t, f in rows]


@dataclass
class ModelSummary:
    agent: str
    model: str
    calls: int
    tokens: int
    failures: int


def summary_by_model(since: datetime, db_path: Path | None = None) -> list[ModelSummary]:
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT agent, COALESCE(model,'default'), COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(1-ok),0)"
            " FROM calls WHERE ts >= ? GROUP BY agent, COALESCE(model,'default') ORDER BY agent, 2",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchall()
    finally:
        conn.close()
    return [ModelSummary(a, m, c, t, f) for a, m, c, t, f in rows]


# ---------- cooldowns ----------


@dataclass
class Cooldown:
    agent: str
    model: str  # '*' = all models of the agent
    until: datetime  # UTC
    reason: str
    created: datetime


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def model_matches(cooldown_model: str, entry_model: str | None) -> bool:
    """'*' matches everything; otherwise equal, or the cooldown name is a substring (opus ~ claude-opus-4)."""
    if cooldown_model == "*":
        return True
    em = (entry_model or "default").lower()
    cm = cooldown_model.lower()
    return em == cm or (cm != "default" and cm in em)


def record_cooldown(agent: str, model: str, until: datetime, reason: str, now: datetime | None = None,
                    db_path: Path | None = None) -> None:  # fmt: skip
    """Insert or replace; the later of an old and a new until_utc wins."""
    now = now or datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        with conn:
            old = conn.execute("SELECT until_utc FROM cooldowns WHERE agent=? AND model=?", (agent, model)).fetchone()
            if old and datetime.fromisoformat(old[0]) > until:
                until = datetime.fromisoformat(old[0])
            conn.execute(
                "INSERT OR REPLACE INTO cooldowns (agent, model, until_utc, reason, created) VALUES (?,?,?,?,?)",
                (agent, model, _iso(until), reason[:500], _iso(now)),
            )
    finally:
        conn.close()


def active_cooldowns(now: datetime | None = None, db_path: Path | None = None) -> list[Cooldown]:
    now = now or datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT agent, model, until_utc, reason, created FROM cooldowns ORDER BY until_utc").fetchall()
    finally:
        conn.close()
    out = [Cooldown(a, m, datetime.fromisoformat(u), r, datetime.fromisoformat(c)) for a, m, u, r, c in rows]
    return [c for c in out if c.until > now]


def clear_cooldowns(agent: str | None = None, db_path: Path | None = None) -> int:
    conn = _connect(db_path)
    try:
        with conn:
            cur = (conn.execute("DELETE FROM cooldowns WHERE agent=?", (agent,)) if agent
                   else conn.execute("DELETE FROM cooldowns"))  # fmt: skip
            return cur.rowcount
    finally:
        conn.close()
