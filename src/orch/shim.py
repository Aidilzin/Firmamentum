"""The `orch-test` shim: the one Bash command the builder and repair agents may run, executed in the sandbox.

Docs: docs/sandbox-design.md, Phase 4. orch writes a small folder before an agent call:

    <worktree root>/../shims/<run id>/orch-test      a `#!/bin/sh` script (Git Bash runs it; the agent's Bash is Git Bash)
    <worktree root>/../shims/<run id>/ticket.json    everything the runner needs, from config.yaml and orch's own state
    <worktree root>/../shims/<run id>/calls.jsonl    one line per run, written by the runner (never by the agent)

The agent's allow rule is exactly `Bash(orch-test)`. The script calls back into orch (`python -I -m orch.shim <ticket>`),
which goes through `sandbox.run_tests`, the same code orch's own test runs use. orch decides "sandbox problem" ONLY from
`calls.jsonl` (written here, from the engine's answer), never from what the agent reports or what a test prints.

The folder lives outside the worktree, so `--restricted` keeps the agent's file tools away from it. The runner runs with
`-I` (isolated mode), so nothing in the worktree can be imported by it. Standard library plus orch's own modules only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Sequence

from . import sandbox, workspace
from .adapters.process import child_env
from .config import ResolvedRepo, SandboxConfig

SHIM_NAME = "orch-test"
TICKET_NAME = "ticket.json"
CALLS_NAME = "calls.jsonl"
ALLOW_RULE = f"Bash({SHIM_NAME})"  # exact match, no `*`: no arguments reach the runner

# What the agent's shell sees. 75 (EX_TEMPFAIL) does not clash with pytest's 0 to 5.
EXIT_USAGE = 2
EXIT_INFRA = 75
EXIT_TIMEOUT = 124
EXIT_UNRUNNABLE = 127
EXIT_CANCELLED = 130

INFRA_PREFIX = "ORCH SANDBOX ERROR: "
INFRA_ADVICE = (
    "This is not a test failure, and no change to the code can fix it. Do not work around it. Stop, set `blocked` to true "
    "and put the line above in `question`."
)
DEPENDENCY_MESSAGE = (
    "the test environment does not match the project's dependency files (pyproject.toml or uv.lock changed). orch prepares "
    "an environment for changed dependency files only after the human approves them. Undo that change, or set `blocked`"
)

_BAD_QUOTED = set("'\n\r\0")  # the script single-quotes its paths


class ShimError(Exception):
    """The shim cannot be written or no longer matches what orch wrote."""


@dataclass(frozen=True)
class ShimHandle:
    root: Path  # the shim folder of this run
    script: Path
    ticket: Path
    hashes: dict[str, str]  # file name -> sha256 of what orch wrote

    @property
    def calls_file(self) -> Path:
        return self.root / CALLS_NAME


# ---------- writing the shim ----------


def shims_root() -> Path:
    """`<worktree root>/../shims`: next to the worktrees, never inside one."""
    return workspace.worktree_root().parent / "shims"


def _sh_path(p: str) -> str:
    """`C:\\a\\b` -> `/c/a/b` for Git Bash to run; anything else is returned as is."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    return f"/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}" if m else p


def _quoted(p: str, what: str) -> str:
    if _BAD_QUOTED & set(p):
        raise ShimError(f"{what} has a character the shim cannot quote safely: {p!r}")
    return f"'{p}'"


