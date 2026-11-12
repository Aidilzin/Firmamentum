"""Golden-output checks for the one-shot CLI: exit code, the interleaved output and stderr, compared with a stored file.

`ORCH_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_cli.py` rewrites the files; review the diff before committing."""

from __future__ import annotations

import os
import re
from pathlib import Path

from typer.testing import CliRunner

from orch import cli

GOLDEN_DIR = Path(__file__).parent / "golden"

_RUN_ID = re.compile(r"\d{8}T\d{6}Z(?:-[a-z0-9]+)*")
_ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[+-]\d\d:\d\d|Z)?")
_LOCAL = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d(?: [A-Za-z][A-Za-z0-9 +\-]*?)?(?= \(usage limit\)|$)", re.MULTILINE)
_ELAPSED = re.compile(r"\b\d+:\d\d\b")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_TMP_PATH = re.compile(r"<TMP>[^\s;'\")]*")


def scrub(text: str, tmp_path: Path) -> str:
    """Replace everything that changes between runs: the temp folder, run ids, times and git hashes."""
    for p in sorted({str(tmp_path), str(tmp_path.resolve()), tmp_path.as_posix(), tmp_path.resolve().as_posix()}, key=len, reverse=True):
        text = text.replace(p, "<TMP>")
    text = _TMP_PATH.sub(lambda m: m.group(0).replace("\\", "/"), text)
    text = _RUN_ID.sub("<RUN>", text)
    text = _ISO.sub("<ISO>", text)
    text = _LOCAL.sub("<WHEN>", text)
    text = _ELAPSED.sub("<T>", text)
    return _SHA.sub("<SHA>", text)


def render(result, tmp_path: Path) -> str:
    return (
        f"exit: {result.exit_code}\n"
        f"--- output ---\n{scrub(result.output, tmp_path)}"
        f"--- stderr ---\n{scrub(result.stderr, tmp_path)}"
        "--- end ---\n"
    )


def check(name: str, result, tmp_path: Path) -> None:
    """Compare one CLI result with tests/golden/<name>.txt."""
    assert "Traceback" not in result.output, result.output
    got = render(result, tmp_path)
    path = GOLDEN_DIR / f"{name}.txt"
    if os.environ.get("ORCH_UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(got, encoding="utf-8", newline="\n")
        return
    assert path.is_file(), f"no golden file {path.name}; run with ORCH_UPDATE_GOLDEN=1 and review it"
    assert got == path.read_text(encoding="utf-8"), f"output differs from tests/golden/{path.name}"


def invoke(args: list[str], **kw):
    """Run the CLI with ASCII glyphs, so the files do not depend on the console encoding."""
    return CliRunner().invoke(cli.app, ["--ascii", *args], **kw)
