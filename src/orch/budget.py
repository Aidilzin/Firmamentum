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