def shim_text(run_id: str, python: str, ticket: str) -> str:
    """The script, as text. Pure. `python` is the interpreter orch itself runs on; `ticket` is the ticket's path."""
    py = _quoted(_sh_path(python), "the python path")
    tk = _quoted(ticket, "the ticket path")
    return (
        "#!/bin/sh\n"
        f"# orch-test for run {run_id}. Written by orch for one agent call; a change to this file stops the run.\n"
        '[ "$#" -eq 0 ] || { echo "orch-test takes no arguments: it runs the project\'s tests in the sandbox" >&2; exit 2; }\n'
        "export MSYS_NO_PATHCONV=1\n"  # Git Bash would otherwise rewrite arguments that look like /paths
        f"exec {py} -I -m orch.shim {tk}\n"
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_shim(
    run_id: str, worktree: Path, repo: ResolvedRepo, cfg: SandboxConfig, branch: str, argv: Sequence[str], timeout_s: int,
    *, root: Path | None = None, python: str | None = None,
) -> ShimHandle:  # fmt: skip
    """Write the shim folder for one agent call (replacing any leftover of an earlier one) and return its handle.
    `argv` is `shlex.split` of the test command from config.yaml, never from a plan."""
    base = Path(root) if root is not None else shims_root()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id or ""):
        raise ShimError(f"bad run id: {run_id!r}")
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise ShimError("the test command is empty")
    folder = base / run_id
    wt = Path(worktree).resolve()
    if folder.resolve() == wt or wt in folder.resolve().parents or folder.resolve() in wt.parents:
        raise ShimError(f"the shim folder {folder} must not be inside the worktree {wt}")
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)  # a leftover of a hard-killed call
    folder.mkdir(parents=True)
    ticket = {
        "run_id": run_id, "worktree": str(wt), "repo_name": repo.name, "repo_path": str(Path(repo.path).resolve()),
        "branch": branch, "image": repo.image, "memory": cfg.memory, "cpus": cfg.cpus,
        "env_passthrough": list(repo.env_passthrough), "argv": list(argv), "timeout_s": int(timeout_s),
    }  # fmt: skip
    ticket_path = folder / TICKET_NAME
    ticket_bytes = (json.dumps(ticket, indent=2) + "\n").encode("utf-8")
    script_path = folder / SHIM_NAME
    script_bytes = shim_text(run_id, python or sys.executable, str(ticket_path)).encode("utf-8")  # LF only
    ticket_path.write_bytes(ticket_bytes)
    script_path.write_bytes(script_bytes)
    try:
        script_path.chmod(0o755)
    except OSError:
        pass
    return ShimHandle(folder, script_path, ticket_path, {SHIM_NAME: _sha(script_bytes), TICKET_NAME: _sha(ticket_bytes)})


def assert_outside(handle: ShimHandle, *dirs: Path) -> None:
    """Raise ShimError if the shim folder is, or is inside, or contains, any of `dirs` (the folders the agent may touch)."""
    me = handle.root.resolve()
    for d in dirs:
        other = Path(d).resolve()
        if me == other or other in me.parents or me in other.parents:
            raise ShimError(f"the shim folder {me} overlaps {other}, which the agent can reach")


def verify(handle: ShimHandle) -> str | None:
    """None when both files are exactly what orch wrote; else a sentence saying what changed."""
    for name, want in handle.hashes.items():
        p = handle.root / name
        try:
            got = _sha(p.read_bytes())
        except OSError:
            return f"the orch-test shim file {name} is gone"
        if got != want:
            return f"the orch-test shim file {name} was changed during the agent call"
    return None


def remove(handle: ShimHandle) -> None:
    shutil.rmtree(handle.root, ignore_errors=True)


# ---------- the agent's environment ----------


def shim_path_value(handle: ShimHandle, base_path: str) -> str:
    """PATH for the agent's process: the shim folder first, then the old entries minus empty and relative ones (a relative
    entry would resolve against the worktree, where the agent could plant a file named orch-test)."""
    keep = [e for e in base_path.split(os.pathsep) if e and os.path.isabs(e)]
    return os.pathsep.join([str(handle.root), *keep])


