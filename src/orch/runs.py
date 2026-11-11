"""Run storage: .orch/runs/<run_id>/ inside the target repo."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import runlock
from .plan import Plan
from .planner import PlannerOutcome

STATUSES = ("planned", "needs_input", "failed", "executing", "paused", "cancelled", "awaiting_approval", "done")
STEP_STATES = ("pending", "running", "done", "failed", "needs_input", "awaiting_approval")


class RunNotFound(LookupError):
    pass


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].strip("-") or "run"


def runs_dir(repo: Path) -> Path:
    return repo / ".orch" / "runs"


def create_run(repo: Path, request: str, now: datetime | None = None) -> Path:
    """Make the run folder and write request.md. run_id = UTC timestamp + slug of the request."""
    now = now or datetime.now(timezone.utc)
    base = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slugify(request)}"
    root = runs_dir(repo)
    root.mkdir(parents=True, exist_ok=True)
    run_id, n = base, 1
    while (root / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    d = root / run_id
    d.mkdir()
    (d / "request.md").write_text(request.rstrip() + "\n", encoding="utf-8")
    return d


def _dump(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_outcome(
    run_dir: Path, outcome: PlannerOutcome, models: dict[str, str | None], created: datetime | None = None
) -> dict:
    """Write plan.json, planner_raw.json, scout.md (if used) and meta.json. Returns the meta dict."""
    if outcome.plan is not None:
        status = "needs_input" if outcome.plan.needs_input else "planned"
        _dump(run_dir / "plan.json", outcome.plan.model_dump(mode="json"))
    else:
        status = "failed"
    _dump(
        run_dir / "planner_raw.json",
        {
            "attempts": [
                {"validation_errors": a.validation_errors, "result": a.result.model_dump(mode="json")}
                for a in outcome.attempts
            ]
        },
    )
    if outcome.scout is not None:
        (run_dir / "scout.md").write_text(outcome.scout.text.rstrip() + "\n", encoding="utf-8")
    meta = {
        "run_id": run_dir.name,
        "status": status,
        "created": (created or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "models": models,
        "total_tokens": outcome.total_tokens,  # planning only (scout + planner); see run_tokens() for the whole run
        "plan_role_tokens": plan_role_tokens(outcome),
        "error": outcome.error,
        "routing": outcome.routes,
    }
    if outcome.resume_after is not None:
        meta["resume_after"] = outcome.resume_after.astimezone(timezone.utc).isoformat(timespec="seconds")
    _dump(run_dir / "meta.json", meta)
    return meta


def plan_role_tokens(outcome) -> dict[str, int]:
    """Planning tokens by role: scout (if used) and planner (every attempt)."""
    out: dict[str, int] = {}
    if outcome.scout is not None:
        out["scout"] = outcome.scout.total_tokens
    out["planner"] = sum(a.result.total_tokens for a in outcome.attempts)
    return out


def run_tokens(meta: dict) -> tuple[dict[str, int], int]:
    """(tokens by role, total) for the WHOLE run: planning (scout, planner) plus execution (builder, reviewer, repair).

    total == sum(by_role) == meta['total_tokens'] (planning) + meta['exec_tokens'] (execution)."""
    plan = dict(meta.get("plan_role_tokens") or {"planner": int(meta.get("total_tokens", 0))})  # older runs: all planner
    by_role = {r: n for r, n in plan.items() if n}
    exec_roles = meta.get("role_tokens") or {}
    for r, n in exec_roles.items():
        by_role[r] = by_role.get(r, 0) + n
    missing = int(meta.get("exec_tokens", 0)) - sum(exec_roles.values())
    if missing > 0:  # runs executed before per-role totals were kept
        by_role["unattributed"] = missing
    return by_role, sum(by_role.values())


@dataclass
class RunInfo:
    run_id: str
    status: str
    created: str
    total_tokens: int
    path: Path
    stale: bool = False  # status is `executing` but nobody holds the run lock: the process died

    @property
    def shown_status(self) -> str:
        return f"{self.status} (stale)" if self.stale else self.status


FILE_RETRIES = 10  # Windows: os.replace / open fail with PermissionError while another handle is open
FILE_RETRY_S = 0.1


def _read_meta(d: Path) -> dict | None:
    for attempt in range(FILE_RETRIES):
        try:
            return json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == FILE_RETRIES - 1:
                return None
            time.sleep(FILE_RETRY_S)
        except (OSError, json.JSONDecodeError):
            return None
    return None


def is_stale(run_dir: Path, meta: dict) -> bool:
    """`executing` on disk, but no process holds the run lock: the executor died mid-run."""
    return meta.get("status") == "executing" and runlock.holder(run_dir) is None


def load_meta(run_dir: Path) -> dict:
    m = _read_meta(run_dir)
    if m is None:
        raise RunNotFound(f"no readable meta.json in {run_dir}")
    return m


def write_meta(run_dir: Path, meta: dict) -> None:
    """Atomic write (unique tmp + replace) so a crash never leaves a half-written meta.json.
    On Windows os.replace raises PermissionError while a reader has meta.json open, so it is retried briefly."""
    tmp = run_dir / f"meta.json.{os.getpid()}.{threading.get_ident()}.tmp"
    _dump(tmp, meta)
    try:
        for attempt in range(FILE_RETRIES):
            try:
                os.replace(tmp, run_dir / "meta.json")
                return
            except PermissionError:
                if attempt == FILE_RETRIES - 1:
                    raise
                time.sleep(FILE_RETRY_S)
    finally:
        tmp.unlink(missing_ok=True)


def list_runs(repo: Path) -> list[RunInfo]:
    """Newest first. A folder without a readable meta.json is shown as status 'incomplete'."""
    root = runs_dir(repo)
    if not root.is_dir():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        m = _read_meta(d) or {}
        out.append(
            RunInfo(d.name, m.get("status", "incomplete"), m.get("created", ""), run_tokens(m)[1], d,
                    stale=is_stale(d, m))  # fmt: skip
        )
    return out


def find_run(repo: Path, run_id: str) -> Path:
    """Exact id, or a unique prefix."""
    root = runs_dir(repo)
    if (root / run_id).is_dir():
        return root / run_id
    matches = [r.path for r in list_runs(repo) if r.run_id.startswith(run_id)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RunNotFound(f"no run {run_id!r} in {root}")
    raise RunNotFound(f"run id {run_id!r} is ambiguous: {', '.join(m.name for m in matches)}")


def load_plan(run_dir: Path) -> Plan | None:
    p = run_dir / "plan.json"
    if not p.is_file():
        return None
    return Plan.model_validate_json(p.read_text(encoding="utf-8"))
