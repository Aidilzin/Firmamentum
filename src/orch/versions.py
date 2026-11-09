"""Version helpers: orch's own version and the `--version` output of the worker CLIs."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from orch.adapters.claude_code import SCRUBBED_ENV
from orch.adapters.process import ProcessOutput, run_process

VERSION_TIMEOUT_S = 15
MAX_REASON_CHARS = 200


def orch_version() -> str:
    try:
        return version("orch")
    except PackageNotFoundError:
        return "unknown"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _error(reason: str) -> str:
    reason = reason[:MAX_REASON_CHARS] or "unknown"
    return f"error ({reason})"


def cli_version(name: str, runner=run_process) -> str:
    env = None
    if name == "claude":
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    out: ProcessOutput = runner(
        [name, "--version"], cwd=Path.cwd(), env=env, timeout_s=VERSION_TIMEOUT_S
    )
    if out.not_found:
        return "not found"
    if out.timed_out:
        return _error("timeout")
    if out.error:
        return _error(_first_line(out.error))
    if out.returncode != 0:
        return _error(_first_line(out.stderr) or f"exit {out.returncode}")
    return _first_line(out.stdout) or _first_line(out.stderr) or _error("no output")
