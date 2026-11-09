"""run_process with python -c stand-ins, never a real agent."""

import os
import sys
import time

from orch.adapters.process import run_process


def _utf8_env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def test_captures_stdout_and_exit_code(tmp_path):
    o = run_process([sys.executable, "-c", "print('hi')"], cwd=tmp_path, timeout_s=30)
    assert o.stdout.strip() == "hi" and o.returncode == 0 and not o.timed_out


def test_non_ascii_stdout(tmp_path):
    o = run_process([sys.executable, "-c", "print('café ✓')"], cwd=tmp_path, env=_utf8_env(), timeout_s=30)
    assert o.stdout.strip() == "café ✓"


def test_non_ascii_stdin_roundtrip(tmp_path):
    code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    o = run_process([sys.executable, "-c", code], cwd=tmp_path, stdin_text="café ✓ 日本語", timeout_s=30)
    assert o.stdout == "café ✓ 日本語"


def test_invalid_utf8_is_replaced_not_raised(tmp_path):
    code = "import sys; sys.stdout.buffer.write(bytes([111, 107, 32, 255, 254]))"
    o = run_process([sys.executable, "-c", code], cwd=tmp_path, timeout_s=30)
    assert o.stdout.startswith("ok ")


def test_timeout_kills_process(tmp_path):
    t0 = time.perf_counter()
    o = run_process([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout_s=1)
    assert o.timed_out
    assert time.perf_counter() - t0 < 20


def test_timeout_kills_grandchild(tmp_path):
    marker = tmp_path / "alive.txt"
    child = (
        "import time, pathlib\n"
        "for _ in range(150):\n"
        f"    pathlib.Path({str(marker)!r}).write_text('x')\n"
        "    time.sleep(0.2)\n"
    )
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    o = run_process([sys.executable, "-c", parent], cwd=tmp_path, timeout_s=3)
    assert o.timed_out
    time.sleep(0.5)
    marker.unlink(missing_ok=True)
    time.sleep(1.0)
    assert not marker.exists()  # the grandchild is no longer writing


def test_not_found(tmp_path):
    o = run_process(["definitely-not-a-real-exe-xyz"], cwd=tmp_path, timeout_s=5)
    assert o.not_found