def shim_env(handle: ShimHandle, timeout_s: int, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment overrides for the agent's `claude` process (`AgentRequest.env_overrides`): PATH, and the two Bash timeout
    variables set above the runner's own timeout so the runner's cleanup always fires first."""
    env = base_env if base_env is not None else child_env()
    path = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    ms = str((int(timeout_s) + 120) * 1000)
    return {"PATH": shim_path_value(handle, path), "BASH_DEFAULT_TIMEOUT_MS": ms, "BASH_MAX_TIMEOUT_MS": ms}


# ---------- what the runner recorded ----------


@dataclass(frozen=True)
class Call:
    kind: str  # ran | infra | unrunnable | timed_out | cancelled
    exit_code: int | None
    detail: str
    duration_s: float


def read_calls(handle: ShimHandle) -> list[Call]:
    """The runner's records, oldest first. A line that is not valid is ignored (it can only come from a crashed runner)."""
    out: list[Call] = []
    try:
        text = handle.calls_file.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            d = json.loads(line)
            out.append(Call(str(d["kind"]), d.get("exit_code"), str(d.get("detail") or ""), float(d.get("duration_s") or 0)))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def infra_calls(calls: Sequence[Call]) -> list[Call]:
    """The only source of "the sandbox failed": records the runner wrote, never the agent's words or the tests' output."""
    return [c for c in calls if c.kind == "infra"]


def summary(calls: Sequence[Call]) -> str:
    if not calls:
        return "ran orch-test 0 times"
    ok = sum(1 for c in calls if c.kind == "ran" and c.exit_code == 0)
    bad = sum(1 for c in calls if c.kind == "ran" and c.exit_code != 0)
    odd = len(calls) - ok - bad
    parts = [f"{ok} passed", f"{bad} failed"] + ([f"{odd} not run ({', '.join(sorted({c.kind for c in calls if c.kind != 'ran'}))})"] if odd else [])
    return f"ran orch-test {len(calls)} time{'s' if len(calls) != 1 else ''}: " + ", ".join(parts)


# ---------- the runner: `python -I -m orch.shim <ticket>` ----------


def _record(folder: Path, res_kind: str, code: int | None, detail: str, t0: float) -> None:
    line = json.dumps({"kind": res_kind, "exit_code": code, "detail": detail, "duration_s": round(time.monotonic() - t0, 1)})
    try:
        with open(folder / CALLS_NAME, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # without a record orch sees no infra call; the run still ends, with the tests' own exit code


def _emit(stream: IO[bytes], text: str) -> None:
    stream.write(text.encode("utf-8", errors="replace"))
    stream.flush()


def _infra_text(detail: str) -> str:
    return f"{INFRA_PREFIX}{detail}\n{INFRA_ADVICE}\n"


def _load_ticket(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("run_id", "worktree", "repo_path", "branch", "image", "memory", "cpus", "argv", "timeout_s"):
        if key not in data:
            raise ValueError(f"ticket has no {key}")
    if not isinstance(data["argv"], list) or not data["argv"] or not all(isinstance(a, str) and a for a in data["argv"]):
        raise ValueError("ticket argv must be a non-empty list of strings")
    return data


def run_ticket(ticket_path: Path, out: IO[bytes], err: IO[bytes]) -> int:
    """Run the tests of one ticket in the sandbox. Writes what the agent should see; returns the exit code for its shell."""
    t0 = time.monotonic()
    folder = ticket_path.parent
    try:
        t = _load_ticket(ticket_path)
        wt = workspace._guard(Path(t["worktree"]), Path(t["repo_path"]), t["branch"])  # again, here, on what is on disk now
        repo = ResolvedRepo(name=t.get("repo_name"), path=Path(t["repo_path"]), test_command=" ".join(t["argv"]), sandbox="docker",
                            image=t["image"], env_passthrough=list(t.get("env_passthrough") or []))  # fmt: skip
        cfg = SandboxConfig(image=t["image"], memory=t["memory"], cpus=t["cpus"])
        res = sandbox.run_tests(repo, cfg, wt, t["argv"], t["run_id"], warm_src=None, timeout_s=int(t["timeout_s"]), own_only=True)
    except Exception as e:  # a refused ticket, a bad config value, a guard failure: the agent must hear it, orch must record it
        detail = f"the orch-test runner refused to run: {e}"
        _record(folder, "infra", None, detail, t0)
        _emit(err, _infra_text(detail))
        return EXIT_INFRA
    except KeyboardInterrupt:
        _record(folder, "cancelled", None, "interrupted", t0)
        _emit(err, "orch-test: stopped\n")
        return EXIT_CANCELLED
    if res.kind == "ran":
        _record(folder, "ran", res.exit_code, "", t0)
        _emit(out, res.stdout)
        _emit(err, res.stderr)
        return res.exit_code if res.exit_code is not None else EXIT_INFRA
    if res.kind == "timed_out":
        _record(folder, "timed_out", None, f"timed out after {t['timeout_s']} s", t0)
        _emit(out, res.stdout)
        _emit(err, res.stderr)
        _emit(err, f"\norch-test: the tests timed out after {t['timeout_s']} s; the container was stopped.\n")
        return EXIT_TIMEOUT
    if res.kind == "cancelled":
        _record(folder, "cancelled", None, "cancelled", t0)
        _emit(err, "orch-test: stopped\n")
        return EXIT_CANCELLED
    if res.kind == "unrunnable":
        detail = f"{res.detail} (exit {res.exit_code})"
        _record(folder, "unrunnable", res.exit_code, detail, t0)
        _emit(err, _infra_text(detail))
        return EXIT_UNRUNNABLE
    # infra
    detail = DEPENDENCY_MESSAGE if res.exit_code == sandbox.EXIT_VENV_NOT_READY else res.detail
    _record(folder, "infra", res.exit_code, detail, t0)
    _emit(err, _infra_text(detail))
    return EXIT_INFRA


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        _emit(sys.stderr.buffer, "usage: python -I -m orch.shim <ticket.json>\n")
        return EXIT_USAGE
    return run_ticket(Path(argv[0]), sys.stdout.buffer, sys.stderr.buffer)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
