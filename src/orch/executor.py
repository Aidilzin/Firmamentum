"""Execute a stored plan: build -> post-checks -> tests -> review -> commit, with bounded repair rounds.

Step states: pending | running | done | failed | needs_input | awaiting_approval | rejected.
While a step runs, `phase` is one of build | checks | tests | review | need_repair | repair | commit.
A step that stops or pauses mid-way stores `stage` (why it stopped) and `resume_phase` (where to continue),
so `resume`, `approve` and `answer` can pick it up without redoing finished work.
meta.json is rewritten after every transition so a crash or a usage limit can be resumed.
"""

from __future__ import annotations

import json
import shlex
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from . import workspace
from .adapters.base import AgentAdapter, AgentRequest, AgentResult, ErrorKind, failure
from .adapters.claude_code import CONFINE_ARGS
from .adapters.process import child_env, run_process
from . import review as review_mod
from .build import BuildReport, load_schema_file
from .checks import classify, is_self_hosting_repo, outside_plan
from .config import Config, Entry, ResolvedRepo
from .plan import Plan, Step
from .planner import extract_json
from .router import call_role
from .runs import load_meta, load_plan, write_meta

ROLES_DIR = Path(__file__).parent / "roles"
BUILDER_TIMEOUT_S = 20 * 60
REPAIR_TIMEOUT_S = 20 * 60
REVIEW_TIMEOUT_S = 10 * 60
TAIL_LINES = 150
TEST_TIMEOUT_S = 10 * 60
BUILDER_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
DENIED_BASH = ("Bash(git)", "Bash(git *)", "Bash(uv add*)", "Bash(uv pip*)", "Bash(pip *)", "Bash(rm *)", "Bash(del *)")

AdapterFactory = Callable[[str], AgentAdapter]


class ExecError(RuntimeError):
    """A precondition failed; nothing was run."""


@dataclass
class ExecOutcome:
    status: str  # done | paused | failed | needs_input | awaiting_approval | rejected
    step_id: str | None = None
    message: str = ""


# ---------- pure helpers ----------


def builder_instructions() -> str:
    return (ROLES_DIR / "builder.md").read_text(encoding="utf-8")


def repair_instructions() -> str:
    return (ROLES_DIR / "repair.md").read_text(encoding="utf-8")


def reviewer_instructions() -> str:
    return (ROLES_DIR / "reviewer.md").read_text(encoding="utf-8")


def repair_args(test_command: str, retest_command: str | None, role_text: str, input_dir: Path) -> list[str]:
    """Same permissions as the Builder, plus read access to ONE folder of per-round inputs (never the step folder)."""
    extra = (retest_command,) if retest_command else ()
    return builder_args(test_command, role_text, extra) + ["--add-dir", str(input_dir)]


def reviewer_args(agent: str, role_text: str, step_dir: Path) -> tuple[list[str], str | None]:
    """(extra CLI args, role text to put inline in the prompt). Read-only either way."""
    if agent == "claude":
        return [
            *CONFINE_ARGS,
            "--permission-prompts", "none",
            "--tools", "Read,Grep,Glob",
            "--add-dir", str(step_dir),
            "--append-system-prompt", role_text,
        ], None  # fmt: skip
    return ["--mode", "plan", "--sandbox", "--add-dir", str(step_dir)], role_text


def builder_args(test_command: str, role_text: str, extra_commands: tuple[str, ...] = ()) -> list[str]:
    """Exact tool permissions for the Builder. Commands MUST come from config.yaml, never from the plan."""
    allowed = ["Read", "Grep", "Glob", "Edit", "Write"]
    for c in dict.fromkeys([test_command, *extra_commands]):
        allowed.append(f"Bash({c})")  # exact match only: a trailing `*` lets pytest flags (--basetemp, -p) through
    return [
        *CONFINE_ARGS,
        "--permission-prompts", "none",
        "--tools", BUILDER_TOOLS,
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", ",".join(DENIED_BASH),
        "--append-system-prompt", role_text,
    ]  # fmt: skip


def build_prompt(plan: Plan, step: Step, done_titles: list[str], test_command: str, answers: list[str] | None = None) -> str:
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
    ]
    if answers:
        lines += ["## Answers from the human to your earlier question", *[f"- {a}" for a in answers], ""]
    lines += [
        "## Test command",
        f"Run this before you finish: {test_command}",
        "",
        "Do only this step. Reply with the JSON report only.",
    ]
    return "\n".join(lines)


HIDDEN_NOTE_REVIEW = (
    "## Files added by the orchestrator on purpose\n"
    "{files} were added by the orchestrator on purpose (extra tests the builder did not see). Do not flag them as "
    "scope creep or an unrequested file. Do review whether the code makes them pass."
)
HIDDEN_NOTE_REPAIR = (
    "## Files added by the orchestrator on purpose\n"
    "{files} were added by the orchestrator on purpose. Never delete, rename or edit them; change the code so "
    "they pass."
)

RESUME_HEADER = (
    "You are now in REPAIR mode for the step you just worked on. Fix only what is described below, with the "
    "smallest change. Never weaken, skip or delete tests. No git, no dependency changes, stay in the working "
    "directory. Reply with the JSON report only.\n\n"
)


