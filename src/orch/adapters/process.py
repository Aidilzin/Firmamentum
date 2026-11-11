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


# Env for every child (workers and test runs) is built from an allow-list, so API keys, tokens and cloud credentials in
# your shell never reach agent-written test code. Names are compared upper-case (Windows env names are not case
# sensitive). Add more per repo with `env_passthrough` in config.yaml.
ENV_ALLOW = frozenset({
    "PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME",
    # needed to start programs and find the user's folders on Windows; none of these hold secrets
    "PATHEXT", "COMSPEC", "WINDIR", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "OS", "LANG", "LC_ALL", "TERM", "TZ", "PYTHONUTF8", "PYTHONIOENCODING",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH",
})  # fmt: skip
ENV_ALLOW_PREFIXES = ("UV_",)
ENV_ALWAYS_DROP_PREFIXES = ("ANTHROPIC_",)  # never reaches any child, even via env_passthrough
VENV_VARS = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")


def child_env(extra_scrub: tuple[str, ...] = (), passthrough: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """Env for every child process orch starts: only allow-listed names, plus `passthrough` (per-repo config).
    `ANTHROPIC_*` is always dropped, and so are venv markers and `extra_scrub`."""
    extra = {p.upper() for p in passthrough}
    drop = {d.upper() for d in (*VENV_VARS, *extra_scrub)}
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        u = k.upper()
        if u in drop or u.startswith(ENV_ALWAYS_DROP_PREFIXES):
            continue
        if u in ENV_ALLOW or u.startswith(ENV_ALLOW_PREFIXES) or u in extra:
            out[k] = v
    return out


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
        # Binary pipes: encode stdin ourselves so Windows never turns LF into CRLF.
        stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
        stdout, stderr = proc.communicate(input=stdin_bytes, timeout=timeout_s)
        out.returncode = proc.returncode
    except subprocess.TimeoutExpired:
        out.timed_out = True
        kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # pipes held by orphans; give up on the remaining output
            stdout, stderr = b"", b""
        out.returncode = proc.returncode
    except BaseException:  # Ctrl-C, SystemExit, anything: never leave the child running
        kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
    out.stdout = (stdout or b"").decode("utf-8", errors="replace")
    out.stderr = (stderr or b"").decode("utf-8", errors="replace")
    out.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return out
