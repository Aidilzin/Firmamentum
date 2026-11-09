"""Execute a stored plan: one Builder call per step in one worktree, the orchestrator runs the tests.

Step states: pending | running | done | failed | needs_input | awaiting_approval.
meta.json is rewritten after every transition so a crash or a usage limit can be resumed.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from . import workspace
from .adapters.base import AgentAdapter, AgentRequest, AgentResult, ErrorKind
from .adapters.process import run_process
from .build import BuildReport, load_schema_file
from .budget import run_agent
from .config import Config, ResolvedRepo
from .plan import Plan, Step
from .planner import extract_json
from .runs import load_meta, load_plan, write_meta

ROLES_DIR = Path(__file__).parent / "roles"
BUILDER_TIMEOUT_S = 20 * 60
TEST_TIMEOUT_S = 10 * 60
BUILDER_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
DENIED_BASH = ("Bash(git)", "Bash(git *)", "Bash(uv add*)", "Bash(uv pip*)", "Bash(pip *)", "Bash(rm *)", "Bash(del *)")
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
_SENSITIVE_RE = re.compile(r"auth|payment|secret", re.IGNORECASE)

AdapterFactory = Callable[[str], AgentAdapter]


class ExecError(RuntimeError):
    """A precondition failed; nothing was run."""


@dataclass
class ExecOutcome:
    status: str  # done | paused | failed | needs_input | awaiting_approval
    step_id: str | None = None
    message: str = ""


# ---------- pure helpers ----------


def builder_instructions() -> str:
    return (ROLES_DIR / "builder.md").read_text(encoding="utf-8")


def builder_args(test_command: str, role_text: str) -> list[str]:
    """Exact tool permissions for the Builder. `test_command` MUST come from config.yaml, never from the plan."""
    allowed = ["Read", "Grep", "Glob", "Edit", "Write", f"Bash({test_command})", f"Bash({test_command} *)"]
    return [
        "--permission-prompts", "none",
        "--tools", BUILDER_TOOLS,
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", ",".join(DENIED_BASH),
        "--append-system-prompt", role_text,
    ]  # fmt: skip


def build_prompt(plan: Plan, step: Step, done_titles: list[str], test_command: str) -> str:
    lines = ["## Goal", plan.goal, ""]
    if plan.assumptions:
        lines += ["## Assumptions", *[f"- {a}" for a in plan.assumptions], ""]
    lines += [
        f"## Your step: [{step.id}] {step.title}",
        step.description,
        "",
        "Files expected: " + (", ".join(step.files) or "none listed"),
        f"Acceptance: {step.acceptance}",
        "",
        "## Already done (earlier steps, committed)",
        *([f"- {t}" for t in done_titles] or ["- (none)"]),
        "",
        "## Test command",
        f"Run this before you finish: {test_command}",
        "",
        "Do only this step. Reply with the JSON report only.",
    ]
    return "\n".join(lines)


def is_protected(path: str) -> bool:
    p = path.replace("\\", "/").strip("/")
    parts = p.split("/")
    name = parts[-1].lower()
    if ".git" in [x.lower() for x in parts]:
        return True
    if name.startswith(".env") or name.endswith((".pem", ".key")):
        return True
    return bool(_SENSITIVE_RE.search(p))


def is_dependency_file(path: str) -> bool:
    return path.replace("\\", "/").split("/")[-1] in DEPENDENCY_FILES


def outside_plan(changed: list[str], planned: list[str]) -> list[str]:
    norm = [p.replace("\\", "/").lstrip("./") for p in planned]

    def planned_ok(f: str) -> bool:
        return any(f == p or (p.endswith("/") and f.startswith(p)) for p in norm)

    return [f for f in changed if not planned_ok(f)]


def _parse_report(result: AgentResult) -> BuildReport | None:
    data: object = result.structured
    if data is None:
        try:
            data = extract_json(result.text)
        except (json.JSONDecodeError, ValueError):
            return None
    try:
        return BuildReport.model_validate(data)
    except ValidationError:
        return None


# ---------- executor ----------


class Executor:
    def __init__(
        self,
        repo: ResolvedRepo,
        run_dir: Path,
        cfg: Config,
        get_adapter: AdapterFactory,
        log: Callable[[str], None] = lambda m: None,
    ):
        self.repo, self.run_dir, self.cfg, self.get_adapter, self.log = repo, run_dir, cfg, get_adapter, log
        self.meta: dict = {}
        self.plan: Plan | None = None
        self.test_command = ""
        self.wt: Path = Path()

    # -- meta --
    def _save(self) -> None:
        write_meta(self.run_dir, self.meta)

    def _set_step(self, sid: str, **kw) -> dict:
        s = self.meta["steps"][sid]
        s.update(kw)
        self._save()
        return s

    def _stop(self, run_status: str, sid: str, message: str) -> ExecOutcome:
        self.meta["status"] = run_status
        self._save()
        return ExecOutcome(run_status, sid, message)

    # -- setup --
    def _load(self) -> None:
        if not self.repo.test_command:
            raise ExecError(
                "this repo has no test_command in config.yaml; add it under repos: (the plan's test_command is never used)"
            )
        self.test_command = self.repo.test_command
        self.plan = load_plan(self.run_dir)
        if self.plan is None:
            raise ExecError("this run has no stored plan (planning failed)")
        if self.plan.needs_input:
            raise ExecError(f"the plan needs input first: {self.plan.question}")
        self.meta = load_meta(self.run_dir)
        if self.cfg.roles["builder"].agent != "claude":
            raise ExecError("the builder role must use agent 'claude' (agy has no tool allow-list)")

    def start(self) -> ExecOutcome:
        self._load()
        assert self.plan is not None
        if "exec" in self.meta:
            raise ExecError("this run was already started; use `orch resume`")
        run_id = self.run_dir.name
        warnings: list[str] = []
        if self.plan.test_command.strip() != self.test_command.strip():
            w = (
                f"plan test_command {self.plan.test_command!r} differs from config {self.test_command!r}; "
                "using config"
            )
            warnings.append(w)
            self.log("warning: " + w)
        wt = workspace.create(self.repo.path, self.repo.name or self.repo.path.name, run_id)
        self.wt = wt.path
        self.meta["exec"] = {
            "worktree": str(wt.path),
            "branch": wt.branch,
            "base_sha": wt.base_sha,
            "repo_name": self.repo.name or self.repo.path.name,
            "test_command": self.test_command,
            "warnings": warnings,
        }
        self.meta["steps"] = {
            s.id: {"title": s.title, "status": "pending", "tokens": 0, "calls": 0, "tests": None, "commit": None,
                   "warnings": [], "question": None, "error": None}
            for s in self.plan.topo_order()
        }  # fmt: skip
        self.meta["status"] = "executing"
        self._save()
        return self._loop()

    def resume(self) -> ExecOutcome:
        self._load()
        ex = self.meta.get("exec")
        if not ex or "steps" not in self.meta:
            raise ExecError("this run was never started; use `orch exec`")
        self.wt = Path(ex["worktree"])
        if not self.wt.is_dir():
            raise ExecError(f"the worktree {self.wt} is gone (orch clean?); it cannot be resumed")
        if self.meta.get("status") == "done":
            return ExecOutcome("done", None, "already done")
        self.meta["status"] = "executing"
        self._save()
        return self._loop()

    # -- main loop --
    def _loop(self) -> ExecOutcome:
        assert self.plan is not None
        steps = self.meta["steps"]
        for step in self.plan.topo_order():
            st = steps[step.id]
            if st["status"] == "done":
                continue
            if st["status"] in ("failed", "needs_input", "awaiting_approval"):
                return self._stop(st["status"], step.id, f"step {step.id} is {st['status']}; fix it, then resume (Phase 4: approve)")
            if any(steps[d]["status"] != "done" for d in step.depends_on):
                return self._stop("failed", step.id, f"step {step.id} depends on an unfinished step")
            if step.risk_flags:
                self._set_step(step.id, status="awaiting_approval", rule=", ".join(step.risk_flags))
                return self._stop("awaiting_approval", step.id, f"step {step.id} has risk flags: {', '.join(step.risk_flags)}")
            outcome = self._run_step(step)
            if outcome is not None:
                return outcome
        self.meta["status"] = "done"
        self._save()
        return ExecOutcome("done", None, "all steps done")

    def _run_step(self, step: Step) -> ExecOutcome | None:
        """Returns an ExecOutcome to stop the run, or None to continue."""
        assert self.plan is not None
        sid = step.id
        sdir = self.run_dir / "steps" / sid
        sdir.mkdir(parents=True, exist_ok=True)
        steps = self.meta["steps"]
        workspace.reset_to_head(self.wt)  # a crashed or paused step may have left partial edits
        self._set_step(sid, status="running", error=None, question=None, warnings=[])
        self.log(f"[{sid}] {step.title}: running builder")

        role = self.cfg.roles["builder"]
        done_titles = [steps[s.id]["title"] for s in self.plan.topo_order() if steps[s.id]["status"] == "done"]
        req = AgentRequest(
            prompt=build_prompt(self.plan, step, done_titles, self.test_command),
            cwd=self.wt,
            model=role.model,
            timeout_s=BUILDER_TIMEOUT_S,
            json_schema=load_schema_file(),
            extra_args=builder_args(self.test_command, builder_instructions()),
        )
        result = run_agent(self.get_adapter(role.agent), req)
        (sdir / "builder_raw.json").write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self._set_step(sid, tokens=steps[sid]["tokens"] + result.total_tokens, calls=steps[sid]["calls"] + 1,
                       session_id=result.session_id)  # fmt: skip
        self.meta["exec_tokens"] = self.meta.get("exec_tokens", 0) + result.total_tokens

        if not result.ok:
            if result.error_kind == ErrorKind.RATE_LIMITED:
                self._set_step(sid, status="pending", error="rate limited")
                return self._stop("paused", sid, "rate limited; run paused, `orch resume` later")
            self._set_step(sid, status="failed", error=f"{result.error_kind.value}: {result.text}"[:500])
            return self._stop("failed", sid, f"builder failed: {result.error_kind.value}")

        report = _parse_report(result)
        if report is None:
            self._warn(sid, "builder reply was not a valid report; relying on git and the tests")
        elif report.blocked:
            self._set_step(sid, status="needs_input", question=report.question)
            return self._stop("needs_input", sid, f"builder is blocked: {report.question}")

        changed = workspace.changed_files(self.wt)  # the truth, not report.files_changed
        extra = outside_plan(changed, step.files)
        if extra:
            self._warn(sid, "files outside the step's plan: " + ", ".join(extra))
        self._set_step(sid, changed_files=changed)

        if (hit := [f for f in changed if is_protected(f)]):
            self._set_step(sid, status="awaiting_approval", rule="touches_auth_payments_secrets_env", flagged=hit)
            return self._stop("awaiting_approval", sid, "protected paths touched: " + ", ".join(hit))
        if (hit := [f for f in changed if is_dependency_file(f)]):
            self._set_step(sid, status="awaiting_approval", rule="new_dependency", flagged=hit)
            return self._stop("awaiting_approval", sid, "dependency files changed: " + ", ".join(hit))

        passed, tests = self._run_tests(sdir)
        self._set_step(sid, tests=tests)
        if not passed:
            self._set_step(sid, status="failed", error="tests failed" if tests["exit_code"] is not None else tests["error"])
            return self._stop("failed", sid, f"tests failed for {sid} (see {sdir / 'tests.txt'})")

        sha = workspace.commit_step(self.wt, sid, step.title, changed)
        (sdir / "diff.patch").write_text(workspace.diff(self.wt, f"{sha}^", sha), encoding="utf-8")
        self._set_step(sid, status="done", commit=sha)
        self.log(f"[{sid}] done {sha[:8]}")
        return None

    def _warn(self, sid: str, msg: str) -> None:
        self.log(f"warning [{sid}]: {msg}")
        s = self.meta["steps"][sid]
        s.setdefault("warnings", []).append(msg)
        self._save()

    def _run_tests(self, sdir: Path) -> tuple[bool, dict]:
        """Run config's test_command in the worktree. Pass/fail by exit code only."""
        t0 = time.perf_counter()
        try:
            argv = shlex.split(self.test_command)
        except ValueError as e:
            return False, {"exit_code": None, "passed": False, "error": f"bad test_command: {e}", "duration_s": 0}
        out = run_process(argv, cwd=self.wt, timeout_s=TEST_TIMEOUT_S)
        text = f"$ {self.test_command}\n[exit {out.returncode}]\n{out.stdout}\n--- stderr ---\n{out.stderr}\n"
        if out.timed_out:
            text += "\n[timed out]\n"
        (sdir / "tests.txt").write_text(text, encoding="utf-8")
        passed = out.returncode == 0 and not out.timed_out and not out.not_found
        err = "timed out" if out.timed_out else ("test command not found" if out.not_found else "")
        return passed, {
            "exit_code": out.returncode, "passed": passed, "error": err,
            "duration_s": round(time.perf_counter() - t0, 1),
        }  # fmt: skip