def repair_prompt(
    plan: Plan, step: Step, ctx: dict, test_tail: str, input_dir: Path, test_command: str,
    retest_command: str | None, answers: list[str] | None = None, hidden_files: list[str] | None = None,
) -> str:  # fmt: skip
    reasons = []
    if "tests" in ctx["reasons"]:
        reasons.append("the tests fail")
    if "review" in ctx["reasons"]:
        reasons.append("the reviewer rejected the change")
    lines = [
        "## Goal", plan.goal, "",
        f"## Step to repair: [{step.id}] {step.title}", step.description, "",
        f"Acceptance: {step.acceptance}", "",
        "## Why this repair round: " + " and ".join(reasons), "",
    ]  # fmt: skip
    if "tests" in ctx["reasons"]:
        lines += [f"## Test output (last {TAIL_LINES} lines)", "```", test_tail.rstrip(), "```", ""]
    if ctx["issues"]:
        lines += ["## Reviewer issues to fix (blocker/major)"]
        for i in ctx["issues"]:
            loc = f"{i['file']}:{i['line']}" if i.get("line") else i["file"]
            lines.append(f"- [{i['severity']}] {loc}: {i['problem']} -> fix: {i['fix']}")
        lines.append("")
    if answers:
        lines += ["## Answers from the human to your earlier question", *[f"- {a}" for a in answers], ""]
    if hidden_files:
        lines += [HIDDEN_NOTE_REPAIR.format(files=", ".join(hidden_files)), ""]
    lines += [
        f"## Files for this round (read-only folder): {input_dir}",
        "- diff.patch (the current uncommitted change of this step), test_tail.txt, review_issues.json",
        "",
        "## Test commands",
        *([f"Quick re-test first: {retest_command}"] if retest_command else []),
        f"Full test command (must pass): {test_command}",
        "",
        "Fix this step only. Reply with the JSON report only.",
    ]  # fmt: skip
    return "\n".join(lines)


def review_prompt(
    plan: Plan, step: Step, diff_path: Path, test_command: str, inline_role: str | None = None,
    hidden_files: list[str] | None = None,
) -> str:  # fmt: skip
    lines = [inline_role, ""] if inline_role else []
    lines += [
        "## Goal", plan.goal, "",
        f"## Step under review: [{step.id}] {step.title}", step.description, "",
        "Files expected: " + (", ".join(step.files) or "none listed"),
        f"Acceptance: {step.acceptance}", "",
        "## The change",
        f"Read this file first: {diff_path}",
        "It is the unified diff of this step's uncommitted change in the current working directory (new files included).",
        f"The orchestrator already ran `{test_command}` and it passed.", "",
    ]  # fmt: skip
    if hidden_files:
        lines += [HIDDEN_NOTE_REVIEW.format(files=", ".join(hidden_files)), ""]
    lines += ["Reply with the JSON review only."]
    return "\n".join(lines)


def tail_lines(path: Path, n: int) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]) + "\n"
    except OSError:
        return ""


def _parse_json(result: AgentResult, model):
    """Validate the reply against a pydantic model. Returns (obj | None, error strings)."""
    data: object = result.structured
    if data is None:
        try:
            data = extract_json(result.text)
        except (json.JSONDecodeError, ValueError):
            return None, ["the reply was not valid JSON"]
    try:
        return model.model_validate(data), []
    except ValidationError as e:
        return None, [x["msg"] for x in e.errors()]


def _parse_report(result: AgentResult) -> BuildReport | None:
    return _parse_json(result, BuildReport)[0]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_step_state(title: str) -> dict:
    return {
        "title": title, "status": "pending", "tokens": 0, "calls": 0, "tests": None, "commit": None,
        "warnings": [], "question": None, "error": None, "stage": None, "rule": None, "phase": None,
        "resume_phase": None, "roles_tokens": {}, "rounds_used": 0, "rounds_total": 0, "rounds": [],
        "reviews": [], "approved": [], "answers": [], "repair_ctx": None, "last_failure": None,
        "risk_approved": False, "base_sha": None,
    }  # fmt: skip


def _inside(path: Path, parent: Path) -> bool:
    p, q = path.resolve(), parent.resolve()
    return p == q or q in p.parents


def check_hidden_tests(hidden: Path, repo: Path, worktree: Path | None = None) -> list[str]:
    """Validate --hidden-tests DIR and return the file names to copy. Raises ExecError (nothing is spent yet).

    Refused: not a directory, empty, subfolders, DIR inside the target repo or inside any orch worktree (the
    builder could read it), or a name that already exists in the repo's tests/ (at HEAD) or in the worktree's tests/."""
    hidden = Path(hidden)
    if not hidden.is_dir():
        raise ExecError(f"--hidden-tests {hidden} is not a directory")
    if _inside(hidden, repo):
        raise ExecError(f"--hidden-tests {hidden} is inside the target repo {repo}; keep it outside so the builder cannot see it")
    roots = [workspace.worktree_root()] + ([worktree] if worktree else [])
    if any(_inside(hidden, r) for r in roots):
        raise ExecError(f"--hidden-tests {hidden} is inside an orch worktree; keep it outside")
    entries = sorted(hidden.iterdir())
    if any(e.is_dir() for e in entries):
        raise ExecError(f"--hidden-tests {hidden} has subfolders; only flat files are copied")
    names = [e.name for e in entries if e.is_file() and not e.name.startswith(".")]  # dotfiles (markers) are skipped
    if not names:
        raise ExecError(f"--hidden-tests {hidden} has no files")
    tracked = set(workspace.git(Path(repo), "ls-tree", "-r", "--name-only", "HEAD", "--", "tests/", check=False).stdout.split())
    clash = [n for n in names if f"tests/{n}" in tracked or (Path(repo) / "tests" / n).exists()]
    if worktree is not None:
        clash += [n for n in names if (worktree / "tests" / n).exists() and n not in clash]
    if clash:
        raise ExecError("--hidden-tests: tests/ already has file(s) named " + ", ".join(clash))
    return names


