"""Run storage: .orch/runs/<run_id>/ inside the target repo."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .plan import Plan
from .planner import PlannerOutcome

STATUSES = ("planned", "needs_input", "failed", "executing", "paused", "awaiting_approval", "done")
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
        "total_tokens": outcome.total_tokens,
        "error": outcome.error,
        "routing": outcome.routes,
    }
    if outcome.resume_after is not None:
        meta["resume_after"] = outcome.resume_after.astimezone(timezone.utc).isoformat(timespec="seconds")
    _dump(run_dir / "meta.json", meta)
    return meta


@dataclass
class RunInfo:
    run_id: str
    status: str
    created: str
    total_tokens: int
    path: Path


def _read_meta(d: Path) -> dict | None:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_meta(run_dir: Path) -> dict:
    m = _read_meta(run_dir)
    if m is None:
        raise RunNotFound(f"no readable meta.json in {run_dir}")
    return m


def write_meta(run_dir: Path, meta: dict) -> None:
    """Atomic write (tmp + replace) so a crash never leaves a half-written meta.json."""
    tmp = run_dir / "meta.json.tmp"
    _dump(tmp, meta)
    os.replace(tmp, run_dir / "meta.json")


def list_runs(repo: Path) -> list[RunInfo]:
    """Newest first. A folder without a readable meta.json is shown as status 'incomplete'."""
    root = runs_dir(repo)
    if not root.is_dir():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        m = _read_meta(d) or {}
        out.append(
            RunInfo(d.name, m.get("status", "incomplete"), m.get("created", ""), int(m.get("total_tokens", 0)), d)
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
