"""`orch sandbox test`: run a repo's tests by hand through exactly the path a real run uses (sandbox.run_tests).

No model call, no worktree, no run folder. `--probe` plants three tests that try to escape and checks that all three fail
inside while the repo's real tests pass.
"""

from __future__ import annotations

import re
import secrets
import shlex
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Literal

from . import sandbox, workspace
from .config import ResolvedRepo, SandboxConfig

PROBE_KINDS = ("write", "network", "host env")
_BLOCKED = re.compile(r"^BLOCKED: (write|network|host env): (.*)$", re.MULTILINE)


class ManualError(Exception):
    """A request that cannot be run (no test command, the repo is `sandbox: off`, a missing folder)."""


@dataclass
class ManualResult:
    verdict: Literal["pass", "failed", "error", "cancelled"]
    run: sandbox.TestRun
    message: str
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (kind, how) the probe saw

    @property
    def exit_code(self) -> int:
        return {"pass": 0, "failed": 1, "error": 2, "cancelled": 130}[self.verdict]


def probe_file() -> Path:
    return sandbox.dockerfile_dir() / "probe" / "probe_escape.py"


@contextmanager
def _as_is(folder: Path) -> Iterator[Path]:
    yield folder


def _command(target: ResolvedRepo, retest: bool) -> list[str]:
    text = target.retest_command if retest else target.test_command
    if not text:
        raise ManualError(f"this repo has no {'retest_command' if retest else 'test_command'} in config.yaml")
    try:
        return shlex.split(text)
    except ValueError as e:
        raise ManualError(f"bad {'retest_command' if retest else 'test_command'}: {e}") from e


def _verdict(res: sandbox.TestRun) -> tuple[str, str]:
    if res.kind == "cancelled":
        return "cancelled", "stopped by you; the container was removed"
    if res.kind == "infra":
        return "error", f"SANDBOX ERROR: {res.detail}"
    if res.kind == "unrunnable":
        return "error", f"SANDBOX ERROR: {res.detail} (exit {res.exit_code})"
    if res.kind == "timed_out":
        return "failed", "TESTS FAILED (timed out)"
    if res.passed:
        return "pass", "PASS"
    return "failed", f"TESTS FAILED (exit {res.exit_code})"


def run_manual(
    target: ResolvedRepo, cfg: SandboxConfig, *, src: Path | None = None, retest: bool = False, timeout_s: int = 600,
    probe: bool = False, log: Callable[[str], None] = lambda m: None, cancel: threading.Event | None = None,
) -> ManualResult:  # fmt: skip
    """Run the repo's test command (or retest command) in the sandbox. `src` defaults to the repo itself. With `probe`, the
    tests run on a scratch export of `src`'s HEAD with the escape tests added, and that folder is deleted afterwards."""
    if target.sandbox == "off":
        raise ManualError("this repo has `sandbox: off`; there is nothing to test in a sandbox")
    command = _command(target, retest)
    folder = Path(src).resolve() if src is not None else Path(target.path).resolve()
    if not folder.is_dir():
        raise ManualError(f"{folder} is not a folder")
    token = secrets.token_hex(4)
    extra_env: dict[str, str] = {}
    scratch: Path | None = None
    try:
        if probe:
            scratch = workspace.warm_root() / f"probe-{token}"
            try:
                workspace.export_tree(folder, "HEAD", scratch)
            except workspace.WorkspaceError as e:
                raise ManualError(f"--probe needs a git repository with a commit: {e}") from e
            (scratch / "tests").mkdir(exist_ok=True)
            shutil.copyfile(probe_file(), scratch / "tests" / "test_escape_probe.py")
            folder = scratch
            extra_env["ORCH_PROBE_CANARY"] = f"canary-{token}"  # in the docker CLI's environment only, never in the argv
            if "pytest" in command:
                command = [*command, "-rP"]  # show what the passing probe tests printed
        res = sandbox.run_tests(target, cfg, folder, command, f"manual-{token}", warm_src=lambda: _as_is(folder),
                                timeout_s=timeout_s, cancel=cancel, log=log, extra_env=extra_env)  # fmt: skip
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    verdict, message = _verdict(res)
    if not probe:
        return ManualResult(verdict, res, message)  # type: ignore[arg-type]
    seen = [(m.group(1), m.group(2).strip()) for m in _BLOCKED.finditer(res.stdout.replace("\r", ""))]
    if verdict == "failed":
        escaped = next((ln.strip() for ln in res.stdout.splitlines() if "ESCAPED:" in ln), "")
        message = "PROBE FAILED: " + (escaped or message + "; a real test failed or an escape attempt got through")
    if verdict != "pass":
        return ManualResult(verdict, res, message, seen)  # type: ignore[arg-type]
    missing = [k for k in PROBE_KINDS if k not in {kind for kind, _ in seen}]
    if missing:
        return ManualResult("failed", res, "PROBE FAILED: tests passed but no BLOCKED line for " + ", ".join(missing), seen)
    return ManualResult("pass", res, "PROBE OK: the real tests pass and all three escape attempts were blocked", seen)
