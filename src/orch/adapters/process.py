"""Shared subprocess runner: timeout, process-tree kill, UTF-8 capture. No shell is ever used."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
STDERR_TAIL_CHARS = 2000


@dataclass
class ProcessOutput:
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    not_found: bool = False
    elapsed_ms: int = 0
    error: str = ""  # spawn failure description, if any

    @property
    def stderr_tail(self) -> str:
        return self.stderr[-STDERR_TAIL_CHARS:]


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and all its descendants so no agent keeps running."""
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    timeout_s: int = 600,
) -> ProcessOutput:
    out = ProcessOutput()
    exe = shutil.which(argv[0], path=(env or os.environ).get("PATH")) or argv[0]
    full = [exe, *argv[1:]]
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(
            full,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    except FileNotFoundError as e:
        out.not_found = True
        out.error = f"executable not found: {argv[0]} ({e})"
        return out
    except OSError as e:
        out.error = f"failed to spawn {argv[0]}: {e}"
        return out

    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_s)
        out.returncode = proc.returncode
    except subprocess.TimeoutExpired:
        out.timed_out = True
        kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # pipes held by orphans; give up on the remaining output
            stdout, stderr = "", ""
        out.returncode = proc.returncode
    out.stdout = stdout or ""
    out.stderr = stderr or ""
    out.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return out
