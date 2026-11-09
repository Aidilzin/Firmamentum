"""Phase 0 spike: drive `claude -p` and `agy -p` headless from Python on Windows.

Stdlib only. Spawns only the official executables; never touches credentials.
Usage:
    uv run python spike.py                  # real calls (costs plan quota)
    uv run python spike.py --from-fixtures  # re-parse saved fixtures, no CLI calls
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROMPT = "Reply PONG"
TIMEOUT_S = 120
FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_SCRUB_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


@dataclass
class Result:
    cli: str
    ok: bool = False
    error: str = ""  # classified failure, empty on success
    reply: str = ""
    duration_s: float = 0.0
    usage: dict = field(default_factory=dict)
    raw_stdout: str = ""
    raw_json: object = None
    command: list[str] = field(default_factory=list)
    prompt_via: str = ""  # "stdin" | "argument"
    output_format: str = ""


def clean_env(scrub: tuple[str, ...] = ()) -> dict[str, str]:
    env = os.environ.copy()
    for k in scrub:
        env.pop(k, None)
    return env


def run_cli(cli: str, args: list[str], prompt: str, via_stdin: bool, env: dict) -> Result:
    """Run one CLI call. Returns a Result with a classified error on failure."""
    res = Result(cli=cli, prompt_via="stdin" if via_stdin else "argument")
    exe = shutil.which(cli)
    if not exe:
        res.error = f"exe not found: '{cli}' is not on PATH"
        return res
    # Non-stdin: replace the "{prompt}" token in args, or append the prompt if absent.
    if via_stdin:
        argv = [exe, *args]
    elif any("{prompt}" in a for a in args):
        argv = [exe, *[a.replace("{prompt}", prompt) for a in args]]
    else:
        argv = [exe, *args, prompt]
    res.command = [cli, *argv[1:]]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            input=prompt if via_stdin else None,
            stdin=None if via_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        res.duration_s = time.perf_counter() - t0
        res.error = f"timeout after {TIMEOUT_S}s"
        return res
    except OSError as e:
        res.error = f"failed to spawn: {e}"
        return res
    res.duration_s = time.perf_counter() - t0
    res.raw_stdout = proc.stdout or ""
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-400:]
        res.error = f"non-zero exit {proc.returncode}; stderr tail: {tail!r}; stdout head: {res.raw_stdout[:200]!r}"
        return res
    if not res.raw_stdout.strip():
        res.error = f"empty stdout (exit 0); stderr tail: {(proc.stderr or '').strip()[-300:]!r}"
        return res
    return res


def parse_json_stdout(res: Result) -> None:
    """Parse stdout as one JSON doc, or NDJSON for stream-json. Sets res.error on failure."""
    text = res.raw_stdout.strip()
    try:
        res.raw_json = json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    if res.output_format == "stream-json":
        events = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                res.error = f"invalid JSON on NDJSON line {i}: {line[:120]!r}"
                return
        res.raw_json = events
        return
    res.error = f"invalid JSON: {text[:200]!r}"


# ---------- per-CLI extraction ----------

def extract_claude(res: Result) -> None:
    d = res.raw_json
    if isinstance(d, list):  # some versions emit an array of events
        d = next((e for e in reversed(d) if isinstance(e, dict) and e.get("type") == "result"), None)
    if not isinstance(d, dict):
        res.error = "claude: unexpected JSON shape (no result object)"
        return
    res.reply = str(d.get("result", ""))
    u = d.get("usage") or {}
    res.usage = {
        "input": u.get("input_tokens"),
        "output": u.get("output_tokens"),
        "cached": (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
    }
    if d.get("is_error") or d.get("subtype", "success") != "success":
        res.error = f"claude reported error: is_error={d.get('is_error')} subtype={d.get('subtype')} result={res.reply[:200]!r}"


def extract_agy(res: Result) -> None:
    d = res.raw_json
    if not isinstance(d, dict):
        res.error = "agy: unexpected JSON shape (expected one object)"
        return
    res.reply = str(d.get("response", "")).strip()
    u = d.get("usage") or {}
    res.usage = {
        "input": u.get("input_tokens"),
        "output": u.get("output_tokens"),
        "cached": u.get("cache_read_tokens"),
    }
    if d.get("status") != "SUCCESS":
        res.error = f"agy reported non-SUCCESS status: {d.get('status')!r}"


# ---------- orchestration of the two spikes ----------

def spike_claude() -> Result:
    args = ["-p", "--output-format", "json", "--model", "sonnet"]
    env = clean_env(CLAUDE_SCRUB_ENV)
    res = run_cli("claude", args, PROMPT, True, env)
    res.output_format = "json"
    if not res.error:
        parse_json_stdout(res)
    if not res.error:
        extract_claude(res)
    res.ok = not res.error
    return res


def spike_agy() -> tuple[Result, list[Result]]:
    """Try stdin+json first; fall back to argument+json; then argument+stream-json.

    Returns (winning-or-last result, all attempts) so findings can report the bug check.
    """
    attempts: list[Result] = []
    plan = [
        (True, "json"),
        (False, "json"),
        (False, "stream-json"),
    ]
    env = clean_env()
    for via_stdin, fmt in plan:
        # agy's -p greedily takes the next token as its prompt, so put it last
        # (stdin) or attach the prompt with -p=<prompt> (argument).
        args = ["--output-format", fmt, "-p"] if via_stdin else ["--output-format", fmt, "-p={prompt}"]
        res = run_cli("agy", args, PROMPT, via_stdin, env)
        res.output_format = fmt
        if not res.error:
            parse_json_stdout(res)
        if not res.error:
            extract_agy(res)
        res.ok = not res.error
        attempts.append(res)
        if res.ok:
            return res, attempts
    return attempts[-1], attempts


def report(res: Result) -> None:
    status = "OK" if res.ok else "FAIL"
    print(f"== {res.cli}: {status} ==")
    print(f"  command    : {' '.join(res.command) if res.command else '(not run)'}")
    print(f"  prompt via : {res.prompt_via}  | output-format: {res.output_format}")
    if res.error:
        print(f"  error      : {res.error}")
    print(f"  reply      : {res.reply!r}")
    print(f"  duration   : {res.duration_s:.1f}s")
    print(f"  usage      : {res.usage}")


def save_fixture(name: str, res: Result) -> None:
    FIXTURES.mkdir(exist_ok=True)
    if res.raw_json is not None:
        (FIXTURES / name).write_text(json.dumps(res.raw_json, indent=2, ensure_ascii=False), encoding="utf-8")
    elif res.raw_stdout:
        (FIXTURES / (name + ".raw.txt")).write_text(res.raw_stdout, encoding="utf-8")


def from_fixtures() -> int:
    for cli, fname, extract in (("claude", "claude_pong.json", extract_claude), ("agy", "agy_pong.json", extract_agy)):
        res = Result(cli=cli, prompt_via="(fixture)", output_format="(fixture)")
        res.raw_json = json.loads((FIXTURES / fname).read_text(encoding="utf-8"))
        extract(res)
        res.ok = not res.error
        report(res)
    return 0


def main() -> int:
    if "--from-fixtures" in sys.argv:
        return from_fixtures()
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    if only == "agy":  # skip the claude call (saves quota) and keep its saved fixture
        claude_res = Result(cli="claude", ok=True)
    else:
        claude_res = spike_claude()
        report(claude_res)
        save_fixture("claude_pong.json", claude_res)

    agy_res, attempts = spike_agy()
    print(f"\n(agy attempts: {len(attempts)})")
    for a in attempts:
        print(f"  - via {a.prompt_via}, format {a.output_format}: {'OK' if a.ok else 'FAIL: ' + a.error}")
    report(agy_res)
    save_fixture("agy_pong.json", agy_res)
    return 0 if (claude_res.ok and agy_res.ok) else 1


if __name__ == "__main__":
    sys.exit(main())