# ---------- executor ----------


class Executor:
    def __init__(
        self,
        repo: ResolvedRepo,
        run_dir: Path,
        cfg: Config,
        get_adapter: AdapterFactory,
        log: Callable[[str], None] = lambda m: None,
        on_update: Callable[[dict], None] | None = None,
        hidden_tests: Path | None = None,
    ):
        self.repo, self.run_dir, self.cfg, self.get_adapter, self.log = repo, run_dir, cfg, get_adapter, log
        self.on_update = on_update  # display only: called with the meta dict after every save
        self.hidden_tests = Path(hidden_tests).resolve() if hidden_tests else None
        self.meta: dict = {}
        self.plan: Plan | None = None
        self.test_command = ""
        self.retest_command: str | None = None
        self.wt: Path = Path()
        self.last_entry: Entry | None = None  # entry that served the most recent agent call
        self._resume_after: datetime | None = None
        self.last_called = False  # False when the last _agent() found every entry cooling and called nothing

    # -- meta --
    def _save(self) -> None:
        write_meta(self.run_dir, self.meta)
        if self.on_update is not None:
            self.on_update(self.meta)

    def _set_step(self, sid: str, **kw) -> dict:
        s = self.meta["steps"][sid]
        prev, new = s.get("status"), kw.get("status")
        if new is not None and new != prev:  # timestamps are data only: the latest running segment of the step
            if new == "running":
                s["started_at"] = _utc_now()
                s.pop("ended_at", None)
            elif prev == "running":
                s["ended_at"] = _utc_now()
        s.update(kw)
        self._save()
        return s

    def _mark_run_start(self) -> None:
        self.meta.setdefault("started_at", _utc_now())
        self.meta.pop("ended_at", None)

    def _mark_run_end(self) -> None:
        self.meta["ended_at"] = _utc_now()

    def _stop(self, run_status: str, sid: str, message: str) -> ExecOutcome:
        self.meta["status"] = run_status
        self._mark_run_end()
        self._save()
        return ExecOutcome(run_status, sid, message)

    def _st(self, sid: str) -> dict:
        """Step state with every Phase 4 field present (older meta.json files lack them)."""
        s = self.meta["steps"][sid]
        for k, v in new_step_state(s.get("title", "")).items():
            s.setdefault(k, v)
        return s

    # -- setup --
    def _load(self) -> None:
        if not self.repo.test_command:
            raise ExecError(
                "this repo has no test_command in config.yaml; add it under repos: (the plan's test_command is never used)"
            )
        self.test_command = self.repo.test_command
        self.retest_command = self.repo.retest_command or None
        self.plan = load_plan(self.run_dir)
        if self.plan is None:
            raise ExecError("this run has no stored plan (planning failed)")
        if self.plan.needs_input:
            raise ExecError(f"the plan needs input first: {self.plan.question}")
        self.meta = load_meta(self.run_dir)
        for role in ("builder", "repair"):
            if any(e.agent != "claude" for e in self.cfg.roles[role]):
                raise ExecError(f"the {role} role must use agent 'claude' (agy has no tool allow-list)")

    def _load_started(self, need_worktree: bool = True) -> None:
        self._load()
        ex = self.meta.get("exec")
        if not ex or "steps" not in self.meta:
            raise ExecError("this run was never started; use `orch exec`")
        if self.meta.get("status") == "rejected":
            raise ExecError("this run was rejected; start a new run")
        self.wt = Path(ex["worktree"])
        if self.hidden_tests is None and ex.get("hidden_tests"):
            self.hidden_tests = Path(ex["hidden_tests"]["dir"])
        if need_worktree and not self.wt.is_dir():
            raise ExecError(f"the worktree {self.wt} is gone (orch clean?); it cannot be continued")

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
        if self.hidden_tests is not None:  # refuse before a worktree exists
            check_hidden_tests(self.hidden_tests, self.repo.path)
        wt = workspace.create(self.repo.path, self.repo.name or self.repo.path.name, run_id)
        self.wt = wt.path
        self.meta["exec"] = {
            "worktree": str(wt.path),
            "branch": wt.branch,
            "base_sha": wt.base_sha,
            "repo_name": self.repo.name or self.repo.path.name,
            "test_command": self.test_command,
            "retest_command": self.retest_command,
            "warnings": warnings,
        }
        if self.hidden_tests is not None:
            self.meta["exec"]["hidden_tests"] = {"dir": str(self.hidden_tests), "step": None, "files": []}
        self.meta["steps"] = {s.id: new_step_state(s.title) for s in self.plan.topo_order()}
        self.meta["status"] = "executing"
        self._mark_run_start()
        self._save()
        return self._loop()

    def resume(self) -> ExecOutcome:
        self._load_started()
        if self.meta.get("status") == "done":
            return ExecOutcome("done", None, "already done")
        self.meta["status"] = "executing"
        self.meta.pop("resume_after", None)
        self._mark_run_start()
        self._save()
        return self._loop()

    # -- human decisions --
    def _pick(self, step_id: str | None, statuses: tuple[str, ...], what: str) -> str:
        steps = self.meta["steps"]
        if step_id is not None:
            if step_id not in steps:
                raise ExecError(f"no step {step_id!r} in this run")
            if steps[step_id]["status"] not in statuses:
                raise ExecError(f"step {step_id} is {steps[step_id]['status']}, not {what}")
            return step_id
        found = [sid for sid, s in steps.items() if s["status"] in statuses]
        if len(found) != 1:
            raise ExecError(f"{len(found)} steps are {what}; name one with --step" if found else f"no step is {what}")
        return found[0]

    def approve(self, step_id: str | None = None, retry: bool = False) -> ExecOutcome:
        self._load_started()
        sid = self._pick(step_id, ("awaiting_approval",), "awaiting approval")
        st = self._st(sid)
        stage = st.get("stage")
        if stage == "repairs_exhausted":
            if not retry:
                raise ExecError(
                    f"step {sid} still fails after {st['rounds_used']} repair round(s); orch never commits failing "
                    f"tests or a rejected change. Use --retry for another set of repair rounds."
                )
            self._set_step(sid, status="pending", resume_phase="need_repair", rounds_used=0, stage=None, rule=None,
                           error=None)  # fmt: skip
        elif stage == "post_check":
            approved = st["approved"] + [a for a in st.get("flagged_items") or [] if a not in st["approved"]]
            self._set_step(sid, status="pending", resume_phase="tests", approved=approved, stage=None, rule=None,
                           flagged=None, flagged_items=None)  # fmt: skip
        elif stage == "pre_builder":
            self._set_step(sid, status="pending", risk_approved=True, resume_phase=None, stage=None, rule=None)
        else:
            raise ExecError(f"step {sid} has no recorded stop reason; it cannot be approved")
        self.meta["status"] = "executing"
        self._mark_run_start()
        self._save()
        return self._loop()

    def reject(self, step_id: str) -> ExecOutcome:
        self._load_started(need_worktree=False)
        sid = self._pick(step_id, ("awaiting_approval", "needs_input", "failed", "pending", "running"), "open")
        self._set_step(sid, status="rejected", resume_phase=None)
        return self._stop("rejected", sid, f"step {sid} rejected by you; the run is stopped")

    def answer(self, step_id: str | None, text: str) -> ExecOutcome:
        self._load_started()
        sid = self._pick(step_id, ("needs_input",), "waiting for an answer")
        st = self._st(sid)
        answers = st["answers"] + [text]
        phase = "need_repair" if st.get("stage") == "repair" else None
        self._set_step(sid, status="pending", answers=answers, resume_phase=phase, question=None, stage=None)
        self.meta["status"] = "executing"
        self._mark_run_start()
        self._save()
        return self._loop()

    # -- main loop --
    def _loop(self) -> ExecOutcome:
        assert self.plan is not None
        steps = self.meta["steps"]
        for step in self.plan.topo_order():
            st = self._st(step.id)
            if st["status"] == "done":
                continue
            if st["status"] in ("failed", "needs_input", "awaiting_approval", "rejected"):
                return self._stop(st["status"], step.id, f"step {step.id} is {st['status']}")
            if any(steps[d]["status"] != "done" for d in step.depends_on):
                return self._stop("failed", step.id, f"step {step.id} depends on an unfinished step")
            phase = st.get("resume_phase")
            if step.risk_flags and not st["risk_approved"] and phase is None:
                self._set_step(step.id, status="awaiting_approval", stage="pre_builder", rule=", ".join(step.risk_flags))
                return self._stop("awaiting_approval", step.id, f"step {step.id} has risk flags: {', '.join(step.risk_flags)}")
            outcome = self._run_step(step, phase)
            if outcome is not None:
                return outcome
        self.meta["status"] = "done"
        self._mark_run_end()
        self._save()
        return ExecOutcome("done", None, "all steps done")

    def _warn(self, sid: str, msg: str) -> None:
        s = self._st(sid)
        if msg in s["warnings"]:
            return
        self.log(f"warning [{sid}]: {msg}")
        s["warnings"].append(msg)
        self._save()

    # -- agent calls --
    def _agent(self, sid: str, role_name: str, build: Callable[[Entry], AgentRequest]) -> AgentResult:
        """Run one role call through the router. If every usable entry is rate limited (or cooling) the returned
        result is a RATE_LIMITED failure and `self._resume_after` says when to try again."""
        def with_env(entry: Entry) -> AgentRequest:
            return build(entry).model_copy(update={"env_passthrough": list(self.repo.env_passthrough)})

        routed = call_role(role_name, with_env, self.get_adapter, self.cfg)
        self._resume_after = routed.resume_after if routed.paused else None
        if routed.paused and routed.rate_limited is None:  # nothing was called: all entries already cooling
            self.last_entry = None
            self.last_called = False
            return failure(self.cfg.roles[role_name][0].agent, ErrorKind.RATE_LIMITED, "all entries are in cooldown")
        result = routed.rate_limited if routed.paused else routed.result
        assert result is not None
        self.last_called = True
        self.last_entry = routed.entry
        st = self._st(sid)
        sdir = self.run_dir / "steps" / sid
        if role_name == "builder" and not (sdir / "builder_raw.json").exists():
            raw = "builder_raw.json"
        else:
            raw = f"{role_name}_raw_{st['calls'] + 1}.json"
        (sdir / raw).write_text(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        rt = st["roles_tokens"]
        rt[role_name] = rt.get(role_name, 0) + result.total_tokens
        upd: dict = {"tokens": st["tokens"] + result.total_tokens, "calls": st["calls"] + 1, "roles_tokens": rt}
        if routed.entry is not None:
            used = st.setdefault("roles_used", {})
            used[role_name] = {"entry": routed.entry.label, "fallback": routed.fallback}
            upd["roles_used"] = used
            self.meta.setdefault("calls_log", []).append(
                {"step": sid, "role": role_name, "entry": routed.entry.label, "fallback": routed.fallback}
            )
        if role_name in ("builder", "repair") and result.session_id and not routed.paused:
            upd["session_id"] = result.session_id
            upd["session_entry"] = routed.entry.label if routed.entry else None
        self._set_step(sid, **upd)
        self.meta["exec_tokens"] = self.meta.get("exec_tokens", 0) + result.total_tokens
        by_role = self.meta.setdefault("role_tokens", {})
        by_role[role_name] = by_role.get(role_name, 0) + result.total_tokens
        self._save()
        return result

    def _agent_stop(self, sid: str, role_name: str, result: AgentResult, resume_phase: str | None) -> ExecOutcome:
        if result.error_kind == ErrorKind.RATE_LIMITED:
            self._set_step(sid, status="pending", error="rate limited", resume_phase=resume_phase)
            when = ""
            if self._resume_after is not None:
                self.meta["resume_after"] = self._resume_after.astimezone(timezone.utc).isoformat(timespec="seconds")
                when = " after " + self._resume_after.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            return self._stop("paused", sid, f"rate limited ({role_name}); run paused, `orch resume` later{when}")
        self._set_step(sid, status="failed", error=f"{result.error_kind.value}: {result.text}"[:500], stage=role_name,
                       resume_phase=None)  # fmt: skip
        return self._stop("failed", sid, f"{role_name} failed: {result.error_kind.value}")

    # -- one step --
    def _run_step(self, step: Step, phase: str | None = None) -> ExecOutcome | None:
        """Returns an ExecOutcome to stop the run, or None to continue."""
        sid = step.id
        (self.run_dir / "steps" / sid).mkdir(parents=True, exist_ok=True)
        if phase is None:
            workspace.reset_to_head(self.wt, self._branch)  # a crashed or paused step may have left partial edits
            self._set_step(sid, base_sha=workspace.head_sha(self.wt))
            self._set_step(sid, status="running", error=None, question=None, warnings=[], phase="build", stage=None,
                           rule=None, resume_phase=None)  # fmt: skip
            self.log(f"[{sid}] {step.title}: running builder")
            if (stop := self._build(step)) is not None:
                return stop
            if (stop := self._copy_hidden(step)) is not None:
                return stop
            phase = "checks"
        else:
            self._set_step(sid, status="running", error=None, question=None, resume_phase=None, stage=None, rule=None)
            self.log(f"[{sid}] {step.title}: continuing at {phase}")
            if not self._st(sid).get("base_sha"):
                self._set_step(sid, base_sha=workspace.head_sha(self.wt))
        return self._pipeline(step, phase)

    def _build(self, step: Step) -> ExecOutcome | None:
        assert self.plan is not None
        sid = step.id
        steps = self.meta["steps"]
        done_titles = [steps[s.id]["title"] for s in self.plan.topo_order() if steps[s.id]["status"] == "done"]
        prompt = build_prompt(self.plan, step, done_titles, self.test_command, self._st(sid)["answers"])

        def req(entry: Entry) -> AgentRequest:
            return AgentRequest(prompt=prompt, cwd=self.wt, model=entry.model, timeout_s=BUILDER_TIMEOUT_S,
                                json_schema=load_schema_file(),
                                extra_args=builder_args(self.test_command, builder_instructions()))  # fmt: skip

        result = self._agent(sid, "builder", req)
        if not result.ok:
            return self._agent_stop(sid, "builder", result, None)
        report = _parse_report(result)
        if report is None:
            self._warn(sid, "builder reply was not a valid report; relying on git and the tests")
        elif report.blocked:
            self._set_step(sid, status="needs_input", question=report.question, stage="builder")
            return self._stop("needs_input", sid, f"builder is blocked: {report.question}")
        return None

    def _copy_hidden(self, step: Step) -> ExecOutcome | None:
        """--hidden-tests: after the first built step, before any check, copy DIR's files into <worktree>/tests/.
        The builder never saw them. They count as planned files and are committed with the step."""
        ex = self.meta.get("exec") or {}
        rec = ex.get("hidden_tests")
        if self.hidden_tests is None or not rec or rec.get("step"):
            return None
        sid = step.id
        try:
            names = check_hidden_tests(self.hidden_tests, self.repo.path, self.wt)
        except ExecError as e:
            self._set_step(sid, status="failed", error=str(e), stage="hidden_tests", resume_phase=None)
            return self._stop("failed", sid, str(e))
        dest = self.wt / "tests"
        dest.mkdir(parents=True, exist_ok=True)
        for n in names:
            shutil.copyfile(self.hidden_tests / n, dest / n)
        files = [f"tests/{n}" for n in names]
        rec.update(step=sid, files=files)
        self._set_step(sid, hidden_files=files)
        self.log(f"[{sid}] hidden tests copied: " + ", ".join(files))
        return None

    def _pipeline(self, step: Step, phase: str) -> ExecOutcome | None:
        sid = step.id
        sdir = self.run_dir / "steps" / sid
        while True:
            st = self._st(sid)
            self._set_step(sid, phase=phase)
            if phase == "checks":
                if (stop := self._post_checks(step)) is not None:
                    return stop
                phase = "tests"
            elif phase == "tests":
                state = self._tests(sid, sdir)
                if state == "broken":
                    tests = self._st(sid)["tests"] or {}
                    self._set_step(sid, status="failed", error=tests.get("error") or "tests could not run", stage="tests")
                    return self._stop("failed", sid, f"tests could not run for {sid}: {tests.get('error')}")
                if state == "fail":
                    self._set_step(sid, repair_ctx={"reasons": ["tests"], "issues": []})
                    phase = "need_repair"
                    continue
                has_diff = bool(workspace.diff_uncommitted(self.wt).strip())
                phase = "review" if self.cfg.review == "per_step" and has_diff else "commit"
            elif phase == "review":
                result = self._review(step, sdir)
                if isinstance(result, ExecOutcome):
                    return result
                if result.verdict == "reject":
                    issues = [i.model_dump() for i in result.blocking_issues]
                    self._set_step(sid, repair_ctx={"reasons": ["review"], "issues": issues})
                    phase = "need_repair"
                    continue
                phase = "commit"
            elif phase == "need_repair":
                if st["rounds_used"] >= self.cfg.max_repair_rounds:
                    return self._exhausted(step)
                phase = "repair"
            elif phase == "repair":
                if (stop := self._repair(step, sdir)) is not None:
                    return stop
                phase = "checks"
            elif phase == "commit":
                return self._commit(step, sdir)
            else:
                raise ExecError(f"unknown phase {phase!r}")

    @property
    def _branch(self) -> str:
        return workspace.branch_name(self.run_dir.name)

    def _check_git_state(self, sid: str) -> ExecOutcome | None:
        """Stop when the worktree's HEAD or branch moved since the step began (H3/H4)."""
        base = self._st(sid).get("base_sha")
        problem = workspace.check_head(self.wt, base, self._branch) if base else None
        if problem is None:
            return None
        self._set_step(sid, status="failed", error=problem, stage="git_state_changed", resume_phase=None)
        return self._stop("failed", sid, f"git_state_changed: {problem}")

    def _post_checks(self, step: Step) -> ExecOutcome | None:
        sid = step.id
        if (stop := self._check_git_state(sid)) is not None:
            return stop
        changed = workspace.changed_files(self.wt)  # the truth, not report.files_changed
        extra = outside_plan(changed, step.files + (self._st(sid).get("hidden_files") or []))
        if extra:
            self._warn(sid, "files outside the step's plan: " + ", ".join(extra))
        self._set_step(sid, changed_files=changed)
        approved = {(a["file"], a["rule"]) for a in self._st(sid)["approved"]}  # per file AND rule
        hits: list[dict] = []
        selfhost = is_self_hosting_repo(self.repo.path)
        for f in changed:
            for rule in classify(f, self.cfg.post_checks, selfhost):
                hits.append({"file": f, "rule": rule})
        for item in workspace.removed_tests(self.wt):
            hits.append({"file": item, "rule": "tests_removed"})
        hits = [h for h in hits if (h["file"], h["rule"]) not in approved]
        if not hits:
            return None
        rules = list(dict.fromkeys(h["rule"] for h in hits))
        files = [h["file"] for h in hits]
        self._set_step(sid, status="awaiting_approval", stage="post_check", rule=", ".join(rules), flagged=files,
                       flagged_items=hits)  # fmt: skip
        return self._stop("awaiting_approval", sid, f"{', '.join(rules)}: " + ", ".join(files))

    # -- tests --
    def _tests(self, sid: str, sdir: Path) -> str:
        """'pass' | 'fail' | 'broken'. In repair rounds the quick retest runs first; only the full command gates."""
        st = self._st(sid)
        in_repair = bool(st["rounds"])
        sfx = f"_r{st['rounds_total']}" if in_repair else ""
        cur = st["rounds"][-1] if in_repair else None
        if in_repair and self.retest_command and self.retest_command != self.test_command:
            ok, info = self._run_cmd(self.retest_command, sdir / f"retest{sfx}.txt")
            if cur is not None:
                cur["retest"] = info
            self._save()
            if info.get("unrunnable"):
                return "broken"
            if not ok:
                self._set_step(sid, last_failure=f"retest{sfx}.txt")
                return "fail"
        ok, info = self._run_cmd(self.test_command, sdir / f"tests{sfx}.txt")
        if cur is not None:
            cur["tests"] = info
        self._set_step(sid, tests=info)
        if info.get("unrunnable"):
            return "broken"
        if not ok:
            self._set_step(sid, last_failure=f"tests{sfx}.txt")
            return "fail"
        return "pass"

    def _run_cmd(self, command: str, out_file: Path) -> tuple[bool, dict]:
        """Run a config command in the worktree. Pass/fail by exit code only."""
        t0 = time.perf_counter()
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return False, {"exit_code": None, "passed": False, "error": f"bad test_command: {e}", "duration_s": 0,
                           "unrunnable": True}  # fmt: skip
        out = run_process(argv, cwd=self.wt, env=child_env(passthrough=self.repo.env_passthrough), timeout_s=TEST_TIMEOUT_S)
        text = f"$ {command}\n[exit {out.returncode}]\n{out.stdout}\n--- stderr ---\n{out.stderr}\n"
        if out.timed_out:
            text += "\n[timed out]\n"
        out_file.write_text(text, encoding="utf-8")
        passed = out.returncode == 0 and not out.timed_out and not out.not_found
        err = "timed out" if out.timed_out else ("test command not found" if out.not_found else "")
        return passed, {
            "exit_code": out.returncode, "passed": passed, "error": err,
            "duration_s": round(time.perf_counter() - t0, 1), "unrunnable": out.not_found,
        }  # fmt: skip

    # -- review --
    def _review(self, step: Step, sdir: Path) -> review_mod.Review | ExecOutcome:
        assert self.plan is not None
        sid = step.id
        diff_path = sdir / "diff.patch"
        diff_path.write_text(workspace.diff_uncommitted(self.wt), encoding="utf-8")
        schema = review_mod.load_schema_file()
        role_text = reviewer_instructions()

        def full_prompt(entry: Entry) -> str:
            _, inline = reviewer_args(entry.agent, role_text, sdir.resolve())
            return review_prompt(self.plan, step, diff_path.resolve(), self.test_command, inline,
                                 self._st(sid).get("hidden_files") or None)  # fmt: skip

        def call(make: Callable[[Entry], tuple[str, str | None]]) -> AgentResult:
            """make(entry) -> (prompt, resume_id). Args are per entry: a fallback to agy runs in --mode plan."""

            def req(entry: Entry) -> AgentRequest:
                args, _ = reviewer_args(entry.agent, role_text, sdir.resolve())
                p, resume_id = make(entry)
                return AgentRequest(prompt=p, cwd=self.wt, model=entry.model, timeout_s=REVIEW_TIMEOUT_S,
                                    json_schema=schema, resume_id=resume_id, extra_args=args)  # fmt: skip

            return self._agent(sid, "reviewer", req)

        self.log(f"[{sid}] reviewing")
        first = call(lambda e: (full_prompt(e), None))
        first_entry = self.last_entry
        if not first.ok:
            return self._agent_stop(sid, "reviewer", first, "review")
        review, errors = _parse_json(first, review_mod.Review)
        if review is None:
            feedback = "Your review was invalid:\n" + "\n".join(f"- {e}" for e in errors) + "\nReturn the corrected JSON review."
            bad = json.dumps(first.structured, indent=2) if first.structured is not None else first.text

            def retry_prompt(e: Entry) -> tuple[str, str | None]:
                if e == first_entry and first.session_id:  # same entry: its session already holds the step and the diff
                    return feedback, first.session_id
                # another entry (the router hopped) has seen nothing: full prompt + the bad output + the feedback
                return f"{full_prompt(e)}\n\n## Previous review (invalid)\n{bad}\n\n{feedback}", None

            second = call(retry_prompt) if first.session_id else None
            if second is not None and not second.ok and second.error_kind == ErrorKind.RATE_LIMITED:
                return self._agent_stop(sid, "reviewer", second, "review")
            if second is None or not second.ok:
                second = call(lambda e: (f"{full_prompt(e)}\n\n## Previous review (invalid)\n{bad}\n\n{feedback}", None))
            if not second.ok:
                return self._agent_stop(sid, "reviewer", second, "review")
            review, errors = _parse_json(second, review_mod.Review)
            if review is None:
                self._set_step(sid, status="failed", stage="reviewer", error="reviewer output invalid: " + "; ".join(errors))
                return self._stop("failed", sid, "reviewer returned an invalid review twice")
        st = self._st(sid)
        st["reviews"].append({"round": st["rounds_total"], "verdict": review.verdict, "summary": review.summary,
                              "issues": [i.model_dump() for i in review.issues]})  # fmt: skip
        if st["rounds"]:
            st["rounds"][-1]["verdict"] = review.verdict
        self._save()
        minor = [i for i in review.issues if i.severity == "minor"]
        self.log(f"[{sid}] review: {review.verdict} ({len(review.blocking_issues)} blocking, {len(minor)} minor)")
        return review

    # -- repair --
    def _repair(self, step: Step, sdir: Path) -> ExecOutcome | None:
        assert self.plan is not None
        sid = step.id
        st = self._st(sid)
        n = st["rounds_total"] + 1
        ctx = st.get("repair_ctx") or {"reasons": ["tests"], "issues": []}
        rin = sdir / "repair_input" / f"round{n}"  # the ONLY folder the repair agent may read besides the repo
        rin.mkdir(parents=True, exist_ok=True)
        (rin / "diff.patch").write_text(workspace.diff_uncommitted(self.wt), encoding="utf-8")
        tail = tail_lines(sdir / st["last_failure"], TAIL_LINES) if "tests" in ctx["reasons"] and st.get("last_failure") else ""
        (rin / "test_tail.txt").write_text(tail or "(no test failure this round)\n", encoding="utf-8")
        (rin / "review_issues.json").write_text(json.dumps(ctx["issues"], indent=2) + "\n", encoding="utf-8")
        prompt = repair_prompt(self.plan, step, ctx, tail, rin.resolve(), self.test_command, self.retest_command,
                               st["answers"], st.get("hidden_files") or None)  # fmt: skip
        args = repair_args(self.test_command, self.retest_command, repair_instructions(), rin.resolve())
        schema = load_schema_file()

        session_entry = st.get("session_entry")

        def call(allow_resume: bool) -> AgentResult:
            """Resume the builder/last repair session only on the entry that created it; any other entry starts fresh."""

            def req(entry: Entry) -> AgentRequest:
                resume = bool(allow_resume and st.get("session_id") and entry.label == session_entry)
                return AgentRequest(prompt=(RESUME_HEADER + prompt) if resume else prompt, cwd=self.wt, model=entry.model,
                                    timeout_s=REPAIR_TIMEOUT_S, json_schema=schema,
                                    resume_id=st["session_id"] if resume else None, extra_args=args)  # fmt: skip

            return self._agent(sid, "repair", req)

        before = st["roles_tokens"].get("repair", 0)
        self.log(f"[{sid}] repair round {st['rounds_used'] + 1}/{self.cfg.max_repair_rounds}: " + " + ".join(ctx["reasons"]))
        mode, resume_failed = "fresh", False
        result: AgentResult | None = None
        if st.get("session_id"):
            result = call(True)
            if result.ok or result.error_kind == ErrorKind.RATE_LIMITED:
                mode = "resumed" if (self.last_entry and self.last_entry.label == session_entry) else "fresh"
            else:
                self.log(f"[{sid}] resume failed ({result.error_kind.value}); using a fresh call")
                result, mode, resume_failed = None, "fresh", True
        if result is None:
            result = call(False)
        report = _parse_report(result) if result.ok else None
        if not result.ok:
            if result.error_kind == ErrorKind.RATE_LIMITED:
                outcome = "rate_limited"
            else:
                outcome = f"failed: {result.error_kind.value}"
        elif report is not None and report.blocked:
            outcome = "blocked"
        else:
            outcome = "ok"
        called = self.last_called  # False only when every entry was cooling and nothing was called
        st = self._st(sid)
        if called:  # a repair call that was made counts as a round whatever its outcome
            st["rounds"].append({
                "n": n, "reasons": ctx["reasons"], "session": mode, "resume_failed": resume_failed, "outcome": outcome,
                "tokens": st["roles_tokens"].get("repair", 0) - before, "retest": None, "tests": None, "verdict": None,
            })  # fmt: skip
            self._set_step(sid, rounds_used=st["rounds_used"] + 1, rounds_total=n)
        if not result.ok:
            return self._agent_stop(sid, "repair", result, "need_repair")
        if report is None:
            self._warn(sid, "repair reply was not a valid report; relying on git and the tests")
        elif report.blocked:
            self._set_step(sid, status="needs_input", question=report.question, stage="repair")
            return self._stop("needs_input", sid, f"repair is blocked: {report.question}")
        return None

    def _exhausted(self, step: Step) -> ExecOutcome:
        sid = step.id
        st = self._st(sid)
        ctx = st.get("repair_ctx") or {"reasons": [], "issues": []}
        parts = []
        if "tests" in ctx["reasons"]:
            parts.append(f"tests still fail (see {self.run_dir / 'steps' / sid / (st.get('last_failure') or 'tests.txt')})")
        if ctx["issues"]:
            top = "; ".join(
                f"{i['file']}{':' + str(i['line']) if i.get('line') else ''} {i['problem']}"[:100] for i in ctx["issues"][:3]
            )
            parts.append(f"{len(ctx['issues'])} blocker/major review issue(s): {top}")
        summary = f"after {st['rounds_used']} repair round(s): " + "; ".join(parts)
        self._set_step(sid, status="awaiting_approval", stage="repairs_exhausted", rule="tests_fail_after_3_repairs",
                       summary=summary, resume_phase=None)  # fmt: skip
        return self._stop("awaiting_approval", sid, summary)

    def _commit(self, step: Step, sdir: Path) -> ExecOutcome | None:
        sid = step.id
        st = self._st(sid)
        if (stop := self._check_git_state(sid)) is not None:
            return stop
        sha = workspace.commit_step(self.wt, sid, step.title, st.get("changed_files") or [], self._branch)
        (sdir / "diff.patch").write_text(workspace.diff(self.wt, f"{sha}^", sha), encoding="utf-8")
        self._set_step(sid, status="done", commit=sha, phase=None, resume_phase=None, repair_ctx=None)
        self.log(f"[{sid}] done {sha[:8]}")
        return None
